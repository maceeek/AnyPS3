"""Cross-group guest ABI regressions with fixed endian/status oracles."""
import shutil
import struct
import unittest

from anyps3.services import IMPORTS
from tests import test_lv2_spu_ext, test_runtime_objects


def close_import():
    return IMPORTS[('sysPrxForUser', '0xe0da8efd')][1].replace('state', 's')


def elf_payload(spu):
    header = b'\x7fELF\x01\x02\x01' + bytes(9) + struct.pack(
        '>HHIIIIIHHHHHH', 2, 23, 1, 0x100, 52, 0, 0, 52, 32, 1, 0, 0, 0)
    return ','.join(str(v) for v in header + struct.pack(
        '>IIIIIIII', 1, 84, 0x100, 0, 16, 16, 7, 4) + spu.data)


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class IntegrationGroupsTests(unittest.TestCase):
    def native(self, body, **kwargs):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;
 auto sc=[&](unsigned n,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,
             std::uint64_t d=0,std::uint64_t e=0,std::uint64_t f=0){
     r[11]=n;r[3]=a;r[4]=b;r[5]=c;r[6]=d;r[7]=e;r[8]=f;syscall(s);return r[3];
 };
''' + body, **kwargs)

    def test_guest_create_bind_start_wait_send_and_spu_receive(self):
        # wrch out_mbox,r3; receive-event; yield; branch to self.
        spu = test_lv2_spu_ext.image([0x21a00e03, 0x110, 1, 0x32000000])
        self.native(r'''
 mem.store(0x10100,4,1);mem.store(0x10104,4,2);
 assert(sc(128,0x10000,0x10100,0,4)==0);
 const auto queue=mem.load(0x10000,4);assert(queue==0x8d000000 && s.events.queues[0].type==2);
 const std::vector<std::uint8_t> elf={''' + elf_payload(spu) + r'''};
 std::copy(elf.begin(),elf.end(),mem.access(0x10400,elf.size(),true));
 r[3]=0x10200;r[4]=0x10400;r[5]=1;guest_spu_image_import(s);assert(r[3]==0);
 // Zeroed group/thread attributes; arg1's preferred SPU word is the queue key.
 mem.store(0x10340,8,UINT64_C(0xdeadbeef00000000));
 assert(sc(170,0x10004,1,128,0x10300)==0);const auto group=mem.load(0x10004,4);
 assert(sc(172,0x10008,group,0,0x10200,0x10310,0x10340)==0);
 const auto thread=mem.load(0x10008,4);
 assert(sc(193,thread,queue,0xdeadbeef)==0);
 assert(sc(193,thread,queue,0x1234)==0x8001000a);
 assert(sc(173,group)==0);auto& g=s.spu.groups[0];auto& t=g.threads[0];
 assert(g.state==3 && t.waiting_receive && t.pc==0x104 && t.steps==1);
 // PPU receives reject even an empty SPU queue, before touching invalid outputs.
 assert(sc(130,queue,0,0)==0x80010002);
 assert(sc(131,queue,0,4,0)==0x80010002);
 assert(sc(134,0x1000c,1,0xabcdef)==0);const auto port=mem.load(0x1000c,4);
 assert(sc(136,port,queue)==0);
 assert(sc(138,port,UINT64_C(0xaabbccdd12345678),UINT64_C(0x1122334487654321),0x80000000)==0);
 mem.store(0x10040,4,0xfeedface);
 assert(sc(131,queue,0x10060,4,0x10040)==0x80010002 && mem.load(0x10040,4)==0xfeedface);
 assert(s.events.queues[0].data.size()==1);
 assert(spu_run(s,0) && g.state==6 && !t.waiting_receive && t.pc==0x108 && t.steps==2);
 assert((t.in_mailbox==std::vector<std::uint32_t>{0,0x12345678,0x87654321,0x80000000}));
 assert(s.events.queues[0].data.empty());
 assert(sc(194,thread,0xdeadbeef)==0 && sc(194,thread,0xdeadbeef)==0x80010005);
''', spu_images=(spu,))

    def test_ppu_queue_defaults_unchanged_and_invalid_types_transactional(self):
        self.native(r'''
 Events::Queue legacy{1,4,0,{}};assert(legacy.type==1);
 mem.store(0x10100,4,1);mem.store(0x10104,4,3);mem.store(0x10000,4,0x55555555);
 assert(sc(128,0x10000,0x10100,0,4)==0x80010002);
 assert(s.events.queues.empty() && mem.load(0x10000,4)==0x55555555);
 mem.store(0x10104,4,1);assert(sc(128,0x10000,0x10100,0,1)==0);
 const auto queue=mem.load(0x10000,4);
 assert(sc(134,0x10004,1,0x12345678)==0);const auto port=mem.load(0x10004,4);
 assert(sc(136,port,queue)==0 && sc(138,port,1,2,3)==0);
 assert(sc(138,port,4,5,6)==0x8001000a);
 assert(sc(130,queue,0,0)==0 && r[4]==0x12345678 && r[5]==1 && r[6]==2 && r[7]==3);
 assert(sc(138,port,4,5,6)==0 && sc(131,queue,0x10200,1,0x10008)==0);
 const std::array<std::uint8_t,8> source={0,0,0,0,0x12,0x34,0x56,0x78};
 assert(std::equal(source.begin(),source.end(),mem.access(0x10200,8,false)));
 assert(mem.load(0x10008,4)==1 && mem.load(0x10208,8)==4 && mem.load(0x10218,8)==6);
 assert(sc(129,queue,0)==0 && sc(130,queue,0,0)==0x80010005);
''')

    def test_kernel_image_close_import_and_syscall_share_handle_lifetime(self):
        spu = test_lv2_spu_ext.image([0x32000000] * 4)
        self.native(r'''
 char dir[]="/tmp/anyps3-integrate-spu-XXXXXX";assert(::mkdtemp(dir));s.fs.hdd_root=dir;
 const auto path=std::filesystem::path(dir)/"image.elf";
 const std::vector<std::uint8_t> elf={''' + elf_payload(spu) + r'''};
 {std::ofstream f(path,std::ios::binary);f.write(reinterpret_cast<const char*>(elf.data()),elf.size());}
 const char guest[]="/dev_hdd0/image.elf";std::copy_n(guest,sizeof(guest),mem.access(0x10100,sizeof(guest),true));
 assert(sc(156,0x10200,0x10100)==0 && sc(156,0x10210,0x10100)==0);
 assert(mem.load(0x10200,4)==1 && mem.load(0x10204,4)==0x23000100 && mem.load(0x10214,4)==0x23000200);
 assert(sc(170,0x10000,2,128,0x10300)==0);const auto group=mem.load(0x10000,4);
 assert(sc(172,0x10004,group,0,0x10200,0x10310,0x10340)==0);
 assert(sc(172,0x10008,group,1,0x10210,0x10310,0x10340)==0);
 const auto initial=s.spu.groups[0].threads[0].initial_ls;assert(initial && (*initial)[0x100]==0x32);
 r[3]=0x10200;
''' + close_import() + r'''
 assert(r[3]==0 && s.spu_ext.images.size()==1 && initial->at(0x100)==0x32);
 assert(sc(158,0x10200)==0x80010005);
 r[3]=0x10200;
''' + close_import() + r'''
 assert(r[3]==UINT64_C(0xffffffff80010005));
 assert(sc(173,group)==0);auto& g=s.spu.groups[0];
 assert(g.threads[0].steps==1024 && g.threads[1].steps==1024);
 const auto ls=g.context;
 assert(sc(158,0x10210)==0 && s.spu_ext.images.empty());
 r[3]=0x10210;
''' + close_import() + r'''
 assert(r[3]==UINT64_C(0xffffffff80010005) && g.context==ls && !g.threads[0].stopped && !g.threads[1].stopped);
 assert(g.threads[1].initial_ls && g.threads[1].initial_ls->at(0x100)==0x32);
 assert(spu_run(s,1) && g.threads[0].steps==1025 && g.threads[1].steps==1025 && g.context==ls);
 mem.store(0x10210,4,7);assert(sc(158,0x10210)==0x80010002);
 r[3]=0x10210;
''' + close_import() + r'''
 assert(r[3]==UINT64_C(0xffffffff80010002));std::filesystem::remove_all(dir);
''', spu_images=(spu,))

    def test_mmapper_page_sizes_alias_permissions_and_unmap(self):
        self.native(r'''
 assert(sc(330,0x10000000,0x200,0x10000000,0x10000)==0);
 const auto area=mem.load(0x10000,4);assert(area==0x30000000);
 for(unsigned flags:{0x8200u,0x8400u}){
     assert(sc(332,flags,0x100000,flags,0x10004)==0);const auto id=mem.load(0x10004,4);
     assert(sc(334,area,id,0x40000)==0 && sc(334,area+0x200000,id,0x80000)==0);
     mem.store(area+0xffffc,4,0x12345678);assert(mem.load(area+0x2ffffc,4)==0x12345678);
     for(unsigned offset:{0u,0xfffffu}){
         assert(sc(351,area+offset,0x10100)==0);
         assert(mem.load(0x10100,8)==0x40000 && mem.load(0x10108,8)==15);
         assert(mem.load(0x10110,4)==(flags==0x8200?0x10000:0x100000) && mem.load(0x10114,4)==0);
         assert(sc(351,area+0x200000+offset,0x10100)==0 && mem.load(0x10100,8)==0x80000);
         const std::array<std::uint8_t,8> readonly={0,0,0,0,0,8,0,0};
         assert(std::equal(readonly.begin(),readonly.end(),mem.access(0x10100,8,false)));
     }
     assert(sc(351,area+0x100000,0x10100)==0x80010002);
     assert(sc(351,area,0)==0x8001000d);
     assert(sc(335,area,0x10008)==0 && mem.load(0x10008,4)==id);
     assert(sc(351,area,0x10100)==0x80010002);
     assert(sc(351,area+0x200000,0x10100)==0);
     assert(sc(335,area+0x200000,0x10008)==0 && s.mmapper.page_metadata.empty());
     assert(sc(329,id)==0);
 }
 assert(sc(331,area)==0);
''')

    def test_explicit_page_metadata_hooks_free_reuse_and_stack(self):
        self.native(r'''
 assert(sc(348,0x100000,0x200,0x10000)==0);const auto base=mem.load(0x10000,4);
 // sys_memory_allocate (348) records the requested page size; free (349) forgets it.
 assert(sc(351,base+0xfffff,0x10100)==0 && mem.load(0x10110,4)==0x10000);
 const auto old=import_region(s,base)->bytes;(void)old;
 assert(sc(349,base)==0 && sc(351,base,0x10100)==0x80010002);
 assert(sc(348,0x100000,0x400,0x10000)==0 && mem.load(0x10000,4)==base);
 assert(sc(351,base+0xfffff,0x10100)==0 && mem.load(0x10110,4)==0x100000);   // new region, new metadata
 fault([&]{mmapper_record_pages(s,base,0x2000);});
 fault([&]{mmapper_record_pages(s,0xdead0000,0x10000);});
 // Loader-like RX mapping, deliberately not aligned to the page size.
 mem.map(0x234560,32,5,nullptr,0);mmapper_record_pages(s,0x234560,0x10000);
 assert(sc(351,0x23457f,0x10100)==0 && mem.load(0x10100,8)==0x80000 && mem.load(0x10110,4)==0x10000);
 assert(sc(351,0x234580,0x10100)==0x80010002);
 mem.map(0xd0000000,0x4000,6,nullptr,0);
 assert(sc(351,0xd0003fff,0x10100)==0);
 assert(mem.load(0x10100,8)==0x40000 && mem.load(0x10108,8)==8 && mem.load(0x10110,4)==4096 && mem.load(0x10114,4)==0);
''')


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class MemoryAllocatePageMetadataTests(unittest.TestCase):
    def test_allocate_records_page_size_for_351_and_free_forgets_it(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 assert(call(348,0x100000,0x400,0x10000)==0);const auto big=mem.load(0x10000,4);
 assert(call(348,0x20000,0x200,0x10004)==0);const auto small=mem.load(0x10004,4);
 assert(call(351,big+0x123,0x10100)==0 && mem.load(0x10110,4)==0x100000);
 assert(call(351,small+0x10,0x10100)==0 && mem.load(0x10110,4)==0x10000);
 assert(call(349,big)==0);assert(call(351,big+0x123,0x10100)==0x80010002);
''')
