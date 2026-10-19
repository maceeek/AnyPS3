"""Native import ABI regressions with fixed bytes/errors and shared state."""
import shutil
import unittest

from anyps3.elf import ElfImage, Segment
from anyps3.services import IMPORTS
from tests import test_runtime_objects


def invoke(nid, args='', library='sysPrxForUser'):
    return args + IMPORTS[(library, nid)][1].replace('state', 's') + '\n'


SPU_IMPORT = r'''
 const auto src=0x10100u,out=0x10000u;
 mem.store(src,4,0x7f454c46);mem.store(src+4,4,0x01020100);
 mem.store(src+16,4,0x00020017);mem.store(src+20,4,1);
 mem.store(src+24,4,0x100);mem.store(src+28,4,52);
 mem.store(src+40,2,52);mem.store(src+42,2,32);mem.store(src+44,2,1);
 const auto ph=src+52;
 mem.store(ph,4,1);mem.store(ph+4,4,0x100);mem.store(ph+8,4,0x100);
 mem.store(ph+16,4,16);mem.store(ph+20,4,32);
 mem.store(src+0x100,4,0x32000000); // Original synthetic BR-self instruction.
 r[3]=out;r[4]=src;r[5]=1;guest_spu_image_import(s);assert(r[3]==0);
 const auto table=mem.load(out+8,4);
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class ImportP1Tests(unittest.TestCase):
    def run_native(self, body, **kwargs):
        test_runtime_objects.RuntimeObjectsTests().run_native(body, **kwargs)

    def test_nids_and_remaining_undocumented_heap_imports(self):
        expected = {
            '0xaeb78725': 'sys_lwmutex_trylock', '0x3dd4a957': 'sys_ppu_thread_register_atexit',
            '0xac6fc404': 'sys_ppu_thread_unregister_atexit', '0xbdb18f83': '_sys_malloc',
            '0xf7f7fb20': '_sys_free', '0xe0da8efd': 'sys_spu_image_close',
            '0xb2fcf2c8': '_sys_heap_create_heap', '0xaede4b03': '_sys_heap_delete_heap',
            '0x35168520': '_sys_heap_malloc', '0x44265c08': '_sys_heap_memalign', '0x8a561d92': '_sys_heap_free',
        }
        for nid, name in expected.items():
            self.assertEqual(IMPORTS[('sysPrxForUser', nid)][0], name)
        for nid in ('0xb9bf1078', '0xb6369393', '0xd1ad4570', '0x8985b5b6'):
            self.assertNotIn(('sysPrxForUser', nid), IMPORTS)
        for nid, name in (('0x112a5ee9', 'cellSysmoduleUnloadModule'), ('0x96c07adf', 'cellSysmoduleFinalize')):
            self.assertEqual(IMPORTS[('cellSysmodule', nid)][0], name)

    def test_trylock_be_shared_lock_state_and_recursion_boundary(self):
        self.run_native(r'''
 (void)call;mem.store(0x10200,4,2);mem.store(0x10204,4,16);
 r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);
''' + invoke('0xaeb78725', 'r[3]=0x10100;') + r'''
 assert(r[3]==0);const auto* bytes=mem.access(0x10100,24,false);
 assert(bytes[0]==0 && bytes[1]==0 && bytes[2]==0 && bytes[3]==1);
 assert(bytes[8]==0 && bytes[9]==0 && bytes[10]==0 && bytes[11]==0x12);
 assert(bytes[16]==0x95 && bytes[17]==0 && bytes[18]==0 && bytes[19]==0);
 r[3]=0x10100;r[4]=0;lwmutex_lock(s);assert(mem.load(0x1010c,4)==1);
 r[3]=0x10100;assert(lwmutex_trylock(s)==0 && mem.load(0x1010c,4)==2);
 r[3]=0x10100;lwmutex_unlock(s);assert(mem.load(0x1010c,4)==1);
 r[3]=0x10100;lwmutex_unlock(s);r[3]=0x10100;lwmutex_unlock(s);
 assert(mem.load(0x10100,4)==0xffffffff && mem.load(0x1010c,4)==0);
 r[3]=0x10100;assert(lwmutex_trylock(s)==0);
 mem.store(0x1010c,4,0xfffffffe);assert(lwmutex_trylock(s)==0);
 assert(mem.load(0x1010c,4)==0xffffffff);const auto version=mem.reservation_token(0x10100);
''' + invoke('0xaeb78725', 'r[3]=0x10100;') + r'''
 assert(r[3]==UINT64_C(0xffffffff80010011));assert(mem.reservation_token(0x10100)==version);
''')

    def test_trylock_busy_dead_attributes_and_reserved_handoff(self):
        self.run_native(r'''
 (void)call;mem.store(0x10200,4,1);mem.store(0x10204,4,32);
 r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);r[3]=0x10100;r[4]=0;lwmutex_lock(s);r[3]=0x10100;
 assert(lwmutex_trylock(s)==0x80010008 && mem.load(0x1010c,4)==0);
 s.thread_id=2;const auto token=mem.reservation_token(0x10100);
 const auto bytes=*import_region(s,0x10000)->bytes;
 r[3]=UINT64_C(0xabcdef0100010100);r[4]=123;
''' + invoke('0xaeb78725') + r'''
 assert(r[3]==UINT64_C(0xffffffff8001000a));assert(mem.reservation_token(0x10100)==token);
 assert(*import_region(s,0x10000)->bytes==bytes && !s.wait_kind && !s.reschedule && !s.lwmutex_waiting);
 s.thread_id=1;r[3]=0x10100;lwmutex_unlock(s);r[3]=0x10100;assert(lwmutex_destroy(s)==0);
 r[3]=0x10100;assert(lwmutex_trylock(s)==0x80010002);
 r[3]=0x10140;fault([&]{lwmutex_trylock(s);});
 r[3]=0x10100;r[4]=0x10200;lwmutex_create(s);r[3]=0x10100;
 mem.store(0x10100,4,0xfffffffd);const auto held=mem.reservation_token(0x10100);
 fault([&]{lwmutex_trylock(s);});assert(mem.reservation_token(0x10100)==held);
 mem.store(0x10100,4,0xffffffff);mem.store(0x10108,4,0);fault([&]{lwmutex_trylock(s);});
 assert(mem.load(0x10100,4)==0xffffffff);
''')

    def test_atexit_eight_opds_shared_across_created_threads(self):
        self.run_native(r'''
 (void)call;
 mem.store(0x10100,4,0x10500);mem.store(0x10104,4,0x10800);
 s.tls_memory_size=16;r[3]=0x10000;r[4]=0x10100;r[5]=0;r[6]=1000;r[7]=0x4000;r[8]=1;r[9]=0;
 ppu_thread_create(s);assert(s.ppu_threads.back().thread_atexit==s.thread_atexit);
 assert(mem.load(0x10100,8)==UINT64_C(0x0001050000010800));
''' + invoke('0x3dd4a957', 'r[3]=UINT64_C(0xfeedface00010100);') + r'''
 assert(r[3]==0 && (*s.thread_atexit)[0]==0x10100);
''' + invoke('0x3dd4a957', 'r[3]=0x10100;') + r'''
 assert(r[3]==UINT64_C(0xffffffff80010009));
 for(unsigned i=1;i<8;++i){r[3]=0x10100+8*i;assert(thread_atexit_service(s,false)==0);}
 r[3]=0x10180;assert(thread_atexit_service(s,false)==0x80010004);
 r[3]=0;assert(thread_atexit_service(s,true)==0x80010005);
 ppu_schedule(s);assert(s.thread_id==2 && (*s.thread_atexit)[7]==0x10138);
''' + invoke('0xac6fc404', 'r[3]=0x10118;') + r'''
 assert(r[3]==0 && (*s.thread_atexit)[3]==0);
 r[3]=0;assert(thread_atexit_service(s,false)==0x80010009 && thread_atexit_service(s,true)==0);
 r[3]=0x10118;assert(thread_atexit_service(s,true)==0x80010005);
 r[3]=0x10180;assert(thread_atexit_service(s,false)==0 && (*s.thread_atexit)[3]==0x10180);
 assert(mem.load(0x10100,8)==UINT64_C(0x0001050000010800));
 PpuState other;assert(other.thread_atexit!=s.thread_atexit && (*other.thread_atexit)[0]==0);
''')

    def test_registered_atexit_runs_through_wrapper_before_releasing_stack_tls(self):
        # Registration (thread_atexit_service) feeds the liblv2-wrapper exit; raw exit ignores it.
        self.run_native(r'''
 (void)call;mem.store(0x10100,4,0x10500);mem.store(0x10104,4,0x10800);
 s.tls_memory_size=16;r[3]=0x10000;r[4]=0x10100;r[5]=0;r[6]=1000;r[7]=0x4000;r[8]=1;r[9]=0;
 ppu_thread_create(s);r[3]=0x10100;assert(thread_atexit_service(s,false)==0);
 ppu_schedule(s);assert(s.thread_id==2);
 const auto allocated=mem.allocated,stack=s.stack_base,tls=s.tls_base;
 r[3]=42;bool blocked=false;try{ppu_thread_exit_wrapper(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && !s.returned && s.pc==0x10500 && r[2]==0x10800 && s.lr==0xfffe0010);
 assert(mem.allocated==allocated && s.stack_base==stack && s.tls_base==tls);
 mem.access(stack,4,false);mem.access(tls,4,false);
 r[3]=1;blocked=false;try{ppu_atexit_continue(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.returned && s.exit_status==42);
''')

    def test_exit_waits_while_another_thread_runs_atexit_callbacks(self):
        # sysPrxForUser sys_ppu_thread_exit holds a process lwmutex around the callbacks (RPCS3 ffba2bf0
        # Modules/sys_ppu_thread_.cpp:168-190): a second exiting thread waits, then exits normally.
        # Run 375load stopped here when two game threads exited at once.
        self.run_native(r'''
 (void)call;mem.store(0x10100,4,0x10500);mem.store(0x10104,4,0x10800);
 s.tls_memory_size=16;r[3]=0x10000;r[4]=0x10100;r[5]=0;r[6]=1000;r[7]=0x4000;r[8]=1;r[9]=0;
 ppu_thread_create(s);ppu_schedule(s);assert(s.thread_id==2 && s.pc==0x10500);
 for(auto& t:s.ppu_threads)if(t.thread_id==1){t.wait_kind=6;t.wait_id=2;} // main joins thread 2
 PpuContext holder;holder.thread_id=9;holder.atexit_running=true;s.ppu_threads.push_back(holder);
 const auto pc=s.pc;const auto stack=s.stack_base;
 r[3]=42;bool blocked=false;try{ppu_thread_exit_wrapper(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==13 && s.wait_id==9 && !s.atexit_running && !s.returned);
 assert(s.pc==pc && r[3]==42 && s.stack_base==stack);mem.access(stack,4,false);
 for(auto& t:s.ppu_threads)if(t.thread_id==9){t.atexit_running=false;t.returned=true;}
 ppu_schedule(s);assert(s.thread_id==2 && s.wait_kind==0);
 blocked=false;try{ppu_thread_exit_wrapper(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.returned && s.exit_status==42 && !s.atexit_running);
''')

    def test_malloc_pages_shared_accounting_and_strict_free_ownership(self):
        self.run_native(r'''
 const auto initial=main_memory_used(s);
 s.mmapper.areas.push_back({0x10000000,0x10000000,0x10000000});
 assert(call(348,0x10000,0x200,0x10000)==0);const auto user=mem.load(0x10000,4);
 assert(user==0x20000000);
''' + invoke('0xbdb18f83', 'r[3]=UINT64_C(0x1111111100000001);') + r'''
 const auto a=r[3];assert(a==0x20010000 && main_memory_used(s)==initial+0x20000);
 const auto b=guest_malloc(s,0x10001);assert(b==0x20020000 && main_memory_used(s)==initial+0x40000);
 assert(mem.load(a,8)==0);mem.store(a,8,UINT64_C(0x0102030405060708));
 assert(mem.load(b,8)==0);const auto* bytes=mem.access(a,8,false);for(unsigned i=0;i<8;++i)assert(bytes[i]==i+1);
 assert(call(349,a)==0x80010002);fault([&]{guest_free(s,user);});fault([&]{guest_free(s,a+1);});
 fault([&]{guest_free(s,0);});fault([&]{guest_free(s,0x10000);});
 mem.regions.push_back({0x40000000,6,import_region(s,a)->bytes});
 fault([&]{guest_free(s,a);});assert(mem.load(a,8)==UINT64_C(0x0102030405060708));
 mem.regions.pop_back();
 const auto token=mem.reservation_token(a);auto backing=import_region(s,a)->bytes;
''' + invoke('0xf7f7fb20', 'r[3]=UINT64_C(0xffffffff20010000);') + r'''
 assert(r[3]==UINT64_C(0xffffffff20010000)); // void ABI, no invented return code.
 assert(main_memory_used(s)==initial+0x30000);fault([&]{mem.load(a,1);});fault([&]{guest_free(s,a);});
 assert(mem.write_versions[token.first/128]>token.second);
 assert(mem.load(b,8)==0 && mem.load(user,8)==0);
 guest_free(s,b);assert(call(349,user)==0 && main_memory_used(s)==initial);
 const auto fresh=guest_malloc(s,1);assert(fresh==0x20000000);guest_free(s,fresh);
''')

    def test_malloc_exhaustion_and_process_lifetime_do_not_alias(self):
        self.run_native(r'''
 (void)call;const auto initial=main_memory_used(s),regions=mem.regions.size();
 assert(guest_malloc(s,0)==0 && guest_malloc(s,0xffffffff)==0 && mem.regions.size()==regions);
 s.mmapper.container_reserved=0x10000000-initial-0x10000;
 const auto a=guest_malloc(s,1);assert(a==0x10000000 && main_memory_used(s)==0x10000000);
 assert(guest_malloc(s,1)==0 && guest_malloc(s,0x10001)==0);
 guest_free(s,a);assert(main_memory_used(s)==0xfff0000);
 s.mmapper.container_reserved=0;
 PpuState other;const auto b=guest_malloc(other,1);assert(b==0x10000000);
 mem.map(b,0x10000,6,nullptr,0);fault([&]{guest_free(s,b);});guest_free(other,b);
 {PpuState temporary;assert(guest_malloc(temporary,1)==0x10000000);}
 PpuState later;later.memory.map(0x10000000,0x10000,6,nullptr,0);
 fault([&]{guest_free(later,0x10000000);});
 const auto c=guest_malloc(later,1);assert(c==0x10010000);guest_free(later,c);
 s.mmapper.areas.push_back({0x10000000,0xb0000000,0x10000000});
 assert(guest_malloc(s,1)==0);
''')

    def test_spu_close_preserves_initialized_segments_running_ls_and_source(self):
        payload=(0x32000000).to_bytes(4, 'big') + bytes(12)
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,16,32,16),))
        self.run_native('(void)call;' + SPU_IMPORT + r'''
 assert(mem.load(out,8)==0x100 && mem.load(out+12,4)==2);
 const auto* desc=mem.access(table,48,false);
 assert(desc[0]==0 && desc[3]==1 && desc[6]==1 && desc[7]==0 && desc[11]==16);
 assert(desc[24]==0 && desc[27]==2 && desc[30]==1 && desc[31]==16 && desc[35]==16);
 const auto allocation=mem.allocated;
 s.spu_initialized=true;s.spu_group_limit=6;
 NativeSpu::Group group;group.id=0x4000100;group.count=1;group.context.resize(0x40000);s.spu.groups.push_back(std::move(group));
 r[11]=172;r[3]=0x10300;r[4]=0x4000100;r[5]=0;r[6]=out;r[7]=0x10320;r[8]=0x10340;
 assert(spu_syscall(s)==0);const auto segments=s.spu.groups[0].threads[0].segments;
 r[11]=173;r[3]=0x4000100;assert(spu_syscall(s)==0);
 const auto steps=s.spu.groups[0].threads[0].steps;assert(steps>0);
''' + invoke('0xe0da8efd', 'r[3]=out;') + r'''
 assert(r[3]==0 && mem.allocated==allocation-48);fault([&]{mem.load(table,1);});
 assert(mem.load(out+8,4)==table && mem.load(src+0x100,4)==0x32000000);
 assert(s.spu.groups[0].threads[0].segments.size()==segments.size());
 assert(s.spu.groups[0].context[0x100]==0x32 && s.spu.groups[0].context[0x110]==0);
 assert(spu_run(s,1) && s.spu.groups[0].threads[0].steps==steps+1);
 r[3]=out;fault([&]{guest_spu_image_close(s);});
''', spu_images=(image,))

    def test_spu_close_rejects_foreign_types_mutation_and_failed_import(self):
        self.run_native('(void)call;' + SPU_IMPORT + r'''
 const auto used=mem.allocated;r[3]=out;r[4]=src;r[5]=1;
 fault([&]{guest_spu_image_import(s);});assert(mem.allocated==used);
 r[3]=out;mem.store(out,4,2);assert(guest_spu_image_close(s)==0x80010002);
 mem.store(out,4,1);assert(guest_spu_image_close(s)==0x80010005);
 mem.store(out,4,0);mem.store(out+12,4,1);fault([&]{guest_spu_image_close(s);});
 mem.store(out+12,4,2);mem.store(out+4,4,0x104);fault([&]{guest_spu_image_close(s);});
 mem.store(out+4,4,0x100);fault([&]{guest_free(s,table);});assert(mem.allocated==used);
 mem.store(out+8,4,guest_malloc(s,1));const auto foreign=mem.load(out+8,4);
 fault([&]{guest_spu_image_close(s);});guest_free(s,foreign);mem.store(out+8,4,table);
 assert(guest_spu_image_close(s)==0);
 r[3]=0x10400;r[4]=src;r[5]=1;mem.store(ph+20,4,8);guest_spu_image_import(s);
 assert(r[3]==0x80010007 && mem.load(0x10400,4)==0);
 r[3]=0x10400;fault([&]{guest_spu_image_close(s);});
 r[3]=0;r[4]=0;r[5]=2;guest_spu_image_import(s);assert(r[3]==0x80010002);
 r[3]=0;r[5]=0;fault([&]{guest_spu_image_import(s);});
''')

    def test_sysmodule_finalize_preserves_state_unload_stops_without_writes(self):
        modules=({'name':'test','module_name':'Test','ids':(7,),'exports':()},)
        self.run_native(r'''
 (void)call;s.loaded_modules.insert(7);s.initialized_residents.insert("resident");
 s.module_loading=true;s.module_id=7;r[3]=0x12345678;
''' + invoke('0x96c07adf', library='cellSysmodule') + r'''
 assert(r[3]==0 && s.loaded_modules.count(7) && s.module_loading && s.module_id==7);
 assert(s.initialized_residents.count("resident"));s.module_loading=false;
 for(auto id:{7u,0x10007u,0u,0x58u,0xffffu}){
   r[3]=id;fault([&]{native_module_unload_unsupported(s);});
   assert(r[3]==id && s.loaded_modules.size()==1 && s.loaded_modules.count(7));
 }
 mem.store(0x10100,4,0x54657374);mem.store(0x10104,1,0);
 r[3]=0x10100;r[4]=r[5]=0;native_module_by_name(s);assert(r[3]==0x23000000);
''', native_modules=modules)


if __name__ == '__main__':
    unittest.main()
