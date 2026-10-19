"""Independent native evidence for calls, CR, CTR, rotations and trap behavior."""

import contextlib
import io
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.cli import main
from anyps3.elf import ElfImage
from anyps3.errors import AnyPS3Error
from anyps3.flow import lift
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program


class ProgramTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
    def test_mcrf_copies_only_selected_field_including_self_copy(self):
        from tests import test_runtime_objects
        body='(void)call;auto& state=s;'
        for target in range(8):
            for source in range(8):
                initial=0x01234567 if (target+source)%2 else 0xfedcba98
                word=(19<<26)|(target<<23)|(source<<18)
                expected=(initial&~(15<<(28-target*4)))|(((initial>>(28-source*4))&15)<<(28-target*4))
                body+=f'state.cr=UINT32_C({initial});{{{lift(word,0x100).cpp}}}assert(state.cr==UINT32_C({expected}) && state.pc==0x104);'
        test_runtime_objects.RuntimeObjectsTests().run_native(body)
        for invalid in (1,1<<11,1<<16,1<<21):
            with self.assertRaises(AnyPS3Error):lift(0x4d9c0000|invalid,0x100)

    def test_strict_unknown_rejected_diagnostic_has_explicit_trap(self):
        im = ElfImage.parse(program(0x3860002A, 0x44000002, BLR))
        with self.assertRaisesRegex(AnyPS3Error, "0x00010004"):
            compile_program(im)
        result = compile_program(im, diagnostic=True)
        self.assertEqual(len(result.operations), 1)
        self.assertEqual(list(result.traps), [0x10004])
        self.assertNotIn('case 0x10008:', result.cpp())

    def test_invalid_ctr_branch_and_unknown_spr(self):
        for word in (0x4E000420, 0x7C0002A6):
            with self.subTest(word=hex(word)), self.assertRaises(AnyPS3Error):
                lift(word, 0x10000)

    def test_call_graph_contains_target_and_continuation(self):
        im = ElfImage.parse(program(0x7FE802A6, 0x4800000D, 0x7FE803A6, BLR, 0x3860002A, BLR))
        result = compile_program(im)
        self.assertEqual(len(result.operations), 6)
        self.assertEqual(result.traps, {})

    def test_limits_and_process_entry_validation(self):
        im = ElfImage.parse(program(0x3860002A, BLR))
        for value in (0, 4000001):
            with self.assertRaises(AnyPS3Error):
                compile_program(im, max_instructions=value)
        with self.assertRaises(AnyPS3Error):
            compile_program(im, max_instructions=1)
        result = compile_program(im, max_instructions=1, diagnostic=True)
        self.assertIn(0x10004, result.traps)
        for value in (0, 1000000000001):
            with self.assertRaises(AnyPS3Error):
                result.cpp(max_steps=value)
        with self.assertRaises(AnyPS3Error):
            compile_program(im, entry=0x10000, entry_kind='code', process_entry=True)

    def test_cli_strict_failure_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as d:
            inp, out = Path(d) / 'source.elf', Path(d) / 'out.cpp'
            inp.write_bytes(program(0x44000002))
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(['recompile-program', str(inp), '-o', str(out)]), 1)
            self.assertFalse(out.exists())

    def test_cli_profile_seeds_require_matching_identity_and_integer_addresses(self):
        data = program(BLR)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); inp, out, profile = root/'source.elf', root/'out.cpp', root/'profile.json'
            inp.write_bytes(data)
            for digest, seeds, message in ((None, [], 'pinned'), ('0'*64, [], 'does not match'),
                                          (hashlib.sha256(data).hexdigest(), [True], 'integer code addresses')):
                profile.write_text(json.dumps({'schema_version':1,'title':'fixture',
                    'executable_sha256':digest,'static_seed_entries':seeds}))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    status = main(['recompile-program',str(inp),'--profile',str(profile),'-o',str(out)])
                self.assertEqual(status,1); self.assertIn(message,err.getvalue()); self.assertFalse(out.exists())

    def test_cli_profile_adds_seed_without_executing_it(self):
        data = program(BLR,0x38600063,BLR)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); inp, out, profile = root/'source.elf', root/'out.cpp', root/'profile.json'
            inp.write_bytes(data)
            profile.write_text(json.dumps({'schema_version':1,'title':'fixture',
                'executable_sha256':hashlib.sha256(data).hexdigest(),'static_seed_entries':[0x10004]}))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(['recompile-program',str(inp),'--profile',str(profile),'-o',str(out)])
            self.assertEqual(status,0)
            self.assertEqual(json.loads(stdout.getvalue())['extra_entries'],[0x10004])
            self.assertIn('case 0x10004:',out.read_text())


@unittest.skipUnless(shutil.which('c++'), 'Native program tests require c++.')
class NativeProgramTests(unittest.TestCase):
    def run_native(self, words, expected=None, fault=None, **options):
        data = program(*words)
        max_steps = options.pop('max_steps', 10000)
        result = compile_program(ElfImage.parse(data), **options)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source, binary, elf = root/'native.cpp', root/'native', root/'source.elf'
            source.write_text(result.cpp(max_steps=max_steps)); elf.write_bytes(data)
            build = subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(source),'-o',str(binary)],
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary),str(elf)], capture_output=True, text=True, timeout=5)
            if fault:
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertIn(fault, run.stderr)
                self.assertEqual(run.stdout, '')
            else:
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertTrue(run.stdout.startswith(f'returned r3={expected} steps='), run.stdout)

    def test_call_returns_using_saved_lr(self):
        self.run_native([0x7FE802A6,0x4800000D,0x7FE803A6,BLR,0x3860002A,BLR],42)

    def test_stack_saved_lr_and_nested_call(self):
        # Prologue saves caller LR; subroutine calls another and restores its LR.
        self.run_native([0x7C0802A6,0xF821FF91,0xF8010080,0x48000015,
                         0xE8010080,0x38210070,0x7C0803A6,BLR,
                         0x7FE802A6,0x4800000D,0x7FE803A6,BLR,0x3860002A,BLR],42)

    def test_ctr_loop(self):
        self.run_native([0x38600000,0x38800003,0x7C8903A6,0x38630001,0x4200FFFC,BLR],3)

    def test_cr_signed_branch(self):
        self.run_native([0x3860FFFF,0x2F830000,0x419C000C,0x38600063,BLR,0x3860002A,BLR],42)

    def test_cr_unsigned_branch(self):
        self.run_native([0x3860FFFF,0x2BA30000,0x419D000C,0x38600063,BLR,0x3860002A,BLR],42)

    def test_rotates_and_sign_extension(self):
        cases = [([0x3860FFFF,0x78630020,BLR],0xFFFFFFFF),  # clrldi 32
                 ([0x3860FFFF,0x5463003E,BLR],0xFFFFFFFF),  # word rotation truncates
                 ([0x3860FFFF,0x7C6307B4,BLR],(1<<64)-1), # extsw
                 ([0x3860FFFF,0x70630000,0x4182000C,0x38600063,BLR,0x3860002A,BLR],42)]
        for words, value in cases:
            with self.subTest(words=words): self.run_native(words,value)

    def test_diagnostic_trap_is_never_success(self):
        self.run_native([0x3860002A,0x44000002,BLR], fault='STOP pc=0x00010004', diagnostic=True)

    def test_lost_lr_hits_step_limit_instead_of_false_return(self):
        self.run_native([0x4800000D,BLR,0xFFFFFFFF,0x3860002A,BLR],
                        fault='execution step limit', max_steps=20)

    def test_unknown_indirect_target_has_no_fallback(self):
        self.run_native([0x38602000,0x7C6903A6,0x4E800420], fault='no statically translated block')

    def test_process_entry_sets_argc(self):
        self.run_native([BLR],1, process_entry=True)

    def test_timebase_frequency_syscall_and_unknown_syscall(self):
        self.run_native([0x39600093,0x44000002,BLR],79800000, process_entry=True)
        self.run_native([0x39607FFF,0x44000002,BLR],fault='unimplemented LV2 syscall 32767',process_entry=True)

    def test_fpr_memory_and_sign_moves_preserve_payload(self):
        # Integer bits -1 -> memory -> f1 -> fneg f2 -> memory -> integer r3.
        self.run_native([0x3880FFFF,0xF8820000,0xC8220000,0xFC400850,0xD8420008,0xE8620008,BLR],
                        (1<<63)-1)

    def test_indirect_call_to_explicit_static_seed(self):
        self.run_native([0x7FE802A6,0x3C800001,0x60840020,0x7C8903A6,0x4E800421,
                         0x7FE803A6,BLR,0xFFFFFFFF,0x3860002A,BLR],42,extra_entries=(0x10020,))

    def test_floating_arithmetic_instruction_pipeline(self):
        self.run_native([0x38800006,0xF8820000,0xC8220000,0xFC200E9C,
                         0x38800003,0xF8820000,0xC8420000,0xFC40169C,
                         0xFC611024,0xFC601E5E,0xD8620000,0xE8620000,BLR],2)

    def test_integer_shift_carry_and_high_signed_products(self):
        cases=[([0x3880FFFD,0x7C830E70,BLR],(1<<64)-2),
               ([0x3880FFFD,0x7C830E70,0x7C6102A6,BLR],0x20000000),
               ([0x3880FFFE,0x38A00003,0x7C642896,BLR],(1<<64)-1),
               ([0x3880FFFE,0x38A00003,0x7C642892,BLR],(1<<64)-1)]
        for words,value in cases:
            with self.subTest(words=words): self.run_native(words,value)

    def test_indexed_load_update_and_sign_extension(self):
        cases=[([0x3880FFFF,0x90820004,0x38800004,0x7C62202E,BLR],0xffffffff),
               ([0x3880FFFF,0x90820004,0x38800004,0x7C6222AA,BLR],(1<<64)-1),
               ([0x3880FFFF,0x90820004,0x38800004,0x7C62206E,0x7C431378,BLR],0x20084)]
        for words,value in cases:
            with self.subTest(words=words): self.run_native(words,value)
        with self.assertRaisesRegex(AnyPS3Error,'Invalid indexed update'):
            lift(0x7C63206E,0x10000)

    def test_vector_load_logic_and_store(self):
        self.run_native([0x3880FFFF,0xF8820000,0x38A00000,0x7C2228CE,
                         0x104214C4,0x10421504,0x106114C4,0x7C6229CE,0xE8620008,BLR],(1<<64)-1)

    def test_carry_is_input_and_output_of_extended_add(self):
        self.run_native([0x3880FFFF,0x30640001,0x38800029,0x7C640194,BLR],42)

    def test_vrsave_is_32_bit_and_roundtrips_without_changing_vectors(self):
        self.run_native([0x3880ffff,0x7c8043a6,0x7c6042a6,BLR],0xffffffff)

    def test_word_and_doubleword_division_signed_unsigned_and_zero(self):
        for xo,expected in [(491,(1<<64)-4),(489,(1<<64)-4),(459,2147483643),(457,9223372036854775803)]:
            instruction=(31<<26)|(3<<21)|(4<<16)|(5<<11)|(xo<<1)
            with self.subTest(xo=xo):self.run_native([0x3880fff7,0x38a00002,instruction,BLR],expected)
        # divwu 2/0: architecturally undefined; Cell result 0 (RPCS3 PPUInterpreter DIVWU).
        self.run_native([0x38800002,0x38a00000,0x7c642b96,BLR],0)
