import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.cli import main
from anyps3.demo import CODE_ADDRESS, CODE_OFFSET, DESCRIPTOR_ADDRESS, DESCRIPTOR_OFFSET, make_demo
from anyps3.elf import ElfImage
from anyps3.errors import AnyPS3Error
from anyps3.ppu import decode, recompile
from anyps3.profile import check_profile


BLR = 0x4E800020


def program(*words: int) -> bytes:
    """Replace the demo code while preserving a separate data/OPD segment."""
    code = struct.pack(f">{len(words)}I", *words)
    if len(code) > DESCRIPTOR_OFFSET - CODE_OFFSET:
        raise ValueError("Fixture code is too large.")
    data = bytearray(make_demo())
    data[CODE_OFFSET:DESCRIPTOR_OFFSET] = bytes(DESCRIPTOR_OFFSET - CODE_OFFSET)
    data[CODE_OFFSET:CODE_OFFSET + len(code)] = code
    struct.pack_into(">QQ", data, 64 + 32, len(code), len(code))
    return bytes(data)


class ElfTests(unittest.TestCase):
    def test_demo_header_segments_and_ps3_descriptor(self):
        image = ElfImage.parse(make_demo())
        self.assertEqual((image.bits, image.byte_order, image.machine), (64, "big", 21))
        self.assertEqual(image.resolve_entry(), (CODE_ADDRESS, DESCRIPTOR_ADDRESS + 0x80))
        self.assertEqual(image.segments[0].permissions, "R-X")
        self.assertEqual(image.segments[1].memory_size, 0x100)
        self.assertEqual(image.report()["sha256"], hashlib.sha256(make_demo()).hexdigest())

    def test_every_truncated_demo_is_rejected(self):
        demo = make_demo()
        for size in range(len(demo)):
            with self.subTest(size=size), self.assertRaises(AnyPS3Error):
                ElfImage.parse(demo[:size])

    def test_container_detection(self):
        with self.assertRaisesRegex(AnyPS3Error, "SELF container"):
            ElfImage.parse(b"SCE\0" + bytes(100))
        with self.assertRaisesRegex(AnyPS3Error, "Not an ELF"):
            ElfImage.parse(b"not an executable")

    def test_invalid_load_ranges_and_alignment(self):
        # Independent field corruptions in the ELF64 program header.
        for offset, value, message in [
            (64 + 8, 0xFFFFFFFFFFFFFFFF, "outside"),
            (64 + 40, 1, "exceeds memory"),
            (64 + 48, 3, "alignment"),
            (64 + 16, 0xFFFFFFFFFFFFFFFC, "overflows"),
        ]:
            data = bytearray(make_demo())
            struct.pack_into(">Q", data, offset, value)
            with self.subTest(offset=offset), self.assertRaisesRegex(AnyPS3Error, message):
                ElfImage.parse(bytes(data))

    def test_program_header_table_bounds(self):
        data = bytearray(make_demo())
        struct.pack_into(">Q", data, 32, 0xFFFFFFFFFFFFFFFF)
        with self.assertRaisesRegex(AnyPS3Error, "Program-header table"):
            ElfImage.parse(bytes(data))

    def test_bss_and_nonexecutable_reads_are_rejected(self):
        image = ElfImage.parse(make_demo())
        with self.assertRaisesRegex(AnyPS3Error, "not fully file-backed"):
            image.read_virtual(DESCRIPTOR_ADDRESS + 8, 4)
        with self.assertRaisesRegex(AnyPS3Error, "not in an executable"):
            image.read_virtual(DESCRIPTOR_ADDRESS, 4, executable=True)
        with self.assertRaisesRegex(AnyPS3Error, "Unmapped"):
            image.read_virtual(0x1234, 4)

    def test_ambiguous_segment_mapping_is_rejected(self):
        data = bytearray(make_demo())
        struct.pack_into(">Q", data, 120 + 16, CODE_ADDRESS)
        image = ElfImage.parse(bytes(data))
        with self.assertRaisesRegex(AnyPS3Error, "Ambiguous"):
            image.read_virtual(CODE_ADDRESS, 4)

    def test_elf32_spu_inspection(self):
        ident = b"\x7fELF" + bytes([1, 2, 1, 0, 0]) + bytes(7)
        data = ident + struct.pack(">HHIIIIIHHHHHH", 2, 23, 1, 0, 0, 0, 0, 52, 0, 0, 0, 0, 0)
        image = ElfImage.parse(data)
        self.assertEqual(image.report()["elf"]["machine_name"], "SPU")
        self.assertIsNone(image.report()["ppu_entry"])
        with self.assertRaisesRegex(AnyPS3Error, "ELF64 big-endian"):
            recompile(image)

    def test_little_endian_host_elf_inspection(self):
        ident = b"\x7fELF" + bytes([2, 1, 1, 0, 0]) + bytes(7)
        data = ident + struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 0, 0, 0, 64, 0, 0, 0, 0, 0)
        image = ElfImage.parse(data)
        self.assertEqual(image.machine, 62)
        self.assertEqual(image.byte_order, "little")
        with self.assertRaises(AnyPS3Error):
            recompile(image)

    def test_prx_is_not_treated_as_fixed_executable(self):
        data = bytearray(make_demo())
        struct.pack_into(">H", data, 16, 3)
        with self.assertRaisesRegex(AnyPS3Error, "ET_EXEC"):
            recompile(ElfImage.parse(bytes(data)))

    def test_direct_code_entry_requires_explicit_mode(self):
        data = bytearray(make_demo())
        struct.pack_into(">Q", data, 24, CODE_ADDRESS)
        image = ElfImage.parse(bytes(data))
        with self.assertRaises(AnyPS3Error):
            image.resolve_entry()
        self.assertEqual(image.resolve_entry(kind="code"), (CODE_ADDRESS, 0))


class RecompilerTests(unittest.TestCase):
    def test_demo_lifts_four_instructions(self):
        result = recompile(ElfImage.parse(make_demo()))
        self.assertEqual([i.mnemonic for i in result.instructions], ["addi", "addi", "add", "blr"])

    def test_unsupported_variants_are_not_silently_ignored(self):
        for word in (0x44000002, 0x48000005, 0x41820008, 0x7C632215,
                     0x7C632614, 0x7C600028, 0x4E800021, 0):
            with self.subTest(word=hex(word)), self.assertRaisesRegex(AnyPS3Error, "0x00010000"):
                recompile(ElfImage.parse(program(word, BLR)))

    def test_unconditional_branch_skips_unreachable_data(self):
        result = recompile(ElfImage.parse(program(0x48000008, 0xFFFFFFFF, 0x3860002A, BLR)))
        self.assertEqual([i.address for i in result.instructions], [CODE_ADDRESS, CODE_ADDRESS + 8, CODE_ADDRESS + 12])

    def test_backward_and_absolute_branch_targets(self):
        self.assertEqual(decode(0x4BFFFFFC, 0x10004).successors, (0x10000,))
        self.assertEqual(decode(0x48010002, 0x20000).successors, (0x10000,))

    def test_no_return_and_instruction_limit(self):
        with self.assertRaisesRegex(AnyPS3Error, "No reachable blr"):
            recompile(ElfImage.parse(program(0x48000000)))
        with self.assertRaisesRegex(AnyPS3Error, "Instruction limit"):
            recompile(ElfImage.parse(make_demo()), max_instructions=2)
        with self.assertRaisesRegex(AnyPS3Error, "Instruction limit"):
            recompile(ElfImage.parse(make_demo()), max_instructions=0)

    def test_fallthrough_cannot_read_padding(self):
        with self.assertRaisesRegex(AnyPS3Error, "Unmapped"):
            recompile(ElfImage.parse(program(0x3860002A)))

    def test_branch_cannot_execute_data_segment(self):
        with self.assertRaisesRegex(AnyPS3Error, "not in an executable"):
            recompile(ElfImage.parse(program(0x48020002)))  # ba 0x20000


@unittest.skipUnless(shutil.which("c++"), "Native integration tests require c++ on PATH.")
class NativeExecutionTests(unittest.TestCase):
    def test_compile_and_execute_integer_semantics(self):
        # Literal opcode vectors cross-checked with LLVM's PowerPC assembler.
        cases = [
            ("demo", make_demo(), 42),
            ("negative immediate", program(0x3860FFFF, BLR), (1 << 64) - 1),
            ("addi ignores r0", program(0x38000007, 0x38600001, BLR), 1),
            ("ori reads r0", program(0x38000007, 0x60030000, BLR), 7),
            ("addis sign extension", program(0x3C60FFFF, BLR), (1 << 64) - 65536),
            ("wraparound", program(0x3860FFFF, 0x38630002, BLR), 1),
            ("subf order", program(0x38600003, 0x38800009, 0x7C632050, BLR), 6),
            ("or", program(0x3880000C, 0x38A0000A, 0x7C832B78, BLR), 14),
            ("xor", program(0x3880000C, 0x38A0000A, 0x7C832A78, BLR), 6),
            ("and", program(0x3880000C, 0x38A0000A, 0x7C832838, BLR), 8),
            ("oris", program(0x38600001, 0x64638000, BLR), 0x80000001),
            ("xori", program(0x3860000F, 0x6863000A, BLR), 5),
            ("xoris", program(0x38600001, 0x6C63FFFF, BLR), 0xFFFF0001),
            ("branch", program(0x48000008, 0xFFFFFFFF, 0x3860002A, BLR), 42),
            ("TOC initialization", program(0x38420000, 0x38620000, BLR), DESCRIPTOR_ADDRESS + 0x80),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source, binary = path / "function.cpp", path / "function"
            for name, data, expected in cases:
                with self.subTest(name=name):
                    source.write_text(recompile(ElfImage.parse(data)).cpp(), encoding="utf-8")
                    build = subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                                            str(source), "-o", str(binary)], capture_output=True, text=True, timeout=30)
                    self.assertEqual(build.returncode, 0, build.stderr)
                    run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
                    self.assertEqual(run.returncode, 0, run.stderr)
                    self.assertEqual(run.stdout, f"r3={expected}\n")


class CliTests(unittest.TestCase):
    def invoke(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_demo_inspect_recompile_workflow(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            elf, cpp = path / "demo.elf", path / "demo.cpp"
            self.assertEqual(self.invoke(["demo", "-o", str(elf)])[0], 0)
            status, out, err = self.invoke(["inspect", str(elf), "--json"])
            self.assertEqual((status, err), (0, ""))
            self.assertEqual(json.loads(out)["ppu_entry"]["code_address"], CODE_ADDRESS)
            self.assertEqual(self.invoke(["recompile", str(elf), "-o", str(cpp)])[0], 0)
            self.assertIn("void anyps3_entry", cpp.read_text())

    def test_existing_outputs_and_input_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "demo.elf"
            elf.write_bytes(make_demo())
            self.assertEqual(self.invoke(["recompile", str(elf), "-o", str(elf)])[0], 1)
            self.assertEqual(self.invoke(["demo", "-o", str(elf)])[0], 1)
            self.assertEqual(elf.read_bytes(), make_demo())

    def test_failure_does_not_leave_generated_source(self):
        with tempfile.TemporaryDirectory() as directory:
            elf, cpp = Path(directory) / "bad.elf", Path(directory) / "bad.cpp"
            elf.write_bytes(program(0x44000002, BLR))
            status, out, err = self.invoke(["recompile", str(elf), "-o", str(cpp)])
            self.assertEqual((status, out), (1, ""))
            self.assertIn("0x44000002", err)
            self.assertFalse(cpp.exists())

    def test_missing_input_has_clean_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            status, out, err = self.invoke(["inspect", str(Path(directory) / "absent.elf"), "--json"])
            self.assertEqual((status, out), (1, ""))
            self.assertNotIn("Traceback", err)

    def test_profile_does_not_identify_synthetic_code_as_ratchet(self):
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "demo.elf"
            elf.write_bytes(make_demo())
            profile = Path(__file__).resolve().parents[1] / "profiles" / "ratchet-acit.json"
            status, out, err = self.invoke(["inspect", str(elf), "--json", "--profile", str(profile)])
            self.assertEqual((status, out), (1, ""))
            self.assertIn("does not match", err)
            pending = Path(directory) / "pending.json"
            pending.write_text(json.dumps({"schema_version": 1, "title": "Pending target", "executable_sha256": None}))
            status, out, err = self.invoke(["inspect", str(elf), "--json", "--profile", str(pending)])
            self.assertEqual((status, err), (0, ""))
            self.assertFalse(json.loads(out)["target_profile"]["identity_verified"])

    def test_pinned_profile_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile.json"
            digest = hashlib.sha256(make_demo()).hexdigest()
            profile.write_text(json.dumps({"schema_version": 1, "title": "test", "executable_sha256": digest}))
            self.assertTrue(check_profile(profile, digest)["identity_verified"])
            with self.assertRaisesRegex(AnyPS3Error, "does not match"):
                check_profile(profile, "0" * 64)
            profile.write_text('{"schema_version": 1, "title": "test", "executable_sha256": "x"}')
            with self.assertRaisesRegex(AnyPS3Error, "64-digit"):
                check_profile(profile, digest)


if __name__ == "__main__":
    unittest.main()
