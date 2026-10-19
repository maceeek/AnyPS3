"""Independent native checks of SPU byte order, aliases, transfers and guards."""
import shutil
import unittest
from anyps3.spu import lift
from anyps3.flow import lift as lift_ppu
from anyps3.elf import ElfImage, Segment
from anyps3.errors import AnyPS3Error
from tests import test_runtime_objects


def instruction(word):
    return '{auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};'+lift(0x100,word)+'}\n'


@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class SpuTests(unittest.TestCase):
    def run_native(self,body):
        test_runtime_objects.RuntimeObjectsTests().run_native(body)

    def test_ppu_sleep_grants_running_spu_the_slept_interval(self):
        payload=(0x64<<23).to_bytes(4,'big') # BR to itself.
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,4,4,4),))
        body=r'''
 NativeSpu::Group group;group.state=6;group.context.resize(0x40000);group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups.back();auto& t=g.threads[0];
 t.entry=0x100;
'''
        for i,byte in enumerate(payload):body+=f'g.context[{0x100+i}]={byte};'
        body+=r'''
 spu_prepare(s,t,g.context.data());
 // Only the caller sleeps; with no other ready PPU thread the scheduler keeps
 // running SPUs until the deadline, then the retried usleep returns 0.
 bool blocked=false;try{call(141,2000);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.sleep_microseconds==2000);
 const auto start=std::chrono::steady_clock::now();ppu_schedule(s,true);
 assert(std::chrono::steady_clock::now()-start>=std::chrono::microseconds(1500) && !s.wait_kind && t.steps>0);
 assert(call(141,2000)==0);
 g.state=1;const auto stopped=t.steps;blocked=false;try{call(141,500);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked);ppu_schedule(s,true);assert(call(141,500)==0 && t.steps==stopped); // Stopped groups do not run.
 // During module initialization the host waits and SPUs get the interval at 3.2 GHz.
 g.state=6;s.module_loading=true;assert(call(141,30)==0 && t.steps==stopped+96000);
 s.module_loading=false;fault([&]{call(141,1000001);});assert(t.steps==stopped+96000);
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=(image,))

    def test_vsync_flip_wait_lets_running_spu_progress(self):
        payload=(0x64<<23).to_bytes(4,'big') # BR to itself.
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,4,4,4),))
        body=r'''
 (void)call;assert(setenv("ANYPS3_SPU_MAX_STEPS","100000000000",1)==0);
 NativeSpu::Group group;group.state=6;group.context.resize(0x40000);group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups.back();auto& t=g.threads[0];
 t.entry=0x100;
'''
        for i,byte in enumerate(payload):body+=f'g.context[{0x100+i}]={byte};'
        body+=r'''
 spu_prepare(s,t,g.context.data());
 auto& gpu=s.rsx;gpu.driver=0x10000;gpu.reports=0x10000;gpu.flip_modes[1]=2;
 gpu.display_buffers[1]={640,360,2560,0};mem.store(gpu.driver+0x1100,4,0x40000002);
 gpu.vblank_epoch_ns=static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
     std::chrono::steady_clock::now().time_since_epoch()).count());
 fault([&]{rsx_flip_display(s,1,0x80000101);}); // Waits for vblank, then capture needs Vulkan.
 assert(t.steps>=65536 && t.steps<UINT64_C(100000000000)); // SPU ran during the vblank wait.
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=(image,))

    def test_configurable_step_limit_checks_boundary_without_changing_execution(self):
        # BR to itself: the scheduler must execute real AOT code past the old
        # limit, then stop before executing an instruction outside its budget.
        payload=(0x64<<23).to_bytes(4,'big')
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,4,4,4),))
        body=r'''
 (void)call;assert(spu_step_limit(nullptr)==10000000);
 assert(spu_step_limit("1")==1 && spu_step_limit("18446744073709551615")==UINT64_MAX);
 for(const auto* value:{"","0","-1","+1"," 1","1 ","1x","18446744073709551616"})
     fault([&]{spu_step_limit(value);});
 assert(setenv("ANYPS3_SPU_MAX_STEPS","10000002",1)==0);
 NativeSpu::Group group;group.state=6;group.context.resize(0x40000);group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups.back();auto& t=g.threads[0];
 t.entry=0x100;
'''
        for i,byte in enumerate(payload):body+=f'g.context[{0x100+i}]={byte};'
        body+=r'''
 spu_prepare(s,t,g.context.data());t.steps=9999999;
 assert(spu_run(s,3) && t.steps==10000002 && t.pc==0x100);
 fault([&]{spu_run(s,1);});assert(t.steps==10000002 && t.pc==0x100);
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=(image,))

    def test_mailbox_events_deliver_real_payload_and_report_full_queue(self):
        self.run_native(r'''
 NativeSpu::Thread t;t.id=0x1234;t.ports[7]=0x5678;
 std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 s.events.queues.push_back({0x5678,1,0,{}});auto& q=s.events.queues.back();
 auto blocked=[&](auto f){bool hit=false;try{f();}catch(const SpuBlocked&){hit=true;}assert(hit);};
 blocked([&]{spu_read_channel(s,t,29);});
 fault([&]{spu_write_channel(s,t,ls,30,0x47000001);});
 spu_write_channel(s,t,ls,28,0xabcdef01);
 blocked([&]{spu_write_channel(s,t,ls,28,99);});
 spu_write_channel(s,t,ls,30,0x47123456);
 assert(!t.out_mailbox_full && t.in_mailbox.empty() && q.data.size()==1);
 spu_write_channel(s,t,ls,28,0xfedcba98);
 spu_write_channel(s,t,ls,30,0x07654321);
 assert(!t.out_mailbox_full && spu_read_channel(s,t,29)==0x8001000a && q.data.size()==1);
 // Throw to a full queue consumes the outbox without an inbox reply.
 spu_write_channel(s,t,ls,28,0x13579bdf);spu_write_channel(s,t,ls,30,0x47654321);
 assert(!t.out_mailbox_full && t.in_mailbox.empty() && q.data.size()==1);
 assert(call(130,q.id)==0 && r[4]==UINT64_C(0xffffffff53505501) && r[5]==t.id && r[6]==((UINT64_C(7)<<32)|0x123456) && r[7]==0xabcdef01);
 spu_write_channel(s,t,ls,28,0xfedcba98);spu_write_channel(s,t,ls,30,0x07654321);
 assert(!t.out_mailbox_full && spu_read_channel(s,t,29)==0);
 assert(call(130,q.id)==0 && r[6]==((UINT64_C(7)<<32)|0x654321) && r[7]==0xfedcba98);
 spu_write_channel(s,t,ls,28,42);spu_write_channel(s,t,ls,30,0x08000001);
 assert(spu_read_channel(s,t,29)==0x80010016 && q.data.empty());
 spu_write_channel(s,t,ls,28,42);spu_write_channel(s,t,ls,30,0x48000001);
 assert(t.in_mailbox.empty() && !t.out_mailbox_full);
 t.in_mailbox={99};spu_write_channel(s,t,ls,28,42);spu_write_channel(s,t,ls,30,0x07000001);
 assert(spu_read_channel(s,t,29)==MM_EBUSY && q.data.empty() && !t.out_mailbox_full);
 spu_write_channel(s,t,ls,28,42);fault([&]{spu_write_channel(s,t,ls,30,0x80000000);});
 assert(t.out_mailbox_full && t.out_mailbox==42);
''')

    def test_dma_spans_adjacent_mappings_and_rejects_holes_atomically(self):
        self.run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 mem.map(0x12000,0x1000,6,nullptr,0);
 mem.regions.push_back({0x30000,6,mem.regions[0].bytes});
 mem.regions.push_back({0x32000,6,mem.regions[1].bytes});
 for(unsigned i=0;i<32;++i)mem.store(0x11ff0+i,1,i);
 spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x11ff0);spu_write_channel(s,t,ls,19,32);
 spu_write_channel(s,t,ls,21,0x40);
 for(unsigned i=0;i<32;++i){assert(ls[0x200+i]==i);ls[0x200+i]=255-i;}
 const auto first=mem.reservation_token(0x11f80),second=mem.reservation_token(0x12000);
 spu_write_channel(s,t,ls,21,0x20);
 for(unsigned i=0;i<32;++i)assert(mem.load(0x31ff0+i,1)==255-i);
 assert(mem.reservation_token(0x11f80)!=first && mem.reservation_token(0x12000)!=second);
 mem.map(0x20000,16,6,nullptr,0);mem.map(0x20020,16,6,nullptr,0);
 spu_write_channel(s,t,ls,18,0x20000);
 fault([&]{spu_write_channel(s,t,ls,21,0x40);});
 for(unsigned i=0;i<32;++i)assert(ls[0x200+i]==255-i);
 fault([&]{spu_write_channel(s,t,ls,21,0x20);});
 for(unsigned i=0;i<16;++i)assert(mem.load(0x20000+i,1)==0);
 mem.map(0x21000,16,6,nullptr,0);mem.map(0x21010,16,4,nullptr,0);
 spu_write_channel(s,t,ls,18,0x21000);
 fault([&]{spu_write_channel(s,t,ls,21,0x20);});
 for(unsigned i=0;i<32;++i)assert(mem.load(0x21000+i,1)==0);
 mem.map(0x22000,16,6,nullptr,0);mem.map(0x22010,16,7,nullptr,0);
 spu_write_channel(s,t,ls,18,0x22000);
 fault([&]{spu_write_channel(s,t,ls,21,0x20);});
 for(unsigned i=0;i<32;++i)assert(mem.load(0x22000+i,1)==0);
 fault([&]{mem.transfer(0xfffffff0,ls,32,true);});
''')

    def test_quadword_shifts_with_bit_counts_and_aliasing(self):
        body='(void)call;NativeSpu::Thread t;'
        raw=bytes.fromhex('8123456789abcdef0123456789abcdef')
        value=int.from_bytes(raw,'big');mask=(1<<128)-1
        for count in (0,1,7,8,15,63,120,127,128,129,248,255,256,0xffffffff):
            for op in (0x1cc,0x1cd,0x1cf,0x1d9,0x1db,0x1fb,0x1dd,0x1df,0x1f9):
                body+='t.gpr[1]={'+','.join(map(str,raw))+'};'
                body+=f'spu_word(t.gpr[2],0,UINT32_C({count}));'
                encoded=count&127 if op in (0x1fb,0x1f9) else 2
                body+=instruction((op<<21)|(encoded<<14)|(1<<7)|1)
                if op==0x1cc:
                    bits=((count>>3)&15)*8
                    expected=((value<<bits)|(value>>(128-bits)))&mask
                elif op==0x1cd:expected=value>>((-(count>>3)&31)*8)
                elif op==0x1cf:expected=(value<<(((count>>3)&31)*8))&mask
                elif op in (0x1d9,0x1f9):expected=value>>(-count&7)
                elif op==0x1dd:expected=value>>((-count&31)*8)
                elif op==0x1df:expected=(value<<((count&31)*8))&mask
                else:expected=(value<<(count&7))&mask
                for i,byte in enumerate(expected.to_bytes(16,'big')):
                    body+=f'assert(t.gpr[1][{i}]=={byte});'
        self.run_native(body)

    def test_byte_sums_preserve_both_halfword_results_and_aliases(self):
        body='(void)call;NativeSpu::Thread t;'
        for target in (1,2,3):
            body+='t.gpr[1]={255,255,255,255,0,0,0,0,1,2,3,4,128,0,127,1};'
            body+='t.gpr[2]={0,0,0,0,255,255,255,255,4,3,2,1,1,1,1,1};'
            body+=instruction((0x253<<21)|(2<<14)|(1<<7)|target)
            for i,value in enumerate((0x000003fc,0x03fc0000,0x000a000a,0x00040100)):
                body+=f'assert(spu_word(t.gpr[{target}],{i})==UINT32_C({value}));'
        self.run_native(body)

    def test_unsigned_halfword_multiply_ignores_high_halves(self):
        body='(void)call;NativeSpu::Thread t;'
        left=(0x1234ffff,0x80008000,0xffff0000,0xffffffff)
        right=(0x5678ffff,0x80000002,0xffffffff,0xabcd0001)
        for target in (1,2,3):
            for i,(a,b) in enumerate(zip(left,right)):
                body+=f'spu_word(t.gpr[1],{i},UINT32_C({a}));spu_word(t.gpr[2],{i},UINT32_C({b}));'
            body+=instruction((0x3cc<<21)|(2<<14)|(1<<7)|target)
            for i,value in enumerate((0xfffe0001,0x10000,0,0xffff)):
                body+=f'assert(spu_word(t.gpr[{target}],{i})==UINT32_C({value}));'
        self.run_native(body)

    def test_integer_multiply_variants_signed_halves_and_accumulators(self):
        body='(void)call;NativeSpu::Thread t;'
        left=(0x8000ffff,0xffff8000,0x7fff0000,0xffffffff)
        right=(0xffff8000,0x8000ffff,0xffff7fff,0x0001ffff)
        signed=lambda value:(value&65535)-65536 if value&32768 else value&65535
        for op in (0x3c4,0x3c5,0x3c6,0x3c7,0x3ce,0x346,0x34e,0x74<<3,0x75<<3):
            for target in (1,2,3):
                for imm in ((-512,-1,0,1,511) if op in (0x74<<3,0x75<<3) else (0,)):
                    for i,(a,b) in enumerate(zip(left,right)):
                        body+=f'spu_word(t.gpr[1],{i},UINT32_C({a}));spu_word(t.gpr[2],{i},UINT32_C({b}));spu_word(t.gpr[3],{i},UINT32_MAX);'
                    body+=instruction((op<<21)|(((imm&1023) if op in (0x74<<3,0x75<<3) else 2)<<14)|(1<<7)|target)
                    for i,(a,b) in enumerate(zip(left,right)):
                        if op==0x3c5:value=((a>>16)*(b&65535))<<16
                        elif op in (0x3ce,0x34e):value=(a>>16)*(b>>16)
                        elif op in (0x3c6,0x346):value=signed(a>>16)*signed(b>>16)
                        elif op==0x75<<3:value=(a&65535)*(imm&65535)
                        elif op==0x74<<3:value=signed(a)*imm
                        else:value=signed(a)*signed(b)
                        if op==0x3c7:value>>=16
                        if op in (0x346,0x34e):value+=a if target==1 else b if target==2 else 0xffffffff
                        body+=f'assert(spu_word(t.gpr[{target}],{i})==UINT32_C({value&0xffffffff}));'
        self.run_native(body)

    def test_halfword_rotates_and_shifts_boundary_counts(self):
        body='(void)call;NativeSpu::Thread t;'
        values=(0x8001,0xffff,0x1234,0x7fff,0,1,0x8000,0xabcd)
        for op in (0x5c,0x5d,0x5e,0x5f,0x7c,0x7d,0x7e,0x7f):
            for count in (0,1,7,15,16,17,31,32,63,127):
                for i,value in enumerate(values):
                    body+=f't.gpr[1][{i*2}]={value>>8};t.gpr[1][{i*2+1}]={value&255};t.gpr[2][{i*2}]=0;t.gpr[2][{i*2+1}]={count+i};'
                body+=instruction((op<<21)|((count if op>=0x7c else 2)<<14)|(1<<7)|1)
                for i,value in enumerate(values):
                    c=count if op>=0x7c else count+i;kind=op&3
                    if kind==0:
                        n=c&15;expected=((value<<n)|(value>>(16-n)))&65535
                    elif kind==1:expected=value>>(-c&31)
                    elif kind==2:expected=((value-65536 if value&0x8000 else value)>>(-c&31))&65535
                    else:expected=(value<<(c&31))&65535
                    body+=f'assert(spu_element(t.gpr[1],{i*2},2)=={expected});'
        self.run_native(body)

    def test_halfword_arithmetic_wraps_each_lane(self):
        body='(void)call;NativeSpu::Thread t;'
        values=(0,1,0xffff,0x8000,0x7fff,0x1234,0xff00,0x00ff)
        for op in (0x48,0xc8,0xd<<3,0x1d<<3):
            for imm in (-512,-1,0,1,511):
                for i,value in enumerate(values):
                    b=values[7-i]
                    body+=f't.gpr[1][{i*2}]={value>>8};t.gpr[1][{i*2+1}]={value&255};t.gpr[2][{i*2}]={b>>8};t.gpr[2][{i*2+1}]={b&255};'
                immediate=op in (0xd<<3,0x1d<<3)
                body+=instruction((op<<21)|(((imm&1023) if immediate else 2)<<14)|(1<<7)|1)
                for i,value in enumerate(values):
                    b=imm if immediate else values[7-i]
                    expected=(b-value if op in (0x48,0xd<<3) else b+value)&65535
                    body+=f'assert(spu_element(t.gpr[1],{i*2},2)=={expected});'
        self.run_native(body)

    def test_fpscr_read_write_masks_reserved_bits(self):
        body='(void)call;NativeSpu::Thread t;'
        body+=instruction((0x398<<21)|4)
        body+='assert(t.gpr[4]==SpuRegister{});t.gpr[3].fill(255);'
        body+=instruction((0x3ba<<21)|(3<<7))
        body+=instruction((0x398<<21)|4)
        for i,mask in enumerate((0xf07,0x3f07,0x3f07,0xf07)):
            body+=f'assert(spu_word(t.gpr[4],{i})=={mask} && spu_word(t.gpr[3],{i})==UINT32_MAX);'
        body+='t.gpr[3]={};'
        body+=instruction((0x3ba<<21)|(3<<7))
        body+=instruction((0x398<<21)|4)
        body+='assert(t.gpr[4]==SpuRegister{});'
        self.run_native(body)

    def test_word_rotates_and_signed_shifts_at_lane_boundaries(self):
        body='(void)call;NativeSpu::Thread t;'
        values=(0x80000001,0x12345678,0xffffffff,0x7fffffff)
        for amount in (0,1,7,31,32,33,63,64,127):
            for opcode in (0x58,0x78,0x5a,0x7a):
                for i,value in enumerate(values):
                    body+=f'spu_word(t.gpr[1],{i},UINT32_C({value}));spu_word(t.gpr[2],{i},{amount+i});'
                # Destination aliases source; register form uses different counts in each lane.
                body+=instruction((opcode<<21)|((2 if opcode<0x78 else amount)<<14)|(1<<7)|1)
                for i,value in enumerate(values):
                    count=amount+i if opcode<0x78 else amount
                    if opcode in (0x58,0x78):
                        n=count&31;expected=((value<<n)|(value>>((32-n)&31)))&0xffffffff
                    else:
                        n=(-count)&63;signed_value=value-(1<<32) if value&0x80000000 else value
                        expected=(signed_value>>n)&0xffffffff
                    body+=f'assert(spu_word(t.gpr[1],{i})==UINT32_C({expected}));'
        self.run_native(body)

    def test_ppu_signal_notification_overwrite_or_and_blocking_read(self):
        self.run_native(r'''
 NativeSpu::Group group;group.threads.emplace_back();group.threads[0].id=0x123;s.spu.groups.push_back(std::move(group));
 auto& t=s.spu.groups[0].threads[0];
 bool blocked=false;try{spu_read_channel(s,t,3);}catch(const SpuBlocked&){blocked=true;}
 assert(blocked && t.waiting_signal==1);
 assert(call(184,t.id,0,0)==0 && t.signal_ready[0]); // Zero is an actual notification.
 assert(spu_read_channel(s,t,3)==0 && !t.signal_ready[0] && !t.waiting_signal);
 assert(call(184,t.id,0,4)==0 && call(184,t.id,0,2)==0);
 assert(spu_read_channel(s,t,3)==2); // Default overwrite mode.
 assert(call(187,t.id,3)==0 && call(188,t.id,0x10000)==0 && mem.load(0x10000,8)==3);
 assert(call(184,t.id,1,4)==0 && call(184,t.id,1,2)==0 && spu_read_channel(s,t,4)==6);
 assert(call(184,t.id,1,1)==0 && spu_read_channel(s,t,4)==1); // Read clears the old bits.
 assert(call(184,t.id,2,99)==MM_EINVAL && call(184,99,0,1)==MM_ESRCH);
 assert(call(187,t.id,4)==MM_EINVAL && t.signal_config==3);
''')

    def test_ppu_atomic_store_observes_spu_dma_and_preserves_cr_fields(self):
        body='(void)call;auto& state=s;'
        for name,xo,width in [('word',20,4),('double',84,8)]:
            load=lift_ppu((31<<26)|(3<<21)|(4<<11)|(xo<<1),0x100).cpp
            store=lift_ppu((31<<26)|(5<<21)|(4<<11)|((xo+130)<<1)|1,0x104).cpp
            body+=f'auto load_{name}=[&]{{{load}}};auto store_{name}=[&]{{{store}}};'
            body+=f'''
 r[4]=0x10100;mem.store(r[4],{width},0x12345678);s.cr=0xabcdef01;s.xer=UINT64_C(1)<<31;
 load_{name}();assert(r[3]==0x12345678);r[5]=0xaabbccdd;
 store_{name}();assert(mem.load(r[4],{width})==0xaabbccdd && s.cr==0x3bcdef01);
 store_{name}();assert(s.cr==0x1bcdef01);
 load_{name}();
 '''
            body+='''{
 NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x10100);
 spu_write_channel(s,t,ls,21,0xd0);ls[0x200]=0x55;spu_write_channel(s,t,ls,21,0xb4);
 assert(spu_read_channel(s,t,27)==0);
 }'''
            body+=f'store_{name}();assert(s.cr==0x1bcdef01 && mem.load(0x10100,1)==0x55);'
            body+=f'r[4]=0x10101;fault([&]{{load_{name}();}});'
        self.run_native(body)

    def test_integer_aliases_shuffle_controls_and_scalar_layout(self):
        body=r'''
 (void)call;NativeSpu::Thread t;
 spu_word(t.gpr[1],0,0xffffffff);spu_word(t.gpr[1],1,0x80000000);
 spu_word(t.gpr[1],2,0x12345678);spu_word(t.gpr[1],3,0);
 spu_word(t.gpr[2],0,1);spu_word(t.gpr[2],1,32);spu_word(t.gpr[2],2,7);spu_word(t.gpr[2],3,63);
'''
        body+=instruction((0xc2<<21)|(2<<14)|(1<<7)|3)  # cg
        body+=r'assert(spu_word(t.gpr[3],0)==1 && spu_word(t.gpr[3],1)==0);'
        body+=instruction((0xc0<<21)|(2<<14)|(1<<7)|1)  # a with destination alias
        body+=r'assert(spu_word(t.gpr[1],0)==0 && spu_word(t.gpr[1],1)==0x80000020 && spu_word(t.gpr[1],2)==0x1234567f && spu_word(t.gpr[1],3)==63);'
        body+=instruction((0x1c<<24)|(1023<<14)|(1<<7)|1)  # ai -1
        body+=r'assert(spu_word(t.gpr[1],0)==0xffffffff && spu_word(t.gpr[1],3)==62);'
        body+=r'''
 for(unsigned i=0;i<16;++i){t.gpr[1][i]=i;t.gpr[2][i]=16+i;}
 t.gpr[3]={0,15,16,31,128,159,192,223,224,255,1,17,2,18,3,19};
'''
        body+=instruction((0xb<<28)|(3<<21)|(2<<14)|(1<<7)|3)
        body+=r'const SpuRegister shuffled={0,15,16,31,0,0,255,255,128,128,1,17,2,18,3,19};assert(t.gpr[3]==shuffled);'
        body+='spu_word(t.gpr[1],0,7);'
        body+=instruction((0x1f6<<21)|(1<<7)|3)
        body+=r'const SpuRegister word_control={16,17,18,19,0,1,2,3,24,25,26,27,28,29,30,31};assert(t.gpr[3]==word_control);'
        body+=instruction((0x1f7<<21)|(1<<7)|3)
        body+=r'const SpuRegister double_control={0,1,2,3,4,5,6,7,24,25,26,27,28,29,30,31};assert(t.gpr[3]==double_control);'
        body+='spu_word(t.gpr[2],0,0x1237);'
        body+=instruction((0x1a9<<21)|(1<<19)|(2<<7)|2)
        body+='assert(t.pc==0x1234 && spu_word(t.gpr[2],0)==0x104 && spu_read_channel(s,t,13)==0);'
        body+=instruction((0x1a8<<21)|(1<<18)|(2<<7))
        body+='assert(t.interrupts_enabled && spu_read_channel(s,t,13)==1);'
        body+='fault([&]{'+instruction((0x1a8<<21)|(3<<18)|(2<<7))+'});'
        self.run_native(body)

    def test_static_image_overlay_identity_and_changed_code_guard(self):
        words=[(0x81<<23)|(3<<7)|3,(0x1c<<24)|(1023<<14)|(3<<7)|3,(0x42<<23)|(0xffff<<7)|3]
        payload=b''.join(w.to_bytes(4,'big') for w in words)
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,len(payload),len(payload),4),))
        overlay=((0x81<<23)|(42<<7)|4).to_bytes(4,'big')
        body='(void)call;NativeSpu::Thread t;t.entry=0x100;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();\n'
        for i,byte in enumerate(payload):body+=f'ls[{0x100+i}]={byte};'
        body+='spu_prepare(s,t,ls);spu_step(s,t,ls);for(unsigned i=0;i<6;++i)spu_step(s,t,ls);assert(spu_word(t.gpr[3],0)==0 && t.pc==0x10c);'
        body+='t.pc=0x3e000;fault([&]{spu_step(s,t,ls);});t.pc=0x1200;fault([&]{spu_step(s,t,ls);});'
        for i,byte in enumerate(overlay):body+=f'ls[{0x1200+i}]={byte};'
        body+='spu_step(s,t,ls);assert(spu_word(t.gpr[4],0)==42);t.pc=0x100;ls[0x100]=2;ls[0x101]=ls[0x102]=ls[0x103]=0;fault([&]{spu_step(s,t,ls);});fault([&]{spu_prepare(s,t,ls);});'
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=(image,),spu_overlays=(((0x1200,overlay),),))

    def test_overlapping_static_versions_and_dynamic_word_select_exact_semantics(self):
        first=(0x81<<23)|(11<<7)|3
        second=(0x81<<23)|(22<<7)|3
        payload=first.to_bytes(4,'big')
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,4,4,4),))
        body='(void)call;NativeSpu::Thread t;t.entry=0x100;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();'
        for i,byte in enumerate(payload):body+=f'ls[{0x100+i}]={byte};'
        body+='spu_prepare(s,t,ls);spu_step(s,t,ls);assert(t.pc==0x104 && spu_word(t.gpr[3],0)==11);'
        for word,value in ((second,22),(first,11)):
            for i,byte in enumerate(word.to_bytes(4,'big')):body+=f'ls[{0x100+i}]={byte};'
            body+=f't.pc=0x100;spu_step(s,t,ls);assert(t.pc==0x104 && spu_word(t.gpr[3],0)=={value});'
        # An IL missing from the static versions executes through the shared interpreter.
        unknown=(0x81<<23)|(33<<7)|3
        for i,byte in enumerate(unknown.to_bytes(4,'big')):body+=f'ls[{0x100+i}]={byte};'
        body+='t.pc=0x100;spu_step(s,t,ls);assert(t.pc==0x104 && spu_word(t.gpr[3],0)==33);'
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=(image,),spu_overlays=(((0x100,second.to_bytes(4,'big')),),))

    def test_dma_copies_both_directions_and_consumes_tag_status(self):
        self.run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 mem.store(0x10100,8,UINT64_C(0x0123456789abcdef));
 spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,17,0);
 spu_write_channel(s,t,ls,18,0x10100);spu_write_channel(s,t,ls,19,16);spu_write_channel(s,t,ls,20,31);
 spu_write_channel(s,t,ls,21,0x40);
 assert(spu_load_word(ls+0x200)==0x01234567 && spu_load_word(ls+0x204)==0x89abcdef);
 fault([&]{spu_read_channel(s,t,24);});
 spu_write_channel(s,t,ls,22,0x80000000);spu_write_channel(s,t,ls,23,2);
 assert(spu_read_channel(s,t,24)==0x80000000);fault([&]{spu_read_channel(s,t,24);});
 ls[0x200]=0xaa;spu_write_channel(s,t,ls,18,0x10200);spu_write_channel(s,t,ls,21,0x21);
 assert(mem.load(0x10200,4)==0xaa234567 && mem.load(0x10100,4)==0x01234567);
 spu_write_channel(s,t,ls,16,0x3fff8);fault([&]{spu_write_channel(s,t,ls,21,0x40);});
 spu_write_channel(s,t,ls,16,0x201);fault([&]{spu_write_channel(s,t,ls,21,0x40);});
 fault([&]{spu_write_channel(s,t,ls,21,0xd0);});
''')

    def test_dma_list_get_put_advance_local_address_and_reject_unsupported(self):
        # Handbook v1.1 19.4.4 (PDF p.530): {LTS,LEAL} elements; LS continues after each element,
        # a short (1/2/4/8 byte) element resumes at the next quadword.
        self.run_native(r"""
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 for(unsigned i=0;i<0x300;i+=4)mem.store(0x10100+i,4,0xa0000000u+i);
 auto put=[&](unsigned at,std::uint32_t v){for(unsigned i=0;i<4;++i)ls[at+i]=v>>(24-8*i);};
 put(0x1000,16);put(0x1004,0x10100);put(0x1008,4);put(0x100c,0x10200);put(0x1010,32);put(0x1014,0x10300);
 auto setup=[&](unsigned local,unsigned list,unsigned size){spu_write_channel(s,t,ls,16,local);spu_write_channel(s,t,ls,17,0);
   spu_write_channel(s,t,ls,18,list);spu_write_channel(s,t,ls,19,size);spu_write_channel(s,t,ls,20,5);};
 setup(0x200,0x1000,24);spu_write_channel(s,t,ls,21,0x44);
 assert(spu_load_word(ls+0x200)==0xa0000000u && spu_load_word(ls+0x20c)==0xa000000cu);
 assert(spu_load_word(ls+0x210)==0xa0000100u && spu_load_word(ls+0x214)==0);   // 4 bytes only
 assert(spu_load_word(ls+0x220)==0xa0000200u && spu_load_word(ls+0x23c)==0xa000021cu && spu_load_word(ls+0x240)==0);
 for(unsigned i=0;i<0x40;++i)ls[0x400+i]=0x40+i;
 put(0x1100,8);put(0x1104,0x10380);put(0x1108,16);put(0x110c,0x10390);
 setup(0x400,0x1100,16);spu_write_channel(s,t,ls,21,0x26);   // putlf: first 8 bytes, then LS 0x410
 assert(mem.load(0x10380,4)==0x40414243u && mem.load(0x10384,4)==0x44454647u && mem.load(0x10388,4)==0xa0000288u);
 assert(mem.load(0x10390,4)==0x50515253u && mem.load(0x1039c,4)==0x5c5d5e5fu);
 spu_write_channel(s,t,ls,22,1u<<5);spu_write_channel(s,t,ls,23,2);assert(spu_read_channel(s,t,24)==(1u<<5));
 // A stall-and-notify flag on the last element does not stall (no elements remain; RPCS3 do_list_transfer).
 put(0x1200,0x80000010u);put(0x1204,0x10100);setup(0x200,0x1200,8);spu_write_channel(s,t,ls,21,0x44);
 assert(spu_channel_count(s,t,25)==0 && !t.stall_mask);
 put(0x1200,0x00010010u);fault([&]{spu_write_channel(s,t,ls,21,0x44);});
 put(0x1200,16);put(0x1204,0x10104);fault([&]{spu_write_channel(s,t,ls,21,0x44);});   // LS/EA quadword offset differ
 setup(0x200,0x1004,8);fault([&]{spu_write_channel(s,t,ls,21,0x44);});
 ls[0x200]=0x77;setup(0x200,0x1000,0);spu_write_channel(s,t,ls,21,0x44);assert(ls[0x200]==0x77);   // empty list
 setup(0x200,0x1000,0x4008);fault([&]{spu_write_channel(s,t,ls,21,0x44);});
""")

    def test_dma_thread_group_window_reaches_peer_local_storage_and_signals(self):
        # LV2 window 0xf0000000 + spu_num*0x100000 (RPCS3 SPUThread.cpp:2066-2127).
        self.run_native(r"""
 (void)call;NativeSpu::Group g;g.threads.resize(2);g.threads[0].number=3;g.threads[1].number=0;
 g.context.assign(2*0x40000,0);s.spu.groups.push_back(g);
 auto& group=s.spu.groups.back();auto& a=group.threads[0];auto& b=group.threads[1];
 auto* la=group.context.data();auto* lb=group.context.data()+0x40000;
 for(unsigned i=0;i<16;++i)lb[0x7700+i]=0x10+i;
 auto dma=[&](unsigned local,std::uint64_t ea,unsigned size,unsigned cmd){spu_write_channel(s,a,la,16,local);
   spu_write_channel(s,a,la,17,ea>>32);spu_write_channel(s,a,la,18,std::uint32_t(ea));spu_write_channel(s,a,la,19,size);
   spu_write_channel(s,a,la,20,3);spu_write_channel(s,a,la,21,cmd);};
 dma(0x22640,0xf0007700,16,0x40);                       // spu_num 0 = second thread
 assert(spu_load_word(la+0x22640)==0x10111213u && spu_load_word(la+0x2264c)==0x1c1d1e1fu);
 for(unsigned i=0;i<32;++i)la[0x300+i]=0x80+i;
 dma(0x300,0xf0000000u+0x3ff00,32,0x20);assert(lb[0x3ff00]==0x80 && lb[0x3ff1f]==0x9f);
 dma(0x300,0xf0300000u+0x40,16,0x40);assert(la[0x300]==0);   // own window (spu_num 3)
 la[0x40c]=0;la[0x40d]=0;la[0x40e]=0;la[0x40f]=5;b.signal_config=1;b.signals[0]=2;
 dma(0x40c,0xf005400c,4,0x20);assert(b.signal_ready[0] && b.signals[0]==7);
 dma(0x40c,0xf005c00c,4,0x20);assert(b.signal_ready[1] && b.signals[1]==5);
 fault([&]{dma(0x400,0xf005400c,4,0x20);});               // quadword offsets differ
 fault([&]{dma(0x300,0xf0200000,16,0x40);});              // no spu_num 2
 fault([&]{dma(0x300,0xf0000000u+0x3fff0,32,0x40);});     // crosses end of LS
 fault([&]{dma(0x40c,0xf005410c,4,0x20);});               // other MMIO register
 fault([&]{dma(0x300,0xe0000000,16,0x40);});              // raw SPU MMIO
 NativeSpu::Thread lone;std::vector<std::uint8_t> storage(0x40000);
 spu_write_channel(s,lone,storage.data(),16,0);spu_write_channel(s,lone,storage.data(),17,0);
 spu_write_channel(s,lone,storage.data(),18,0xf0000000);spu_write_channel(s,lone,storage.data(),19,16);
 spu_write_channel(s,lone,storage.data(),20,1);fault([&]{spu_write_channel(s,lone,storage.data(),21,0x40);});
""")

    def test_atomic_dma_detects_alias_writes_including_aba(self):
        self.run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 mem.regions.push_back({0x20000,6,mem.regions.front().bytes});
 mem.store(0x10100,4,0x12345678);
 spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x10100);
 spu_write_channel(s,t,ls,21,0xd0);assert(spu_read_channel(s,t,27)==4);
 assert(spu_load_word(ls+0x200)==0x12345678);
 mem.store(0x20100,4,0xaabbccdd);mem.store(0x20100,4,0x12345678);
 ls[0x200]=0x55;spu_write_channel(s,t,ls,21,0xb4);
 assert(spu_read_channel(s,t,27)==1 && mem.load(0x10100,4)==0x12345678);
 spu_write_channel(s,t,ls,21,0xd0);assert(spu_read_channel(s,t,27)==4);
 ls[0x200]=0x99;spu_write_channel(s,t,ls,21,0xb4);
 assert(spu_read_channel(s,t,27)==0 && mem.load(0x20100,4)==0x99345678);
 spu_write_channel(s,t,ls,21,0xb4);assert(spu_read_channel(s,t,27)==1);
 fault([&]{spu_read_channel(s,t,27);});
''')

    def test_reservation_event_latch_mask_ack_and_read_count(self):
        self.run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 auto blocked=[&]{bool hit=false;try{spu_read_channel(s,t,0);}catch(const SpuBlocked&){hit=true;}assert(hit);};
 // Ordinary threads start with interrupts disabled. Event masks do not enable them.
 assert(spu_read_channel(s,t,13)==0);
 spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x10100);
 spu_write_channel(s,t,ls,21,0xd0);blocked();
 mem.store(0x10100,4,77);spu_collect_events(s,t);
 assert(t.event_status==0x400 && !t.event_ready);blocked();
 spu_write_channel(s,t,ls,1,0x400);assert(spu_read_channel(s,t,11)==0x400);
 assert(spu_read_channel(s,t,13)==0);
 assert(spu_read_channel(s,t,0)==0x400 && !t.waiting_event);blocked();
 // Reading consumes the channel count, not the pending event bit.
 spu_write_channel(s,t,ls,1,0x400);assert(spu_read_channel(s,t,0)==0x400);
 spu_write_channel(s,t,ls,2,0x400);assert(t.event_status==0);blocked();
 spu_write_channel(s,t,ls,21,0xd0);mem.store(0x10100,4,88);
 spu_write_channel(s,t,ls,1,0);assert(t.event_ready && spu_read_channel(s,t,0)==0);
 spu_write_channel(s,t,ls,1,0x400);assert(spu_read_channel(s,t,0)==0x400);
 fault([&]{spu_write_channel(s,t,ls,1,0x10);});assert(t.event_mask==0x400);
''')

    def test_static_spu_wait_releases_execution_and_wakes_on_real_write(self):
        words=[(0x81<<23)|(1<<7)|3,(0xd<<21)|7,(0x81<<23)|(42<<7)|3,0x32000000]
        payload=b''.join(w.to_bytes(4,'big') for w in words)
        image=ElfImage(payload,32,'big',0,2,23,0x100,(Segment(0,1,7,0,0x100,len(payload),len(payload),4),))
        body=r'''
 (void)call;s.pc=0x1234;s.r[3]=0x9876;
 NativeSpu::Group group;group.state=6;group.context.resize(0x40000);group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups.back();auto& t=g.threads[0];auto* ls=g.context.data();
 t.entry=0x100;
'''
        for i,byte in enumerate(payload):body+=f'ls[{0x100+i}]={byte};'
        body+=r'''
 spu_prepare(s,t,ls);spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x10100);
 spu_write_channel(s,t,ls,21,0xd0);spu_write_channel(s,t,ls,1,0x400);
 assert(spu_run(s,8) && t.waiting_event && t.steps==1 && t.pc==0x104);
 assert(!spu_run(s,8) && t.steps==1);
 mem.store(0x10100,4,123);
 assert(spu_run(s,2) && !t.waiting_event && t.steps==3 && t.pc==0x10c);
 assert(spu_word(t.gpr[7],0)==0x400 && spu_word(t.gpr[3],0)==42);
 assert(s.pc==0x1234 && s.r[3]==0x9876);
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=(image,))
