"""SPU ISA v1.2 fixed vectors for SPRs, interrupt branches and control flow."""
import shutil
import unittest

from anyps3.spu import lift
from anyps3.spu_scan import direct_flow, spans
from anyps3.elf import ElfImage, Segment
from tests import test_runtime_objects


def instruction(word):
    return '{[[maybe_unused]] auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};' + lift(0x100, word) + '}\n'


class SpuScanTests(unittest.TestCase):
    def test_flow_does_not_fall_through_unconditional_branch_or_unknown_stop(self):
        names = {0x190: 'BR', 0: 'STOP', 0x201: 'NOP', 0x1a8: 'BI'}
        # BR +8, skipped STOP 0, NOP, indirect return; trailing data is unresolved.
        words = {0x100: 0x32000100, 0x104: 0, 0x108: 0x40200000,
                 0x10c: 0x35000000, 0x110: 0x00000203}
        visited, _, edges, indirect = direct_flow(words, [0x100], names)
        self.assertEqual(visited, {0x100, 0x108, 0x10c})
        self.assertEqual(indirect, [0x10c])
        self.assertEqual(edges, [])
        self.assertEqual(spans([12, 0, 4, 4, 16]), [['0x0', '0x4'], ['0xc', '0x10']])

    def test_flow_keeps_call_return_conditional_paths_and_missing_edges(self):
        names = {0x198: 'BRSL', 0x100: 'BRZ', 0: 'STOP'}
        words = {0x100: 0x33000200, 0x104: 0x20000000, 0x108: 0,
                 0x110: 0x00000102}
        visited, _, edges, _ = direct_flow(words, [0x100], names)
        self.assertEqual(visited, {0x100, 0x104, 0x108, 0x110})
        self.assertEqual(edges, [])
        visited, _, edges, _ = direct_flow({0x100: 0x33000200}, [0x100], names)
        self.assertEqual(edges, [(0x100, 0x104), (0x100, 0x110)])


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuGapNativeTests(unittest.TestCase):
    def native(self, body, images=()):
        test_runtime_objects.RuntimeObjectsTests().run_native(body, spu_images=images)

    def test_cell_has_no_sprs_all_reads_zero_and_writes_leave_state_unchanged(self):
        body = '(void)call;NativeSpu::Thread t;t.pc=0x100;t.fpscr.fill(0xa5);'
        for sa in (0, 1, 73, 127):
            for target in (0, 3, 127):
                body += f't.gpr[{target}].fill(0xff);'
                body += instruction((0x10c << 21) | (sa << 7) | target)
                body += f'for(auto b:t.gpr[{target}]){{assert(b==255);}}'
                body += instruction((0xc << 21) | (sa << 7) | target)
                body += f'assert(t.gpr[{target}]==SpuRegister{{}} && t.pc==0x104);'
        body += 'for(auto b:t.fpscr)assert(b==0xa5);assert(!t.interrupts_enabled && !t.srr0_valid);'
        self.native(body)

    def test_bisled_link_alias_count_and_branch_interrupt_flags(self):
        body = r'''
 (void)call;NativeSpu::Thread t;t.event_status=0x400;t.event_mask=0x400;
 // Pending bit without channel count: no branch, but the link is still written.
 spu_word(t.gpr[3],0,0x1237);t.gpr[3][15]=255;
'''
        body += instruction((0x1ab << 21) | (1 << 18) | (3 << 7) | 3)
        body += 'assert(t.pc==0x104 && !t.interrupts_enabled && spu_word(t.gpr[3],0)==0x104);for(unsigned i=4;i<16;++i)assert(!t.gpr[3][i]);'
        body += 't.event_ready=true;spu_word(t.gpr[3],0,0x1237);'
        body += instruction((0x1ab << 21) | (1 << 18) | (3 << 7) | 3)
        body += 'assert(t.pc==0x1234 && t.interrupts_enabled && t.event_ready && spu_word(t.gpr[3],0)==0x104);'
        body += 'spu_word(t.gpr[3],0,0x4567);'
        body += instruction((0x1ab << 21) | (2 << 18) | (3 << 7) | 3)
        body += 'assert(t.pc==0x4564 && !t.interrupts_enabled && t.event_ready);'
        # Link wraps at the LS boundary, including a destination/source alias.
        body += '{auto& g=t.gpr;SpuRegister out{};' + lift(0x3fffc, (0x1a9 << 21) | (3 << 7) | 3) + '}'
        body += 'assert(t.pc==0x104 && spu_word(t.gpr[3],0)==0);'
        self.native(body)

    def test_conditional_branch_flags_change_only_when_taken(self):
        body = '(void)call;NativeSpu::Thread t;spu_word(t.gpr[2],0,0x1237);'
        for op, taken, untaken in ((0x128, 0, 1), (0x129, 1, 0),
                                    (0x12a, 0x12340000, 0x12340001),
                                    (0x12b, 0x12340001, 0x12340000)):
            body += f'spu_word(t.gpr[3],0,UINT32_C({untaken}));t.interrupts_enabled=false;'
            body += instruction((op << 21) | (3 << 18) | (2 << 7) | 3)
            body += 'assert(t.pc==0x104 && !t.interrupts_enabled);'
            body += f'spu_word(t.gpr[3],0,UINT32_C({taken}));'
            body += instruction((op << 21) | (1 << 18) | (2 << 7) | 3)
            body += 'assert(t.pc==0x1234 && t.interrupts_enabled);'
            body += instruction((op << 21) | (2 << 18) | (2 << 7) | 3)
            body += 'assert(!t.interrupts_enabled);'
            body += 'fault([&]{' + instruction((op << 21) | (3 << 18) | (2 << 7) | 3) + '});'
        self.native(body)

    def test_srr0_channels_require_sync_c_and_iret_ignores_ra(self):
        body = r'''
 (void)call;NativeSpu::Thread t;std::array<std::uint8_t,16> ls{};
 fault([&]{spu_interrupt_return(t);});fault([&]{spu_read_channel(s,t,15);});
 assert(spu_channel_count(s,t,14)==1 && spu_channel_count(s,t,15)==1);
 spu_word(t.gpr[3],0,0x1237);spu_word(t.gpr[2],0,0xdead);
'''
        body += instruction((0x10d << 21) | (14 << 7) | 3).replace('ls,', 'ls.data(),')
        body += 'fault([&]{spu_interrupt_return(t);});'
        body += instruction(0x00400000)  # SYNC C=0 does not commit SRR0.
        body += 'fault([&]{spu_interrupt_return(t);});'
        body += instruction(0x00500000)  # SYNCC.
        body += instruction((0xd << 21) | (15 << 7) | 4)
        body += 'assert(spu_word(t.gpr[4],0)==0x1237 && spu_word(t.gpr[4],1)==0);'
        body += instruction((0x1aa << 21) | (1 << 18) | (2 << 7))
        body += 'assert(t.pc==0x1234 && t.interrupts_enabled && spu_read_channel(s,t,13)==1);'
        body += 'fault([&]{spu_write_channel(s,t,ls.data(),14,0x200);});'
        self.native(body)

    def test_real_aot_interrupt_preempts_blocked_read_and_returns_to_retry(self):
        # Handler at zero reads/acks the real reservation-lost event, then IRETE.
        # Main waits on signal1, so a signal wait must not hide the interrupt.
        words = {0: 0x01a00005, 4: 0x21a00105, 8: 0x35440000,
                 0x100: 0x01a00183, 0x104: 0x00000100}
        payload = bytearray(0x108)
        for pc, word in words.items():
            payload[pc:pc + 4] = word.to_bytes(4, 'big')
        image = ElfImage(bytes(payload), 32, 'big', 0, 2, 23, 0x100,
                         (Segment(0, 1, 7, 0, 0, len(payload), len(payload), 4),))
        body = r'''
 (void)call;NativeSpu::Group group;group.state=6;group.count=1;
 group.context.resize(0x40000);group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups[0];auto& t=g.threads[0];
 auto* ls=g.context.data();t.entry=0x100;
'''
        for pc, word in words.items():
            body += f'ls[{pc}]=0x{word >> 24:02x};ls[{pc + 1}]=0x{(word >> 16)&255:02x};ls[{pc + 2}]=0x{(word >> 8)&255:02x};ls[{pc + 3}]=0x{word&255:02x};'
        body += r'''
 spu_prepare(s,t,ls);t.decrementer_running=false;spu_write_channel(s,t,ls,16,0x200);spu_write_channel(s,t,ls,18,0x10100);
 spu_write_channel(s,t,ls,21,0xd0);spu_write_channel(s,t,ls,1,0x400);
 spu_branch_interrupts(t,1);
 assert(!spu_run(s,2) && t.waiting_signal==1 && t.pc==0x100 && t.steps==0);
 mem.store(0x10100,4,77);
 assert(spu_run(s,3) && t.pc==0x100 && t.srr0==0x100 && t.steps==3);
 assert(spu_word(t.gpr[5],0)==0x400 && !t.event_status && !t.event_ready && t.interrupts_enabled);
 assert(!spu_run(s,2) && t.waiting_signal==1 && t.pc==0x100);
 t.signals[0]=0x12345678;t.signal_ready[0]=true;
 assert(spu_run(s,2) && t.pc==0x108 && t.steps==5 && spu_word(t.gpr[3],0)==0x12345678);
'''
        self.native(body, (image,))

    def test_decrementer_ticks_wrap_events_freeze_and_thread_option(self):
        self.native(r'''
 (void)call;NativeSpu::Thread t;std::array<std::uint8_t,16> ls{};
 spu_reset_decrementer(t,100);assert(t.decrementer==0 && t.decrementer_running);
 t.decrementer=3;t.event_mask=0x20;
 spu_update_decrementer(t,103);assert(t.decrementer==0 && !t.event_ready);
 spu_update_decrementer(t,104);assert(t.decrementer==0xffffffff && t.event_status==0x20 && t.event_ready);
 t.event_status=0;t.event_ready=false;
 spu_update_decrementer(t,UINT64_C(0x100000067));
 assert(t.decrementer==0 && !t.event_ready); // exactly back to zero, no underflow yet
 spu_update_decrementer(t,UINT64_C(0x200000069));
 assert(t.decrementer==0xfffffffe && t.event_ready); // more than one full wrap
 t.decrementer_running=false;t.event_mask=0;t.event_ready=false;t.event_status=0;
 spu_write_channel(s,t,ls.data(),7,0x80000000);
 assert(t.decrementer_running); // old value was negative, so no new sign transition
 t.decrementer_running=false;t.decrementer=0;t.event_status=0;t.event_ready=false;
 spu_write_channel(s,t,ls.data(),7,0x80000000);
 assert(t.event_status==0x20 && !t.event_ready);
 spu_write_channel(s,t,ls.data(),1,0x20);assert(t.event_ready);
 spu_read_channel(s,t,0);spu_write_channel(s,t,ls.data(),1,0);
 spu_write_channel(s,t,ls.data(),2,0x20);assert(!t.decrementer_running && !t.event_status);
 const auto frozen=spu_read_channel(s,t,8);
 spu_update_decrementer(t,UINT64_MAX);assert(spu_read_channel(s,t,8)==frozen);
 t.options=2;spu_reset_decrementer(t,UINT64_C(0x123456789));
 assert(t.decrementer==0xdcba9876 && t.decrementer_running);
''')

    def test_masked_decrementer_is_accounted_lazily_and_exactly(self):
        # TM (0x20) masked: the per-instruction interrupt sample must not advance the decrementer; reading it
        # (ch8), acknowledging (ch2) or enabling TM (ch1) accounts the whole interval exactly once.
        self.native(r'''
 (void)call;NativeSpu::Thread t;std::array<std::uint8_t,16> ls{};
 const auto past=timebase_ticks()-1000000;   // decrementer started 1e6 ticks ago with value 10
 t.decrementer=10;t.decrementer_tick=past;t.decrementer_running=true;t.event_mask=0x2;t.interrupts_enabled=true;
 assert(!spu_external_event(s,t));
 assert(t.decrementer==10 && t.decrementer_tick==past && !(t.event_status&0x20));
 const auto value=spu_read_channel(s,t,8);   // full accounting: underflowed, wrapped modulo 2^32
 assert(value==t.decrementer && value>=UINT32_C(0xfff00000) && (t.event_status&0x20) && !t.event_ready);
 spu_write_channel(s,t,ls.data(),1,0x22);assert(t.event_ready);   // enabling TM exposes the latched event
 // Acknowledge first accounts the interval, so the underflow bit is cleared, not raised afterwards.
 t.event_mask=0x2;t.event_ready=false;t.event_status=0;t.decrementer=10;t.decrementer_tick=timebase_ticks()-1000000;
 spu_write_channel(s,t,ls.data(),2,0x20);assert(!(t.event_status&0x20));
 spu_write_channel(s,t,ls.data(),1,0x22);assert(!t.event_ready && !(t.event_status&0x20));
 // TM enabled: the sample itself accounts the time and raises the interrupt condition.
 t.decrementer=10;t.decrementer_tick=timebase_ticks()-1000000;t.decrementer_running=true;t.event_status=0;t.event_ready=false;
 assert(spu_external_event(s,t) && (t.event_status&0x20));
''')

    def test_direct_branch_fallthrough_and_link_wrap_at_ls_boundary(self):
        body='(void)call;NativeSpu::Thread t;spu_word(t.gpr[3],0,1);'
        for word in (0x20000003, 0x22000003, 0x33000003, 0x31000003):
            body += '{[[maybe_unused]] auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};' + lift(0x3fffc, word) + '}'
            body += 'assert(t.pc<0x40000);'
            if word in (0x33000003, 0x31000003):
                body += 'assert(spu_word(t.gpr[3],0)==0);'
            else:
                body += 'assert(t.pc==0);'
        self.native(body)

    def test_unknown_managed_stops_preserve_mailboxes_join_and_status(self):
        self.native(r'''
 (void)call;NativeSpu::Group group;group.state=6;group.count=1;group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& g=s.spu.groups[0];auto& t=g.threads[0];
 t.pc=0x100;t.out_mailbox=0xabcdef01;t.out_mailbox_full=true;t.in_mailbox={77};
 for(unsigned code:{0u,0x10u,0x203u,0x3fffu}){
     fault([&]{spu_stop(s,t,code);});
     assert(t.pc==0x100 && !t.stopped && !t.exit_status_ready && t.out_mailbox_full);
     assert(t.out_mailbox==0xabcdef01 && t.in_mailbox==std::vector<std::uint32_t>{77});
     assert(g.state==6 && !g.join_cause && !g.exit_status);
 }
''')
