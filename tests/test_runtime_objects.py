"""Observable behavior of the emitted native IPC runtime, including failures."""
from pathlib import Path
from dataclasses import replace
import shutil
import subprocess
import tempfile
import unittest
from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program

@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class RuntimeObjectsTests(unittest.TestCase):
    def test_ppu_exit_releases_stack_tls_and_wakes_joiner(self):
        self.run_native(r'''
 mem.store(0x10100,4,0x10500);mem.store(0x10104,4,0x10800);
 s.tls_address=0x10600;s.tls_file_size=0;s.tls_memory_size=16;
 const auto initial=mem.allocated;
 r[3]=0x10000;r[4]=0x10100;r[5]=99;r[6]=1000;r[7]=0x4000;r[8]=1;r[9]=0;
 ppu_thread_create(s);assert(mem.load(0x10000,8)==2 && mem.allocated>initial);
 const auto stack=s.ppu_threads.back().stack_base,tls=s.ppu_threads.back().tls_base;
 assert(call(44,1,0x10008)==0x80010008 && call(44,99,0x10008)==MM_ESRCH);
 mem.store(0x10008,8,0xdeadbeef);
 bool blocked=false;try{call(44,2,0x10008);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && mem.load(0x10008,8)==0xdeadbeef);
 ppu_schedule(s,true);assert(s.thread_id==2);
 r[3]=UINT64_C(0xfedcba9876543210);blocked=false;
 try{ppu_thread_exit(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.returned && mem.allocated==initial);
 fault([&]{mem.load(stack,4);});fault([&]{mem.load(tls,4);});
 ppu_schedule(s,true);assert(s.thread_id==1 && !s.wait_kind && !s.returned);
 assert(call(44,2,0x10008)==0 && mem.load(0x10008,8)==UINT64_C(0xfedcba9876543210));
 assert(call(44,2,0x10008)==MM_ESRCH && s.ppu_threads.size()==1);
 r[3]=0x10000;r[4]=0x10100;r[5]=0;r[6]=1000;r[7]=0x4000;r[8]=0;r[9]=0;
 ppu_thread_create(s);assert(call(44,3,0x10008)==MM_EINVAL);
''')

    def test_memory_containers_reserve_budget_report_usage_and_release(self):
        self.run_native(r'''
 const auto initial=main_memory_used(s);
 assert(call(324,0x10000,0xfffff)==MM_ENOMEM && s.mmapper.containers.empty());
 assert(call(324,0x10000,0x300fff)==0);const auto id=mem.load(0x10000,4);
 assert(main_memory_used(s)==initial+0x300000 && s.mmapper.container_reserved==0x300000);
 assert(call(343,0x10010,id)==0 && mem.load(0x10010,4)==0x300000 && mem.load(0x10014,4)==0x300000);
 assert(call(352,0x10020)==0 && mem.load(0x10020,4)==0xfd00000 && mem.load(0x10024,4)==0xfd00000-initial);
 assert(call(348,0xfe00000,0x400,0x10030)==MM_ENOMEM);
 assert(call(324,0x10030,0xfe00000)==MM_ENOMEM && s.mmapper.containers.size()==1);
 s.mmapper.containers[0].used=0x100000;
 assert(call(325,id)==MM_EBUSY && call(343,0x10010,id)==0 && mem.load(0x10014,4)==0x200000);
 s.mmapper.containers[0].used=0;
 assert(call(325,id)==0 && main_memory_used(s)==initial && s.mmapper.container_reserved==0);
 assert(call(325,id)==MM_ESRCH && call(343,0x10010,id)==MM_ESRCH);
''')

    def run_native(self, body, *, spu_images=(), spu_overlays=(), native_modules=()):
        translated=compile_program(ElfImage.parse(program(BLR)),process_entry=True,native_modules=native_modules)
        source=replace(translated,spu_images=spu_images,spu_overlays=spu_overlays).cpp()
        source=source.replace('int main(int argc, char** argv)','int original_main(int argc, char** argv)')
        source+=r'''
#include <cassert>
template<class F> void fault(F f) {bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}
int main() {
 PpuState s; auto& r=s.r; auto& mem=s.memory; mem.map(0x10000,0x2000,6,nullptr,0);
 auto call=[&](unsigned number,std::uint64_t a=0,std::uint64_t b=0,std::uint64_t c=0,std::uint64_t d=0){r[11]=number;r[3]=a;r[4]=b;r[5]=c;r[6]=d;syscall(s);return r[3];};
'''+body+'\n}\n'
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'test.cpp').write_text(source)
            build=subprocess.run(['c++','-std=c++17','-O1','-Wall','-Wextra','-Werror',str(p/'test.cpp'),'-o',str(p/'test')],capture_output=True,text=True,timeout=300)
            self.assertEqual(build.returncode,0,build.stderr)
            run=subprocess.run([str(p/'test')],capture_output=True,text=True,timeout=5)
            self.assertEqual(run.returncode,0,run.stderr)

    def test_module_name_lookup_respects_identity_and_loaded_state(self):
        modules=({'name':'file','module_name':'RealName','ids':(7,),'exports':()},
                 {'name':'resident','module_name':'Resident','ids':(),'exports':(),'resident':True})
        self.run_native(r'''
 (void)call;const char name[]="RealName";std::copy_n(name,sizeof(name),mem.access(0x10100,sizeof(name),true));
 r[3]=0x10100;native_module_by_name(s);assert(r[3]==sign_word(0x8001112e));
 s.loaded_modules.insert(7);r[3]=0x10100;native_module_by_name(s);assert(r[3]==0x23000000);
 const char resident[]="Resident";std::copy_n(resident,sizeof(resident),mem.access(0x10100,sizeof(resident),true));
 r[3]=0x10100;native_module_by_name(s);assert(r[3]==0x23000001);
 r[3]=0x10100;r[4]=1;native_module_by_name(s);assert(r[3]==sign_word(MM_EINVAL));
''',native_modules=modules)

    def test_spu_group_join_waits_for_completion_and_excludes_second_waiter(self):
        self.run_native(r'''
 NativeSpu::Group g;g.id=0x4000100;g.state=6;s.spu.groups.push_back(g);
 mem.store(0x10100,4,0xdeadbeef);mem.store(0x10104,4,0xcafebabe);
 bool blocked=false;try{call(178,g.id,0x10100,0x10104);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==5 && mem.load(0x10100,4)==0xdeadbeef);
 s.thread_id=2;assert(call(178,g.id,0x10100,0x10104)==MM_EBUSY);
 s.thread_id=1;s.wait_kind=0;s.spu.groups[0].state=1;s.spu.groups[0].join_cause=1;s.spu.groups[0].exit_status=123;
 assert(call(178,g.id,0x10100,0x10104)==0 && mem.load(0x10100,4)==1 && mem.load(0x10104,4)==123);
 assert(s.spu.groups[0].joiner==0 && s.spu.groups[0].join_cause==0);
 assert(call(178,0x9999,0x10100,0x10104)==MM_ESRCH);
''')

    def test_spu_image_segments_and_rejection_are_transactional(self):
        self.run_native(r'''
 (void)call;
 const auto src=0x10100u,out=0x10000u;
 mem.store(src,4,0x7f454c46);mem.store(src+4,4,0x01020100);
 mem.store(src+16,4,0x00020017);mem.store(src+20,4,1);
 mem.store(src+24,4,0x100);mem.store(src+28,4,52);
 mem.store(src+40,2,52);mem.store(src+42,2,32);mem.store(src+44,2,2);
 const auto ph=src+52;
 mem.store(ph,4,1);mem.store(ph+4,4,0x100);mem.store(ph+8,4,0x100);
 mem.store(ph+16,4,16);mem.store(ph+20,4,32);
 mem.store(ph+32,4,4);mem.store(ph+36,4,0x120);mem.store(ph+48,4,0x34);
 r[3]=out;r[4]=src;r[5]=1;spu_image_import(s);assert(r[3]==0);
 assert(mem.load(out,4)==0 && mem.load(out+4,4)==0x100 && mem.load(out+12,4)==3);
 const auto table=mem.load(out+8,4);
 assert(mem.load(table,4)==1 && mem.load(table+4,4)==0x100);
 assert(mem.load(table+8,4)==16 && mem.load(table+16,4)==src+0x100);
 assert(mem.load(table+24,4)==2 && mem.load(table+28,4)==0x110);
 assert(mem.load(table+32,4)==16 && mem.load(table+40,4)==0);
 assert(mem.load(table+48,4)==4 && mem.load(table+56,4)==32);
 assert(mem.load(table+64,4)==src+0x134);
 const auto allocated=mem.allocated;mem.store(out,4,0xdeadbeef);
 mem.store(ph+20,4,8);r[3]=out;spu_image_import(s);
 assert(r[3]==0x80010007 && mem.load(out,4)==0xdeadbeef && mem.allocated==allocated);
 r[3]=out;r[5]=2;spu_image_import(s);assert(r[3]==MM_EINVAL);
 r[3]=out;r[5]=0;fault([&]{spu_image_import(s);});
''')

    def test_semaphore_counts_and_spu_reservation_eligibility(self):
        self.run_native(r'''
 mem.store(0x10100,4,2);mem.store(0x10104,4,0x200);
 assert(call(90,0x10000,0x10100,2,1)==MM_EINVAL);
 assert(call(90,0x10000,0x10100,0,2)==0);const auto id=mem.load(0x10000,4);
 assert(call(93,id)==MM_EBUSY);fault([&]{call(92,id);});
 assert(call(94,id,2)==0 && call(94,id,1)==MM_EBUSY);
 assert(call(95,id,0x10004)==0 && mem.load(0x10004,4)==2);
 assert(call(92,id)==0 && call(93,id)==0 && call(93,id)==MM_EBUSY);
 assert(call(94,id,0)==MM_EINVAL && call(94,id,UINT64_MAX)==MM_EINVAL);
 assert(call(91,id)==0 && call(95,id,0x10004)==MM_ESRCH);
 assert(call(14,0x10100,2)==0 && call(14,0x10100,6)==0);
 assert(call(14,0x10100,1)==MM_EINVAL && call(14,0xdead0000,2)==MM_EPERM);
 assert(call(14,0x20000,2)==MM_EINVAL);
''')

    def test_ppu_context_switch_and_real_event_wakeup(self):
        self.run_native(r'''
 mem.store(0x10100,4,0x10500);mem.store(0x10104,4,0x10800);
 s.tls_address=0x10600;s.tls_file_size=4;s.tls_memory_size=16;
 mem.store(0x10600,4,0xcafebabe);
 s.pc=0x10400;s.r[20]=0x1234;s.fpr[4]=0x4321;s.vr[5][7]=0xab;s.cr=0x76543210;
 r[3]=0x10000;r[4]=0x10100;r[5]=99;r[6]=10;r[7]=0x4000;r[8]=1;r[9]=0;
 ppu_thread_create(s);assert(r[3]==0 && mem.load(0x10000,8)==2);
 ppu_schedule(s);assert(s.thread_id==2 && s.pc==0x10500 && r[3]==99 && r[2]==0x10800);
 assert(call(48,2,0x10008)==0 && mem.load(0x10008,4)==10);
 assert(call(48,1,0x10008)==0 && mem.load(0x10008,4)==1000);
 assert(call(48,99,0x10008)==MM_ESRCH && mem.load(0x10008,4)==1000);
 assert(call(47,2,UINT32_MAX)==MM_EINVAL && s.priority==10);
 assert(mem.load(r[13]-0x7000,4)==0xcafebabe && mem.load(r[13]-0x6ffc,8)==0);
 r[20]=0x9876;s.fpr[4]=0x6789;s.vr[5][7]=0xcd;s.cr=0xabcdef01;
 mem.store(0x10200,4,2);mem.store(0x10204,4,1);
 assert(call(128,0x10008,0x10200,0,4)==0);const auto queue=mem.load(0x10008,4);
 bool blocked=false;try{call(130,queue);}catch(const PpuBlocked&){blocked=true;}assert(blocked);
 ppu_schedule(s,true);assert(s.thread_id==1 && s.pc==0x10400 && r[20]==0x1234);
 assert(s.fpr[4]==0x4321 && s.vr[5][7]==0xab && s.cr==0x76543210);
 assert(call(134,0x1000c,1,88)==0);const auto port=mem.load(0x1000c,4);
 assert(call(136,port,queue)==0 && call(138,port,10,20,30)==0);
 ppu_schedule(s);assert(s.thread_id==2 && r[20]==0x9876);
 assert(s.fpr[4]==0x6789 && s.vr[5][7]==0xcd && s.cr==0xabcdef01);
 assert(call(130,queue)==0 && r[4]==88 && r[5]==10 && r[6]==20 && r[7]==30);
 assert(call(47,1,5)==0 && s.reschedule);ppu_schedule(s);
 assert(s.thread_id==1 && s.priority==5 && r[20]==0x1234);
''')

    def test_exclusive_spu_groups_reserve_slots_without_main_save_memory(self):
        self.run_native(r'''
 const auto before=mem.allocated;mem.store(0x10108,4,0x18);
 assert(call(170,0x10000,6,0,0x10100)==MM_EBUSY);
 assert(call(170,0x10000,5,0,0x10100)==0);const auto exclusive=mem.load(0x10000,4);
 assert(mem.allocated==before && s.spu.groups[0].context.size()==5*0x40000 && s.spu.groups[0].type==0x18);
 assert(call(170,0x10004,1,0,0x10100)==MM_EBUSY && s.spu.groups.size()==1);
 assert(call(169,6,1)==MM_EBUSY && s.spu_group_limit==6);
 mem.store(0x10108,4,0);assert(call(170,0x10004,1,100,0x10100)==0);const auto normal=mem.load(0x10004,4);
 assert(mem.allocated==before+0x40000);
 assert(call(171,exclusive)==0 && mem.allocated==before+0x40000);
 assert(call(171,normal)==0 && mem.allocated==before);
 assert(call(169,6,1)==0 && s.spu_group_limit==5);
 mem.store(0x10108,4,0x18);assert(call(170,0x10000,4,0,0x10100)==0);
 assert(call(171,mem.load(0x10000,4))==0 && mem.allocated==before);
''')

    def test_spu_group_initialization_and_event_port_lifetime(self):
        self.run_native(r'''
 const auto before=mem.allocated;
 assert(call(170,0x10000,1,99,0x10100)==0);const auto group=mem.load(0x10000,4);
 assert(mem.allocated==before+0x40000 && call(173,group)==0x8001000f);
 mem.store(0x10204,4,0x100);mem.store(0x10208,4,0x10300);mem.store(0x1020c,4,1);
 mem.store(0x10300,4,1);mem.store(0x10304,4,0x100);
 mem.store(0x10308,4,16);mem.store(0x10310,4,0x10600);
 mem.store(0x10408,4,2);mem.store(0x10500,8,0x12345678);
 r[7]=0x10400;r[8]=0x10500;assert(call(172,0x10004,group,0,0x10200)==0);
 assert(s.spu.groups[0].threads[0].options==2 && s.spu.groups[0].threads[0].arguments[0]==0x12345678);
 assert(call(172,0x10004,group,0,0x10200)==MM_EBUSY);
 mem.store(0x10700,4,2);mem.store(0x10704,4,1);
 assert(call(128,0x10008,0x10700,0,4)==0);const auto queue=mem.load(0x10008,4);
 assert(call(251,group,queue,0,0x1000c)==MM_EINVAL);
 assert(call(251,group,queue,1ull<<63,0x1000c)==0 && mem.load(0x1000c,1)==63);
 assert(call(251,group,queue,1ull<<63,0x1000c)==0x80010015);
 assert(call(252,group,63)==0 && call(251,group,queue,1ull<<63,0x1000c)==0);
 assert(call(185,group,queue,2)==0 && call(185,group,queue,2)==MM_EBUSY);
 assert(call(186,group,2)==0 && call(185,group,queue,2)==0);
 fault([&]{call(173,group);});assert(s.spu.groups[0].state==1);
 assert(call(171,group)==0 && mem.allocated==before && call(171,group)==MM_ESRCH);
''')

    def test_shared_memory_aliases_reservations_and_lifetime(self):
        self.run_native(r'''
 mem.store(0x10000,4,0xdeadbeef);
 assert(call(332,123,0x10000,0x200,0x10000)==MM_ESRCH);
 assert(mem.load(0x10000,4)==0xdeadbeef);
 assert(call(332,123,0x10001,0xc200,0x10000)==MM_EALIGN);
 assert(call(332,123,0x10000,0xc200,0x10000)==0);
 const auto id=mem.load(0x10000,4);
 assert(call(332,123,0x10000,0xc200,0x10000)==MM_EEXIST);
 assert(call(330,0x10000000,0x200,0,0x10004)==0);
 const auto base=mem.load(0x10004,4);
 fault([&]{mem.load(base,4);}); // Reservation does not commit bytes.
 assert(call(334,base+1,id,0x40000)==MM_EALIGN);
 assert(call(334,base,id,0x40000)==0);
 assert(mem.load(base,8)==0);
 assert(call(334,base+0x10000,id,0x80000)==0);
 mem.store(base+12,4,0x12345678);
 assert(mem.load(base+0x1000c,4)==0x12345678); // Actual alias, not a copy.
 fault([&]{mem.store(base+0x1000c,4,0);});
 assert(call(329,id)==MM_EBUSY);
 assert(call(331,base)==MM_EBUSY);
 assert(call(335,base,0x10008)==0 && mem.load(0x10008,4)==id);
 fault([&]{mem.load(base,4);});
 assert(mem.load(base+0x1000c,4)==0x12345678);
 assert(call(335,base+0x10000,0x10008)==0);
 assert(call(329,id)==0 && call(329,id)==MM_ESRCH);
 assert(call(331,base)==0);
 const auto syskey=UINT64_C(0x8006010000000010);
 assert(call(332,syskey,0x100000,0x400,0x10000)==MM_EPERM);
 assert(call(332,syskey,0x10000,0x200,0x10000)==0);
 const auto sid=mem.load(0x10000,4);
 assert(call(329,sid)==0);
 assert(call(332,syskey,0x10000,0x200,0x10000)==0); // System owner retains storage.
''')

    def test_mutex_ownership_recursion_and_condition_lifetime(self):
        self.run_native(r'''
 mem.store(0x10100,4,2);mem.store(0x10104,4,16);mem.store(0x10108,4,0x200);mem.store(0x1010c,4,0x2000);
 assert(call(100,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);
 assert(call(104,id)==MM_EPERM);
 assert(call(102,id)==0 && call(102,id)==0);
 assert(call(101,id)==MM_EBUSY);
 assert(call(104,id)==0 && call(104,id)==0);
 mem.store(0x10200,4,0x200);
 assert(call(105,0x10004,id,0x10200)==0);const auto cond=mem.load(0x10004,4);
 assert(call(101,id)==MM_EPERM);
 assert(call(108,cond)==0); // No waiter, no stored signal.
 assert(call(107,cond)==MM_EPERM);
 assert(call(102,id)==0); fault([&]{call(107,cond);}); assert(call(104,id)==0);
 assert(call(106,cond)==0 && call(106,cond)==MM_ESRCH);
 assert(call(101,id)==0 && call(102,id)==MM_ESRCH);
 mem.store(0x10104,4,32);
 assert(call(100,0x10000,0x10100)==0);const auto plain=mem.load(0x10000,4);
 assert(call(102,plain)==0 && call(102,plain)==0x80010008);
 mem.store(0x10108,4,0x100);mem.store(0x10110,8,123);mem.store(0x10118,4,2);
 assert(call(100,0x10000,0x10100)==MM_ESRCH);
''')

    def test_timed_mutex_lock_times_out_or_acquires(self):
        self.run_native(r'''
 mem.store(0x10100,4,2);mem.store(0x10104,4,32);mem.store(0x10108,4,0x200);mem.store(0x1010c,4,0x2000);
 assert(call(100,0x10000,0x10100)==0);const auto id=mem.load(0x10000,4);
 assert(call(102,id)==0);                         // thread 1 owns it
 s.thread_id=2;bool blocked=false;
 try{call(102,id,5000);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==3 && s.wait_id==id && s.wait_deadline);
 const auto deadline=s.wait_deadline;s.wait_kind=0;blocked=false;
 try{call(102,id,5000);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_deadline==deadline);    // retry keeps the original deadline
 s.wait_kind=0;s.wait_timed_out=true;
 assert(call(102,id,5000)==0x8001000b && !s.wait_deadline && !s.wait_timed_out);   // CELL_ETIMEDOUT
 // Scheduler: an expired waiter is woken with the timeout flag, a live one is not.
 PpuContext waiter;waiter.thread_id=3;waiter.wait_kind=3;waiter.wait_id=id;waiter.wait_deadline=1;
 PpuContext pending=waiter;pending.thread_id=4;pending.wait_deadline=UINT64_MAX;
 s.thread_id=1;s.ppu_threads.clear();s.ppu_threads.push_back(static_cast<PpuContext&>(s));s.ppu_threads.push_back(waiter);s.ppu_threads.push_back(pending);
 ppu_schedule(s);
 bool woke=false,stays=false;
 for(const auto& t:s.ppu_threads){if(t.thread_id==3)woke=!t.wait_kind && t.wait_timed_out;if(t.thread_id==4)stays=t.wait_kind==3 && !t.wait_timed_out;}
 assert(woke && stays);
 // A waiter whose mutex became free acquires instead of timing out.
 s.thread_id=1;assert(call(104,id)==0);s.thread_id=2;s.wait_timed_out=true;s.wait_deadline=1;
 assert(call(102,id,5000)==0 && !s.wait_timed_out && !s.wait_deadline);
''')

    def test_preemption_alternates_two_hardware_threads(self):
        # Cell PPU: two hardware threads run the two best ready LV2 threads concurrently.
        self.run_native(r'''
 (void)call;s.thread_id=1;s.priority=100;
 PpuContext b;b.thread_id=2;b.priority=200;PpuContext c;c.thread_id=3;c.priority=300;
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));s.ppu_threads.push_back(b);s.ppu_threads.push_back(c);
 unsigned seen[4]{};
 for(unsigned i=0;i<8;++i){ppu_schedule(s);++seen[s.thread_id];}
 assert(seen[1]==4 && seen[2]==4 && seen[3]==0);          // 1 and 2 alternate, 3 waits
 for(auto& t:s.ppu_threads)if(t.thread_id==2)t.wait_kind=99;
 if(s.thread_id==2){s.wait_kind=99;ppu_schedule(s,true);}  // a blocked context yields to the best
 for(unsigned i=0;i<6;++i){ppu_schedule(s);++seen[s.thread_id];}
 assert(seen[3]>0 && seen[1]>4);                          // now 1 and 3 share both hardware threads
''')

    def test_event_fifo_capacity_receive_abi_and_disconnect(self):
        self.run_native(r'''
 mem.store(0x10100,4,2);mem.store(0x10104,4,1);
 assert(call(128,0x10000,0x10100,123,2)==0);const auto queue=mem.load(0x10000,4);
 assert(call(128,0x10000,0x10100,123,2)==MM_EEXIST);
 assert(call(134,0x10004,1,42)==0);const auto port=mem.load(0x10004,4);
 assert(call(138,port,1,2,3)==0x80010016);
 assert(call(136,port,queue)==0 && call(136,port,queue)==0x80010015);
 assert(call(138,port,1,2,3)==0 && call(138,port,4,5,6)==0);
 assert(call(138,port,7,8,9)==MM_EBUSY);
 assert(call(130,queue)==0 && r[4]==42 && r[5]==1 && r[6]==2 && r[7]==3);
 assert(call(131,queue,0x10200,2,0x10008)==0 && mem.load(0x10008,4)==1);
 assert(mem.load(0x10200,8)==42 && mem.load(0x10208,8)==4 && mem.load(0x10218,8)==6);
 assert(call(131,queue,0x10200,2,0x10008)==0 && mem.load(0x10008,4)==0);
 fault([&]{call(130,queue);});
 assert(call(135,port)==0x80010015);
 assert(call(137,port)==0 && call(135,port)==0);
 assert(call(129,queue)==0 && call(130,queue)==MM_ESRCH);
''')

    def test_sysutil_connect_registers_queue_and_rejects_other_commands(self):
        self.run_native(r'''
 assert(call(128,0x10000,0x10100,1,2)==MM_EINVAL); // Invalid zero attributes.
 mem.store(0x10100,4,2);mem.store(0x10104,4,1);
 assert(call(128,0x10000,0x10100,UINT64_C(0x8006010000000100),127)==0);
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto request=[&](unsigned channel,bool attributes){
    const auto base=channel*0x8000, data=base+1024, strings=attributes?144u:96u, length=attributes?176u:112u;
    std::fill_n(bytes.begin()+data,length,0);
    put(base+8,length);put(base+12,0);put(base+16,length);put(base+20,2);put(base+24,1);
    put(data,0x434d4e44);put(data+4,0x110);put(data+8,64);put(data+12,attributes?76:28);
    put(data+24,strings);put(data+28,attributes?22:8);put(data+68,attributes?3:0);
    const char names[]="connect\0port\0pid\0mcid\0";std::copy_n(names,attributes?22:8,bytes.begin()+data+strings);
    if(attributes){
      put(data+92,8);put(data+96,1);put(data+100,0);
      put(data+108,13);put(data+112,1);put(data+116,1);
      put(data+124,17);put(data+128,1);put(data+132,UINT32_MAX);
    }
 };
 request(1,false); fault([&]{service_sysutil_connect(s,0x86000400);});
 assert(s.sysutil_connections.empty());
 request(0,true);assert(!service_sysutil_connect(s,0x8600000)); // Unrelated condition does not service a ring.
 assert(service_sysutil_connect(s,0x86000000));
 assert(s.sysutil_connections.count(0)==1 && word(16)==128 && word(24)==2);
 const auto response=1024+word(12);
 assert(word(response)==0x434d4e44 && word(response+68)==1 && word(response+100)==0);
 assert(std::string(reinterpret_cast<char*>(bytes.data()+response+120))=="result");
 request(1,false);assert(service_sysutil_connect(s,0x86000400));
 assert(s.sysutil_connections.size()==2);
 request(0,true);fault([&]{service_sysutil_connect(s,0x86000000);}); // Duplicate not silently accepted.
 assert(word(16)==176 && word(24)==1); // Failure did not consume the request.
 bytes[1024+144]='x';fault([&]{service_sysutil_connect(s,0x86000000);});
 assert(s.sysutil_connections.size()==2 && word(16)==176);
''')

    def test_sysutil_video_wire_payload_errors_and_ring_wrap(self):
        self.run_native(r'''
 (void)call;s.sysutil_connections.insert(0);
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto request=[&](unsigned output,unsigned device,unsigned start=0){
   std::vector<std::uint8_t> packet(240,0);
   auto p=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)packet[a+j]=v>>(24-j*8);};
   p(0,0x50415241);p(4,0x110);p(8,64);p(12,104);p(24,176);p(28,21);p(48,208);p(52,32);
   p(68,1);p(84,44);p(88,44);p(92,4);p(96,1);p(100,0x4b06);
   p(108,7);p(112,2);p(136,13);p(140,6);p(148,24);p(152,UINT32_MAX);
   const char names[]="get\0id\0param\0payload\0";std::copy_n(names,21,packet.begin()+176);
   p(208,output);p(212,device);
   for(unsigned i=0;i<packet.size();++i)bytes[1024+(start+i)%0x7c00]=packet[i];
   put(8,(start+240)%0x7c00);put(12,start);put(16,240);put(20,2);put(24,1);
 };
 request(0,0,0x7c00-120); assert(service_sysutil_connect(s,0x86000000));
 auto response=1024+word(12);
 assert(word(12)==120 && word(24)==2 && word(response+100)==0);
 assert(bytes[response+216]==0 && bytes[response+217]==1); // Enabled RGB.
 assert(bytes[response+224]==2 && bytes[response+225]==1 && bytes[response+227]==2);
 assert(bytes[response+231]==1); // 720p progressive 16:9, 59.94Hz.
 request(1,0);assert(service_sysutil_connect(s,0x86000000));
 response=1024+word(12);assert(bytes[response+216]==1 && word(response+100)==0);
 request(2,0);assert(service_sysutil_connect(s,0x86000000));
 assert(word(1024+word(12)+100)==0x8002b225);
 request(0,1);assert(service_sysutil_connect(s,0x86000000));
 assert(word(1024+word(12)+100)==0x8002b224);
 request(0,0);put(1024+148,33);auto before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
 request(0,0);put(1024+100,0xdead);before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
 auto configure=[&](unsigned pitch){
   request(0,0);bytes[1024+176]='s';bytes[1024+177]='e';bytes[1024+178]='t';
   put(1024+100,0x4b07);put(1024+148,28);put(1024+212,0x02000100);put(1024+224,pitch);
 };
 s.rsx.vsync=false;configure(0x1800);assert(service_sysutil_connect(s,0x86000000));
 assert(s.video.pitch==0x1800 && s.video.aspect==1 && s.rsx.vsync);
 response=1024+word(12);assert(word(response+100)==0 && bytes[response+176]=='s');
 request(0,0);assert(service_sysutil_connect(s,0x86000000));response=1024+word(12);
 assert(bytes[response+227]==1); // A later query reflects the actual configuration change.
 configure(0);assert(service_sysutil_connect(s,0x86000000));
 assert(word(1024+word(12)+100)==0x8002b223 && s.video.pitch==0x1800);
''')

    # Audio PARA coverage moved to tests/test_native_audio_full.py (one configured silent endpoint).

    def test_boot_metadata_and_configured_language_responses(self):
        self.run_native(r'''
 (void)call;s.sysutil_connections.insert(0);s.sysutil_connections.insert(UINT64_C(1)<<32);
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 const unsigned base=0x8000,data=base+1024;
 put(base+16,112);put(base+20,2);put(base+24,1);
 put(data,0x47444154);put(data+4,0x110);put(data+8,64);put(data+12,28);put(data+24,96);put(data+28,9);
 const char boot[]="bootInfo";std::copy_n(boot,9,bytes.begin()+data+96);
 auto before=bytes;fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before);
 s.fs.disc="/test-fixture";const char id[]="TEST00001";std::copy_n(id,10,s.fs.boot_info.begin());s.fs.boot_info[15]=3;
 assert(service_sysutil_connect(s,0x86000400));auto response=data+word(base+12);
 assert(word(response+100)==0 && word(response+120)==32);
 assert(std::string(reinterpret_cast<char*>(bytes.data()+response+137))=="elmResult");
 assert(std::string(reinterpret_cast<char*>(bytes.data()+response+160))=="TEST00001" && bytes[response+175]==3);
 const unsigned request=1024;
 put(16,128);put(20,2);put(24,1);put(request,0x50415241);put(request+4,0x110);put(request+8,64);
 put(request+12,44);put(request+24,112);put(request+28,16);put(request+68,1);
 put(request+92,13);put(request+96,1);put(request+100,0x111);
 const char names[]="get_sysparam\0id\0";std::copy_n(names,16,bytes.begin()+request+112);
 s.settings.language=4;assert(service_sysutil_connect(s,0x86000000));response=request+word(12);
 assert(word(response+144)==4 && word(response+100)==0);
''')

    def test_user_page_allocation_alignment_accounting_and_free(self):
        self.run_native(r'''
 const auto initial=mem.allocated;
 assert(call(348,1,0x400,0x10000)==MM_EALIGN);
 assert(call(348,0x100000,0x123,0x10000)==MM_EINVAL);
 assert(call(348,0x100000,0x400,0x10000)==0);
 const auto base=mem.load(0x10000,4);assert(base%0x100000==0 && mem.load(base,8)==0);
 assert(mem.allocated==initial+0x100000);mem.store(base,8,123);
 assert(call(349,base+4)==MM_EINVAL && call(349,0x10000)==MM_EINVAL);
 assert(call(348,0x10000000,0x400,0x10004)==MM_ENOMEM);
 assert(call(349,base)==0 && mem.allocated==initial);fault([&]{mem.load(base,4);});
 assert(call(349,base)==MM_EINVAL);
''')

    def test_native_libc_memory_and_string_boundaries(self):
        self.run_native(r'''
 (void)call;
 mem.store(0x10000,4,0x61ff6200);mem.store(0x10010,4,0x61806200);
 assert(guest_strlen(s,0x10000)==3 && guest_strlen(s,0)==0);
 assert(guest_strcmp(s,0x10000,0x10010)==1);
 assert(guest_strcmp(s,0x10010,0x10000)==UINT64_MAX);
 assert(guest_strcmp(s,0x10000,0x10000)==0);
 r[3]=0x10020;r[4]=0x10000;r[5]=4;guest_copy(s,false);
 assert(r[3]==0x10020 && mem.load(0x10020,4)==0x61ff6200);
 r[3]=0x10022;r[4]=0x1ab;r[5]=2;guest_copy(s,true);
 assert(mem.load(0x10020,4)==0x61ffabab);
 r[3]=0x10001;r[4]=0x10000;r[5]=4;fault([&]{guest_copy(s,false);});
 r[3]=0;r[4]=0;r[5]=0;guest_copy(s,false);
 fault([&]{guest_strlen(s,0x12000);});
''')
