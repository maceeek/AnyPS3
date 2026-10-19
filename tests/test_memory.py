"""Native memory semantics, bounds, and input-identity regression tests."""

from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.errors import AnyPS3Error
from anyps3.ppu import decode, recompile
from anyps3.runtime import STACK_POINTER
from tests.test_anyps3 import BLR, program


class MemoryDecodeTests(unittest.TestCase):
    def test_invalid_update_forms_and_reserved_ds_variants(self):
        for word in (0x84630004, 0x84600004, 0x94600004, 0xE8630001,
                     0xE8600001, 0xF8600001, 0xE8610003, 0xF8610002, 0xF8610003):
            with self.subTest(word=hex(word)), self.assertRaises(AnyPS3Error):
                decode(word, 0x10000)

    def test_memory_map_rejects_overlaps_and_stack_collision(self):
        for address in (0x10000, 0xD0000000):
            data = bytearray(program(0x80620000, BLR))
            struct.pack_into(">Q", data, 120 + 16, address)
            with self.subTest(address=hex(address)), self.assertRaisesRegex(AnyPS3Error, "[Oo]verlap|Ambiguous"):
                recompile(ElfImage.parse(bytes(data)), entry=0x10000, entry_kind="code").cpp()

    def test_memory_map_bounds_allocation(self):
        data = bytearray(program(0x80620000, BLR))
        struct.pack_into(">Q", data, 120 + 40, 512 * 1024 * 1024)
        with self.assertRaisesRegex(AnyPS3Error, "memory limit"):
            recompile(ElfImage.parse(bytes(data))).cpp()


@unittest.skipUnless(shutil.which("c++"), "Native memory tests require c++.")
class NativeMemoryTests(unittest.TestCase):
    def run_case(self, words, *, expected=None, fault=None, alter_input=False):
        data = program(*words, BLR)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, binary, elf = root / "memory.cpp", root / "memory", root / "source.elf"
            source.write_text(recompile(ElfImage.parse(data)).cpp())
            elf.write_bytes(data if not alter_input else data[:-1] + bytes([data[-1] ^ 1]))
            build = subprocess.run(["c++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                                    str(source), "-o", str(binary)], capture_output=True, text=True, timeout=30)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary), str(elf)], capture_output=True, text=True, timeout=5)
            if fault:
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertIn(fault, run.stderr)
                self.assertEqual(run.stdout, "")
            else:
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(run.stdout, f"r3={expected}\n")

    def test_loaded_data_and_zero_bss(self):
        self.run_case([0x8062FF80], expected=0x10000)  # lwz r3,-128(r2): OPD code
        self.run_case([0xE8620000], expected=0)  # ld r3,0(r2): zero-initialized BSS

    def test_big_endian_store_and_width(self):
        # lis r4,0x1234; ori r4,r4,0x5678; stw r4,0(r2); lbz r3,0(r2)
        self.run_case([0x3C801234, 0x60845678, 0x90820000, 0x88620000], expected=0x12)
        self.run_case([0x3880FFFF, 0x90820000, 0x80620000], expected=0xFFFFFFFF)
        self.run_case([0x3880FFFF, 0xF8820000, 0xE8620000], expected=(1 << 64) - 1)

    def test_signed_and_unsigned_loads(self):
        for load, expected in ((0xA8620000, (1 << 64) - 1), (0xA0620000, 65535),
                               (0xE8620002, (1 << 64) - 1)):
            with self.subTest(load=hex(load)):
                self.run_case([0x3880FFFF, 0x90820000, load], expected=expected)

    def test_stack_update_preserves_old_value_and_signed_ds_displacement(self):
        # stdu r1,-112(r1); ld r3,0(r1) must recover the OLD SP.
        self.run_case([0xF821FF91, 0xE8610000], expected=STACK_POINTER)
        # stdu r1,-112(r1); ld r3,0(r1); subf r3,r1,r3 -> frame size
        self.run_case([0xF821FF91, 0xE8610000, 0x7C611850], expected=112)

    def test_load_update_sets_base_and_store_updates_after_access(self):
        # stwu r2,-4(r2); lwzu r3,0(r2) obtains old TOC, not updated TOC.
        self.run_case([0x9442FFFC, 0x84620000], expected=0x20080)
        self.run_case([0xE862FF81, 0x38620000], expected=0x20000)  # ldu r3,-128(r2); mr r3,r2

    def test_zero_ra_ignores_r0(self):
        # r0=TOC; lwz r3,0(0) must still access address zero and fault.
        self.run_case([0x38020000, 0x80600000], fault="unmapped")

    def test_memory_faults(self):
        cases = [([0x80600000], "unmapped"),
                 ([0xE862007C], "unmapped or split"),  # crosses data-segment end
                 ([0x3C800001, 0x90640000], "permission"),  # write RX code
                 ([0x3880FFFF, 0x80640000], "outside 32-bit")]
        for words, fault in cases:
            with self.subTest(fault=fault):
                self.run_case(words, fault=fault)

    def test_image_mismatch_rejected_before_execution(self):
        self.run_case([0x8062FF80], alter_input=True, fault="CRC32 mismatch")


@unittest.skipUnless(shutil.which("c++"), "Native memory tests require c++.")
class RegionCacheAndReservationTests(unittest.TestCase):
    def test_cached_region_revalidated_and_reservations_track_observed_lines(self):
        from tests import test_runtime_objects
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''(void)call;(void)r;
 mem.map(0x40000,0x1000,6,nullptr,0);mem.map(0x50000,0x1000,6,nullptr,0);
 mem.store(0x40010,4,0x11223344);mem.store(0x50010,4,0x55667788);
 for(int i=0;i<3;++i)assert(mem.load(0x40010,4)==0x11223344 && mem.load(0x50010,4)==0x55667788);
 // Erase the region that the cache last hit: the address must fault, the other still works.
 assert(mem.load(0x50010,4)==0x55667788);
 mem.regions.erase(std::remove_if(mem.regions.begin(),mem.regions.end(),[](const auto& v){return v.base==0x50000;}),mem.regions.end());
 fault([&]{mem.load(0x50010,4);});assert(mem.load(0x40010,4)==0x11223344);
 // Replace it at the same address with new bytes: the new mapping is seen.
 mem.map(0x50000,0x1000,6,nullptr,0);assert(mem.load(0x50010,4)==0);
 // Writes before a reservation do not matter; writes to the reserved line invalidate it.
 mem.store(0x40080,4,1);
 const auto token=mem.reservation_token(0x40080);
 mem.store(0x40000,4,2);mem.store(0x40100,4,3);   // other lines (host 0x40000 base is vector data)
 const auto same=mem.reservation_token(0x40080);
 mem.store(0x40084,1,4);
 const auto changed=mem.reservation_token(0x40080);
 assert(changed!=token);
 // A large write spanning the line (e.g. a remapped block) also invalidates.
 const auto before=mem.reservation_token(0x40080);mem.mark_write(mem.access(0x40000,0x1000,false),0x1000);
 assert(mem.reservation_token(0x40080)!=before);
 (void)same;
''')


@unittest.skipUnless(shutil.which("c++"), "Native memory tests require c++.")
class PartialGranuleReservationTests(unittest.TestCase):
    def test_lwarx_stwcx_at_segment_end_inside_partial_granule(self):
        # Original firmware case: lwarx r9,0,r31 with r31=0x202b0c2c, data segment
        # 0x202b0000+0xc38 (granule 0x202b0c00-0x202b0c7f extends past the segment).
        from anyps3.flow import lift
        from tests import test_runtime_objects
        load = lift(0x7d20f828, 0x100).cpp                         # lwarx r9,0,r31
        store = lift((31 << 26) | (5 << 21) | (31 << 11) | (150 << 1) | 1, 0x104).cpp  # stwcx. r5,0,r31
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''(void)call;auto& state=s;
 mem.map(0x60000,0xc38,6,nullptr,0);
 auto load=[&]{''' + load + r'''};auto store=[&]{''' + store + r'''};
 // The old full-line token rejects the partial granule; the word itself is mapped.
 fault([&]{mem.reservation_token(0x60c00);});
 mem.store(0x60c2c,4,0x01020304);r[31]=0x60c2c;s.cr=0;
 load();assert(r[9]==0x01020304 && s.reservation_address==0x60c2c);
 r[5]=0xa1b2c3d4;store();assert(mem.load(0x60c2c,4)==0xa1b2c3d4 && (s.cr>>28)==2);
 // A write elsewhere in the same granule loses the reservation; stwcx. fails without storing.
 load();mem.store(0x60c34,4,7);r[5]=0x55;store();assert(mem.load(0x60c2c,4)==0xa1b2c3d4 && (s.cr>>28)==0);
 // A write to another granule keeps it.
 load();mem.store(0x60b00,4,9);r[5]=0x66;store();assert(mem.load(0x60c2c,4)==0x66 && (s.cr>>28)==2);
 // Bytes past the segment are still unmapped for ordinary access.
 fault([&]{mem.load(0x60c38,4);});r[31]=0x60c38;fault([&]{load();});
 // Fully mapped granule: same token as the 128-byte SPU form.
 assert(mem.reservation_token(0x60b2c,4)==mem.reservation_token(0x60b00));
 // A second mapping sharing the granule would escape tracking: explicit stop.
 mem.map(0x60c40,0x40,6,nullptr,0);r[31]=0x60c2c;fault([&]{load();});
 fault([&]{mem.reservation_token(0x60c44,4);});
''')


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class MemoryFastPathTests(unittest.TestCase):
    def build(self, root, body):
        from anyps3.runtime import MEMORY_CPP
        source='#include <cstdint>\n#include <cstdio>\n#include <cinttypes>\n#include <cassert>\n'+MEMORY_CPP
        source+='\ntemplate<class F>void fault(F f){bool failed=false;try{f();}catch(const std::runtime_error&){failed=true;}assert(failed);}\nint main(){(void)crc32;'+body+'}\n'
        (root/'test.cpp').write_text(source)
        build=subprocess.run(['c++','-std=c++17','-O2','-Wall','-Wextra','-Werror',str(root/'test.cpp'),'-o',str(root/'test')],capture_output=True,text=True,timeout=30)
        self.assertEqual(build.returncode,0,build.stderr)

    def test_page_cache_mutation_aliases_observers_endian_and_split(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.build(root,r'''
 GuestMemory mem;mem.map(0x10000,0x2000,6,nullptr,0);mem.map(0x30000,8,6,nullptr,0);
 mem.map(0x30008,8,6,nullptr,0);mem.map(0x40000,0x1000,5,nullptr,0);
 mem.store(0x10001,8,UINT64_C(0x0123456789abcdef));
 assert(mem.load(0x10001,8)==UINT64_C(0x0123456789abcdef));
 assert(mem.load(0x10001,1)==1 && mem.load(0x10002,2)==0x2345 && mem.load(0x10004,4)==0x6789abcd);
 mem.store(0x10ffe,4,0xaabbccdd);assert(mem.load(0x10ffe,4)==0xaabbccdd);
 mem.store(0x30004,4,0x11223344);mem.store(0x30008,4,0x55667788);
 for(unsigned i=0;i<3;++i){assert(mem.load(0x30004,4)==0x11223344);assert(mem.load(0x30008,4)==0x55667788);}
 fault([&]{mem.load(0x30004,8);});fault([&]{mem.load(UINT64_MAX,8);});fault([&]{mem.store(0x40000,4,0);});
 {GuestMemory::SplitReadScope scope(mem);assert(mem.load(0x30004,8)==UINT64_C(0x1122334455667788));fault([&]{mem.store(0x30004,8,0);});}
 // All aliased accesses notify a dynamically installed observer exactly once.
 auto backing=mem.regions[0].bytes;mem.regions.push_back({0x60000,6,backing});
 unsigned reads=0,writes=0;
 mem.host_access_observer=[&](const void* p,std::uint64_t n,bool write){assert(p && n);if(write)++writes;else ++reads;};
 assert(mem.load(0x60001,2)==0x0123);mem.store(0x60001,2,0xbeef);assert(reads==1 && writes==1);
 const auto token=mem.reservation_token(0x10000,4);mem.store(0x60004,4,0x89abcdef);
 assert(mem.reservation_token(0x10000,4)!=token);
 // A cached hit still observes later permissions and current vector contents.
 mem.regions.back().flags=4;fault([&]{mem.store(0x60004,4,0);});mem.regions.back().flags=6;
 mem.host_access_observer={};mem.regions.erase(mem.regions.begin());
 fault([&]{mem.load(0x10001,4);});assert(mem.load(0x60001,2)==0xbeef);
 mem.map(0x10000,0x1000,6,nullptr,0);assert(mem.load(0x10001,4)==0);
 // Replace an existing cache hit's backing, without changing its vector index.
 assert(mem.load(0x60001,2)==0xbeef);mem.regions[mem.last_region].bytes=std::make_shared<GuestBacking>(0x1000,0x5a);
 assert(mem.load(0x60001,2)==0x5a5a);
 // Observer failure propagates on the cached path and its recursion guard resets.
 mem.host_access_observer=[](const void*,std::uint64_t,bool){throw std::runtime_error("observer");};
 fault([&]{mem.load(0x60001,2);});assert(!mem.in_host_access_observer);
''')
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_guest_backings_are_host_page_aligned_for_guard_ownership(self):
        # Build 319's menu spent ~27 ms/frame in allocator-edge readbacks: the
        # RSX local backing started at host address 16 (mod 4096), so its first
        # guest page could not be owned by a host page protection.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.build(root,r'''
 GuestMemory mem;const std::uint64_t sizes[]={8,0x1001,0x2000,0xF900000};std::uint64_t base=0x10000000;
 for(auto size:sizes){mem.map(base,size,6,nullptr,0);const auto& r=mem.regions.back();
  assert(reinterpret_cast<std::uintptr_t>(r.bytes->data())%4096==0);assert(r.bytes->size()==size);
  for(std::uint64_t i=0;i<size;i+=size/4+1)assert(r.bytes->at(i)==0);
  GuestMemory::HostGuard probe(false);
  if(size%4096==0){assert(probe.add(r.bytes->data(),4096,*r.bytes));assert(probe.add(r.bytes->data()+size-4096,4096,*r.bytes));}
  assert(!probe.add(r.bytes->data()+size-1,2,*r.bytes));probe.spans.clear();
  base+=0x10000000;}
 GuestBacking copy(3,0x7f);assert(reinterpret_cast<std::uintptr_t>(copy.data())%4096==0 && copy[2]==0x7f);
''')
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_trace_history_wrap_and_explicit_live_modes(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            self.build(root,r'''
 for(unsigned i=0;i<520;++i)anyps3_trace_dma("DMA sequence=%u\n",i);
 anyps3_trace_import("import marker\n");
 std::fputs("DUMP NOW\n",stderr);anyps3_trace_dump();
''')
            env={**os.environ,'ANYPS3_TRACE_IMPORTS':'0','ANYPS3_SPU_TRACE_DMA':'0'}
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)
            lines=run.stderr.splitlines();self.assertEqual(lines[0],'DUMP NOW')
            self.assertEqual(lines[1],'TRACE HISTORY last=512 total=521')
            self.assertEqual(lines[2],'DMA sequence=9');self.assertEqual(lines[-1],'import marker');self.assertEqual(len(lines),514)
            env.update(ANYPS3_TRACE_IMPORTS='1',ANYPS3_SPU_TRACE_DMA='1')
            live=subprocess.run([str(root/'test')],capture_output=True,text=True,env=env,timeout=5)
            self.assertEqual(live.returncode,0,live.stderr)
            self.assertTrue(live.stderr.startswith('DMA sequence=0\nDMA sequence=1\n'))
            self.assertIn('import marker\nDUMP NOW\nTRACE HISTORY',live.stderr)
