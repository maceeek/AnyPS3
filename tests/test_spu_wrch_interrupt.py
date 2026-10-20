"""Inline MFC-parameter WRCH (channels 16-20, 22) keeps the generic SPU interrupt sampling boundary.

The exact interpreter samples SPU interrupts after every channel instruction; the generic native path (spu_write_channel sets
irq_next=0, the instruction ends its block) does so at the next block entry.  The inline form stores the parameter without a call
and keeps the instruction inside its block, so with interrupts ENABLED it must reproduce the boundary itself: sample right after the
instruction, leave the native call if the handler was entered (registers flushed, retired count exact), otherwise restart the
sampling interval.  With interrupts disabled (the common case) the instruction stays a store plus one not-taken branch.

Independent oracle: an asynchronous event that arrives after the last sampling point is emulated by calling the native function
directly (spu_native_enter, no call-entry sample) with the event already pending and irq_next far away.  The expected state is the
exact interpreter (trace mode, interrupts disabled) executing exactly K instructions, followed by one interrupt sample
(spu_take_interrupt) with the event pending: pc, SRR0, retired steps, every GPR, the MFC parameters and the interrupt flag must be equal,
for each position K of the first WRCH, for each channel kind, with and without ANYPS3_SPU_PROFILE (the generic call inside the template).
"""
import shutil
import unittest

from tests import test_spu_block
from tests.spu_asm import Asm, ri16

BASE = 0x100
EXIT = 0x8000   # BRA target outside the translated image: the native call ends there


def program(prefix, channel, base):
    """`prefix` ALU instructions with distinct live registers, IL, WRCH <channel>, AI, WRCH 17, AI, WRCH 22, 4 AI, BRA EXIT."""
    a = Asm(base)
    for i in range(prefix):
        a.ai(10 + i, 10 + i, 3 + i)          # r10.. = distinct values (dirty registers cached in vectors)
    a.il(3, 0x1234)
    a.wrch(channel, 3)                        # the first WRCH: the pending event is taken right after it
    a.ai(4, 3, 7)
    a.wrch(17, 4)
    a.ai(5, 4, 9)
    a.wrch(22, 5)
    for i in range(4):
        a.ai(20 + i, 20 + i, 1)
    a.emit(ri16(0x060, EXIT >> 2, 0))         # BRA
    return a.finish()


BODY = r'''
 struct State {std::array<SpuRegister,128> gpr;std::uint32_t pc,srr0;bool srr0_valid,interrupts_enabled;std::uint64_t steps;std::array<std::uint32_t,5> dma;std::uint32_t tag_mask;};
 auto snapshot=[&](){return State{t.gpr,t.pc,t.srr0,t.srr0_valid,t.interrupts_enabled,t.steps,t.dma,t.tag_mask};};
 auto reset=[&](unsigned entry,bool interrupts,bool pending){
  std::fill(ls,ls+0x40000,0);std::copy(image,image+image_size,ls+0x100);
  t.gpr={};t.pc=entry;t.steps=0;t.reloc_delta=0;t.relocated_entries=0;t.interpreted_instructions=0;t.stopped=false;t.dma={};t.tag_mask=0;
  t.srr0=0;t.srr0_valid=false;t.srr0_pending=false;t.irq_next=0;t.event_status=t.event_mask=0;t.event_ready=false;t.interrupts_enabled=interrupts;
  if(pending){t.event_mask=t.event_status=0x10;t.event_ready=true;}
 };
 // oracle: the exact interpreter executes `count` instructions with interrupts disabled, then one sample with the event pending
 auto oracle=[&](unsigned entry,unsigned count){
  reset(entry,false,false);
  spu_step(s,t,ls,count,UINT64_MAX,true);assert(t.steps==count);
  t.event_mask=t.event_status=0x10;t.event_ready=true;t.interrupts_enabled=true;spu_take_interrupt(s,t);
  return snapshot();
 };
 // native: the whole function in one call; the event is pending but irq_next is far away, so only the WRCH boundary can take it
 auto start=[&](bool profile){
  t.irq_next=1000000;spu_profile_on=profile;
  t.block_budget=1000;t.block_limit=UINT64_MAX;t.block_trace=false;t.block_control_exit=false;t.block_exact=false;
 };
 auto same=[&](const State& a,const State& b){return a.gpr==b.gpr && a.pc==b.pc && a.srr0==b.srr0 && a.srr0_valid==b.srr0_valid && a.interrupts_enabled==b.interrupts_enabled
  && a.steps==b.steps && a.dma==b.dma && a.tag_mask==b.tag_mask;};
 const bool profile_default=spu_profile_on;
 for(const auto& v:variants){
  // 1. the pending event is taken right after the first WRCH (instruction number v.first, 1-based): pc, SRR0, steps, all GPRs, MFC parameters
  const auto expected=oracle(v.entry,v.first);
  assert(expected.pc==0 && expected.srr0==v.entry+4*v.first && expected.srr0_valid && !expected.interrupts_enabled && expected.steps==v.first);
  for(const bool profile:{false,true}){
   reset(v.entry,true,true);start(profile);
   try{assert(spu_native_enter(s,t,ls));}catch(...){spu_profile_on=profile_default;throw;}
   spu_profile_on=profile_default;
   const auto actual=snapshot();
   if(!same(expected,actual)){
    std::fprintf(stderr,"MISMATCH entry=%x first=%u profile=%d: pc %x/%x srr0 %x/%x steps %llu/%llu ie %d/%d\n",v.entry,v.first,int(profile),actual.pc,expected.pc,actual.srr0,expected.srr0,
      (unsigned long long)actual.steps,(unsigned long long)expected.steps,int(actual.interrupts_enabled),int(expected.interrupts_enabled));
    throw std::runtime_error("native inline WRCH did not sample the interrupt at the instruction boundary");
   }
  }
  // 2. interrupts enabled, nothing pending: the program runs to its BRA and the sampling interval restarts after the last WRCH
  //    (generic path: irq_next=0, then the next block entry sets steps+executed+SPU_IRQ_INTERVAL)
  for(const bool profile:{false,true}){
   reset(v.entry,true,false);start(profile);
   try{assert(spu_native_enter(s,t,ls));}catch(...){spu_profile_on=profile_default;throw;}
   spu_profile_on=profile_default;
   assert(t.pc==0x8000 && t.steps==v.total && t.interrupts_enabled && !t.srr0_valid);
   assert(t.irq_next==v.last+SPU_IRQ_INTERVAL);
   assert((v.channel_index>=5 || t.dma[v.channel_index]==0x1234) && t.dma[1]==0x1234+7 && t.tag_mask==0x1234+7+9);
  }
  // 3. interrupts disabled: nothing is sampled, the pending event stays pending, irq_next is untouched
  reset(v.entry,false,true);start(false);
  assert(spu_native_enter(s,t,ls));
  assert(t.pc==0x8000 && t.steps==v.total && !t.interrupts_enabled && t.event_ready && t.irq_next==1000000 && !t.srr0_valid);
 }
 std::printf("PASS %u variants\n",unsigned(sizeof(variants)/sizeof(variants[0])));
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuWrchInterruptTests(unittest.TestCase):
    def test_pending_event_is_sampled_after_each_inline_mfc_parameter_wrch(self):
        # One image, one compile: variants at 0x100 + n*0x100 (prefix length x first-WRCH channel; channel 22 = the tag mask).
        variants, image = [], {}
        for n, (prefix, channel) in enumerate([(p, c) for p in (0, 1, 2, 5, 9) for c in (16, 18, 20)] + [(3, 22), (0, 22)]):
            entry = BASE + 0x100 * n
            words = program(prefix, channel, entry)
            image.update({entry + 4 * i: w for i, w in enumerate(words)})
            first = prefix + 2
            variants.append((entry, first, len(words), first + 4, channel - 16 if channel != 22 else 5))
        high = max(image) + 4
        for pc in range(BASE, high, 4):
            image.setdefault(pc, 0x40200000)     # NOP filler between the variants
        data = b''.join(image[pc].to_bytes(4, 'big') for pc in range(BASE, high, 4))
        table = ','.join(f'{{{e}u,{f}u,{t}u,{l}u,{c}u}}' for e, f, t, l, c in variants)
        body = (f'const std::uint8_t image[]={{{",".join(str(b) for b in data)}}};const unsigned image_size={len(data)};'
                f'struct Variant {{unsigned entry,first,total,last,channel_index;}};const Variant variants[]={{{table}}};' + BODY)
        test_spu_block.SpuBlockTests().native(image, body)


if __name__ == '__main__':
    unittest.main()
