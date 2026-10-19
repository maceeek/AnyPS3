"""Native LV2/SPU lifecycle tests with fixed, independent byte/status oracles."""
import shutil
import struct
import unittest

from anyps3.elf import ElfImage, Segment
from anyps3.spu import lift
from anyps3.errors import AnyPS3Error
from tests import test_runtime_objects


def image(words):
    payload = b''.join(w.to_bytes(4, 'big') for w in words)
    return ElfImage(payload, 32, 'big', 0, 2, 23, 0x100,
                    (Segment(0, 1, 7, 0, 0x100, len(payload), len(payload), 4),))


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class Lv2SpuExtTests(unittest.TestCase):
    def native(self, body, **kwargs):
        test_runtime_objects.RuntimeObjectsTests().run_native(body, **kwargs)

    def test_ls_sizes_endian_alignment_states_and_output(self):
        self.native(r'''
 NativeSpu::Group group;group.id=0x4000100;group.state=6;group.count=1;
 group.context.resize(0x40000);group.threads.emplace_back();group.threads[0].id=0x100;
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups[0];
 auto access=[&](unsigned op,unsigned addr,std::uint64_t value,unsigned size){r[11]=op;r[3]=0x100;r[4]=addr;r[5]=value;r[6]=size;syscall(s);return r[3];};
 assert(access(181,0x3fff8,UINT64_C(0x0123456789abcdef),8)==0);
 const std::array<std::uint8_t,8> expected={1,0x23,0x45,0x67,0x89,0xab,0xcd,0xef};
 assert(std::equal(expected.begin(),expected.end(),g.context.begin()+0x3fff8));
 assert(access(182,0x3fff8,0x10100,8)==0 && mem.load(0x10100,8)==UINT64_C(0x0123456789abcdef));
 assert(access(181,0x3ffff,0x1122,1)==0 && g.context[0x3ffff]==0x22);
 assert(access(182,0x3ffff,0x10100,1)==0 && mem.load(0x10100,8)==0x22);
 assert(access(181,0x3fffe,0xfacebeef,2)==0);
 assert(access(182,0x3fffe,0x10100,2)==0 && mem.load(0x10100,8)==0xbeef);
 assert(access(181,0x3fffc,0xfedcba98,4)==0);
 assert(access(182,0x3fffc,0x10100,4)==0 && mem.load(0x10100,8)==0xfedcba98);
 for(unsigned size:{0u,3u,5u,16u})assert(access(181,0,0,size)==MM_EINVAL);
 assert(access(181,0x3ffff,0,2)==MM_EINVAL && access(181,0x40000,0,1)==MM_EINVAL);
 mem.store(0x10100,8,0x5555);g.state=1;
 assert(access(182,0,0x10100,1)==0x8001000f && mem.load(0x10100,8)==0x5555);
 g.state=4;assert(access(182,0,0,1)==0x8001000d);
 assert(call(181,0x9999,0,0,1)==MM_ESRCH);
''')

    def test_mailboxes_ports_and_queue_binding_contract(self):
        self.native(r'''
 NativeSpu::Group group;group.id=0x4000100;group.state=6;group.count=1;group.threads.emplace_back();group.threads[0].id=0x100;
 s.spu.groups.push_back(group);auto& t=s.spu.groups[0].threads[0];
 for(unsigned i=1;i<=5;++i)assert(call(190,0x100,i)==0);
 assert((t.in_mailbox==std::vector<std::uint32_t>{1,2,3,5}));
 assert(spu_read_channel(s,t,29)==1 && spu_read_channel(s,t,29)==2);
 assert(call(190,0x9999,42)==MM_ESRCH);
 s.events.queues.push_back({0x8d000000,4,0,{}});
 assert(call(191,0x100,0x8d000000,1,63)==0);
 assert(call(191,0x100,0x8d000000,1,63)==0x80010015);
 assert(call(191,0x100,0x8d000000,1,64)==MM_EINVAL);
 assert(call(192,0x100,1,63)==0 && call(192,0x100,1,63)==0x80010016);
 assert(call(191,0x100,0x9999,1,0)==MM_ESRCH);
 assert(call(192,0x100,0,0)==MM_EINVAL);
 assert(call(194,0x100,4)==MM_ESRCH);
 t.queue_bindings[0]={4,0x8d000000};assert(call(194,0x100,4)==0 && !t.queue_bindings[0].second);
 // PPU queues cannot be bound as SPU receive queues. With the older event
 // schema lacking type, the required integration API is an explicit stop.
 try{assert(call(193,0x100,0x8d000000,4)==MM_EINVAL);}catch(const std::runtime_error& e){
     assert(std::string(e.what()).find("Events::Queue::type")!=std::string::npos);
 }
''')

    def test_group_control_terminate_join_priority_and_arguments(self):
        self.native(r'''
 NativeSpu::Group group;group.id=0x4000100;group.state=6;group.count=1;group.threads.emplace_back();group.threads[0].id=0x100;
 s.spu.groups.push_back(group);auto& g=s.spu.groups[0];
 assert(call(179,g.id,15)==MM_EINVAL && call(179,g.id,128)==0);
 assert(call(180,g.id,0x10100)==0 && mem.load(0x10100,4)==128);
 for(unsigned i=0;i<4;++i)mem.store(0x10200+8*i,8,UINT64_C(0x1122334455667700)+i);
 assert(call(166,0x100,0x10200)==0 && g.threads[0].arguments[3]==UINT64_C(0x1122334455667703));
 assert(call(174,g.id)==0 && g.state==4 && call(174,g.id)==0);
 assert(call(176,g.id)==0x8001000f && call(175,g.id)==0 && g.state==6);
 assert(call(175,g.id)==0x8001000f && call(176,g.id)==0 && s.reschedule);
 g.state=3;assert(call(174,g.id)==0 && g.state==5);
 assert(call(177,g.id,10)==0x8001000f && call(175,g.id)==0 && g.state==3);
 g.state=6;
 assert(call(177,g.id,0xdeadbeef)==0 && g.state==1 && g.threads[0].stopped);
 assert(call(178,g.id,0x10100,0x10104)==0 && mem.load(0x10100,4)==4 && mem.load(0x10104,4)==0xdeadbeef);
 assert(call(165,0x100,0x10100)==0x8001000f && call(177,g.id,0)==0x8001000f);
 assert(call(250,g.id,0)==MM_EINVAL);
 fault([&]{call(248,g.id);});fault([&]{call(249,g.id);});
 g.type=0x20;fault([&]{call(250,g.id,0);});
 assert(call(250,g.id,2)==MM_EINVAL);
''')

    def test_static_stop_exit_scheduler_and_join(self):
        # Original encodings: wrch out_mbox,r3; stop 0x102; stop 0x101.
        spu=image([0x21a00e03,0x00000102,0x00000101,0x00000100,0x00000001])
        self.native(r'''
 NativeSpu::Group group;group.id=0x4000100;group.count=2;group.state=6;group.context.resize(2*0x40000);group.threads.resize(2);
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups[0];
 for(unsigned i=0;i<2;++i){auto& t=g.threads[i];t.id=0x100+(i<<24);t.entry=0x100;auto* ls=g.context.data()+i*0x40000;
     const std::array<std::uint32_t,5> words={0x21a00e03,0x102,0x101,0x100,1};
     for(unsigned j=0;j<words.size();++j)for(unsigned b=0;b<4;++b)ls[0x100+j*4+b]=words[j]>>(24-b*8);
     spu_prepare(s,t,ls);spu_word(t.gpr[3],0,0x81230000+i);
 }
 assert(spu_run(s,10) && g.state==1 && g.join_cause==2);
 assert(g.threads[0].steps==2 && g.threads[1].steps==2 && !spu_run(s,10));
 assert(call(165,0x100,0x10100)==0 && mem.load(0x10100,4)==0x81230000);
 assert(call(165,0x1000100,0x10100)==0 && mem.load(0x10100,4)==0x81230001);
 assert(call(178,g.id,0x10100,0x10104)==0 && mem.load(0x10100,4)==2 && mem.load(0x10104,4)==0);
 g.state=6;g.threads[0].stopped=false;g.threads[0].pc=0x108;
 g.threads[0].out_mailbox=0xf00dcafe;g.threads[0].out_mailbox_full=true;
 assert(spu_run(s,10) && g.state==1 && g.join_cause==1 && g.exit_status==0xf00dcafe);
 assert(call(178,g.id,0x10100,0x10104)==0 && mem.load(0x10100,4)==1 && mem.load(0x10104,4)==0xf00dcafe);
 g.state=6;auto& t=g.threads[0];t.stopped=false;t.pc=0x10c;const auto steps=t.steps;
 assert(spu_run(s,20) && t.pc==0x110 && t.steps==steps+1);
 assert(spu_run(s,20) && t.pc==0x114 && t.steps==steps+2);
 fault([&]{spu_stop(s,t,0x103);});fault([&]{spu_stop(s,t,0x120);});fault([&]{spu_stop(s,t,2);});
 t.out_mailbox_full=false;fault([&]{spu_stop(s,t,0x101);});
''',spu_images=(spu,))

    def test_receive_event_suspends_group_and_delivers_real_data(self):
        spu=image([0x110,0x111,0x32000000])
        self.native(r'''
 (void)call;NativeSpu::Group group;group.id=0x4000100;group.state=6;group.count=2;
 group.context.resize(2*0x40000);group.threads.resize(2);s.spu.groups.push_back(std::move(group));
 auto& g=s.spu.groups[0];auto& t=g.threads[0];t.id=0x100;t.entry=0x100;
 auto* ls=g.context.data();const std::array<std::uint32_t,3> words={0x110,0x111,0x32000000};
 for(unsigned j=0;j<3;++j)for(unsigned b=0;b<4;++b)ls[0x100+4*j+b]=words[j]>>(24-b*8);
 spu_prepare(s,t,ls);g.threads[1].stopped=true;
 s.events.queues.push_back({0x8d000000,4,0,{}});auto& q=s.events.queues[0];
 t.queue_bindings[0]={0xdeadbeef,q.id};t.out_mailbox=0xdeadbeef;t.out_mailbox_full=true;
 assert(!spu_run(s,8) && g.state==3 && t.waiting_receive && t.pc==0x100 && t.steps==0);
 assert(!spu_run(s,8) && g.state==3 && t.steps==0);
 q.data.push_back({0x1234,UINT64_C(0xabcdef0112345678),UINT64_C(0x1111111187654321),0x80000000});
 // A suspended waiting group must not wake until explicitly resumed.
 g.state=5;assert(!spu_run(s,8) && q.data.size()==1);g.state=3;
 assert(spu_run(s,0) && g.state==6 && t.pc==0x104 && t.steps==1 && !t.waiting_receive && q.data.empty());
 assert((t.in_mailbox==std::vector<std::uint32_t>{0,0x12345678,0x87654321,0x80000000}));
 t.in_mailbox.clear();t.out_mailbox=0xdeadbeef;t.out_mailbox_full=true;
 assert(spu_run(s,1) && t.pc==0x108 && t.in_mailbox[0]==MM_EBUSY);
 t.pc=0x104;t.in_mailbox={1,2};t.out_mailbox_full=true;
 assert(spu_run(s,1) && t.in_mailbox.size()==1 && t.in_mailbox[0]==MM_EBUSY && !t.out_mailbox_full);
 t.pc=0x100;t.in_mailbox={1,2};t.out_mailbox=0xdeadbeef;t.out_mailbox_full=true;
 assert(spu_run(s,1) && t.in_mailbox.size()==1 && t.in_mailbox[0]==MM_EBUSY && t.out_mailbox_full);
 t.pc=0x100;t.in_mailbox.clear();t.out_mailbox=99;t.out_mailbox_full=true;
 assert(spu_run(s,1) && t.in_mailbox[0]==MM_EINVAL);
 t.pc=0x100;t.in_mailbox.clear();t.out_mailbox=0xdeadbeef;t.out_mailbox_full=true;g.type=0x18;
 assert(spu_run(s,1) && t.in_mailbox[0]==MM_EINVAL && g.state==6);
 g.type=0;t.pc=0x100;t.in_mailbox.clear();t.out_mailbox=0xdeadbeef;t.out_mailbox_full=true;
 assert(!spu_run(s,1) && g.state==3);
 s.events.queues.clear();assert(spu_run(s,0) && g.state==6 && t.in_mailbox[0]==0x80010013 && t.pc==0x104);
''',spu_images=(spu,))

    def test_image_open_requires_aot_and_keeps_initialized_image_after_close(self):
        spu=image([0x32000000]*4)
        # ELF32 BE, SPU executable, one PT_LOAD with 16 zero-filled BSS bytes.
        header=b'\x7fELF\x01\x02\x01'+bytes(9)+struct.pack('>HHIIIIIHHHHHH',2,23,1,0x100,52,0,0,52,32,1,0,0,0)
        payload=header+struct.pack('>IIIIIIII',1,84,0x100,0,16,16,7,4)+spu.data
        values=','.join(str(v) for v in payload)
        self.native(r'''
 (void)call;char dir[]="/tmp/anyps3-spu-XXXXXX";assert(::mkdtemp(dir));s.fs.hdd_root=dir;
 const auto path=std::filesystem::path(dir)/"image.elf";
 const std::vector<std::uint8_t> elf={'''+values+r'''};
 {std::ofstream f(path,std::ios::binary);f.write(reinterpret_cast<const char*>(elf.data()),elf.size());}
 const char guest[]="/dev_hdd0/image.elf";std::copy_n(guest,sizeof(guest),mem.access(0x10100,sizeof(guest),true));
 auto open=[&]{r[11]=156;r[3]=0x10200;r[4]=0x10100;syscall(s);return r[3];};
 assert(open()==0 && mem.load(0x10200,4)==1 && mem.load(0x10208,8)==0 && s.spu_ext.images.size()==1);
 const auto handle=mem.load(0x10204,4);assert(handle==0x23000100);
 NativeSpu::Group group;group.id=0x4000100;group.count=1;group.context.resize(0x40000);s.spu.groups.push_back(std::move(group));
 mem.store(0x10300,4,0);mem.store(0x10304,4,0);mem.store(0x10308,4,0);
 for(unsigned i=0;i<4;++i)mem.store(0x10400+8*i,8,i+1);
 r[11]=172;r[3]=0x10500;r[4]=0x4000100;r[5]=0;r[6]=0x10200;r[7]=0x10300;r[8]=0x10400;
 assert(spu_syscall(s)==0 && s.spu.groups[0].threads[0].initial_ls);
 r[11]=158;r[3]=0x10200;syscall(s);assert(r[3]==0 && s.spu_ext.images.empty());
 r[11]=158;r[3]=0x10200;syscall(s);assert(r[3]==MM_ESRCH);
 r[11]=173;r[3]=0x4000100;assert(spu_syscall(s)==0 && s.spu.groups[0].threads[0].steps==1024);
 // Restart resets stale mailbox/event/exit state and uses updated arguments.
 auto& g=s.spu.groups[0];g.state=1;auto& t=g.threads[0];t.stopped=true;t.exit_status_ready=true;t.in_mailbox={9};
 t.event_mask=0x400;t.waiting_event=true;t.arguments[0]=UINT64_C(0x1122334455667788);
 r[11]=173;r[3]=g.id;assert(spu_syscall(s)==0 && t.steps==1024 && !t.stopped && !t.exit_status_ready && t.in_mailbox.empty() && !t.waiting_event);
 assert(spu_word(t.gpr[3],0)==0x11223344 && spu_word(t.gpr[3],1)==0x55667788);
 {auto bad=elf;bad.back()=1;std::ofstream f(path,std::ios::binary);f.write(reinterpret_cast<const char*>(bad.data()),bad.size());}
 mem.store(0x10200,4,0xdeadbeef);fault([&]{open();});assert(s.spu_ext.images.empty() && mem.load(0x10200,4)==0xdeadbeef);
 {std::ofstream f(path,std::ios::binary);f.write("bad",3);}assert(open()==0x80010007);
 std::filesystem::remove_all(dir);assert(open()==0x80010006);
''',spu_images=(spu,))

    def test_stopd_routes_only_to_explicit_unknown_service(self):
        body='(void)call;NativeSpu::Thread t;t.pc=0x100;'
        for word in (0x28000000,0x103,0x120,0):
            body+='fault([&]{'+lift(0x100,word)+'});assert(t.pc==0x100 && !t.stopped);'
        self.native(body)
