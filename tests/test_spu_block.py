"""Native SPU threaded-block regressions with fixed independent results."""
import os
import shutil
import unittest
from unittest import mock
from anyps3.elf import ElfImage, Segment
from anyps3.spu import generate_parts
from tests import test_runtime_objects


def image(words):
    lo, hi=min(words),max(words)+4
    payload=bytearray(hi-lo)
    for pc,word in words.items():payload[pc-lo:pc-lo+4]=word.to_bytes(4,'big')
    return ElfImage(bytes(payload),32,'big',0,2,23,lo,
                    (Segment(0,1,7,0,lo,len(payload),len(payload),4),))


def setup(img):
    body=r'''
 (void)call;NativeSpu::Group group;group.state=6;group.context.resize(0x40000);group.threads.emplace_back();
 s.spu.groups.push_back(std::move(group));auto& group_ref=s.spu.groups[0];auto& t=group_ref.threads[0];
 auto* ls=group_ref.context.data();
'''
    body+=f't.entry=0x{img.entry:x};'
    body+=''.join(f'ls[{img.entry+i}]={b};' for i,b in enumerate(img.data))
    return body+'spu_prepare(s,t,ls);t.decrementer_running=false;\n'


def _ri10(op,value,ra,rt):return (op<<24)|((value&0x3ff)<<14)|(ra<<7)|rt
def _ri16(op,value,rt):return (op<<23)|((value&0xffff)<<7)|rt


def chained_recursion():
    """HIRES7 chained calls: main (0x100) and A (0x200) in native function 0, B (0x1100) in native function 1. B and A call each
    other with BRSL (r0 saved on a stack at r1) until the depth counter r10 reaches 0: 80 nested guest calls, deeper than the
    chaining bound. Hand-counted: B with r10!=0 after the decrement retires 9 + callee, the last B 4, A 7 + callee."""
    ai=lambda rt,ra,v:_ri10(0x1c,v,ra,rt);il=lambda rt,v:_ri16(0x081,v,rt);stqd=lambda rt,ra,off:_ri10(0x24,off//16,ra,rt)
    lqd=lambda rt,ra,off:_ri10(0x34,off//16,ra,rt);bi=lambda ra:(0x1a8<<21)|(ra<<7)
    rel=lambda op,rt,pc,to:_ri16(op,(to-pc)//4,rt)
    w={0x100:il(10,40),0x104:il(20,0),0x108:il(21,0),0x10c:(0x21<<25)|(0x3ff00<<7)|1,0x110:rel(0x066,0,0x110,0x1100),
       0x114:ai(21,21,1),0x118:rel(0x064,0,0x118,0x118)}
    a=[ai(20,20,1),stqd(0,1,0),ai(1,1,-16),rel(0x066,0,0x20c,0x1100),ai(1,1,16),lqd(0,1,0),bi(0)]
    b=[ai(10,10,-1),ai(20,20,2),rel(0x040,10,0x1108,0x1120),stqd(0,1,0),ai(1,1,-16),rel(0x066,0,0x1114,0x200),ai(1,1,16),
       lqd(0,1,0),bi(0)]
    w.update({0x200+4*i:x for i,x in enumerate(a)});w.update({0x1100+4*i:x for i,x in enumerate(b)})
    return w


def _rr(op,rb,ra,rt):return (op<<21)|(rb<<14)|(ra<<7)|rt
def _rrr(op,rt,rb,ra,rc):return (op<<28)|(rt<<21)|(rb<<14)|(ra<<7)|rc


def _words(b):return [int.from_bytes(b[i:i+4],'big') for i in range(0,16,4)]
def _bytes(w):return b''.join((x&0xffffffff).to_bytes(4,'big') for x in w)
def _cpp_reg(b):return 'SpuRegister{{'+','.join(map(str,b))+'}}'


# HIRES8 self-loop block (spu_block.self_loop): 14 instructions at 0x100, the BRNZ targets the block's own start, then BR self.
# Classes: LS load/store, word/halfword arithmetic, shuffle (with the special 0x80/0xc0/0xe0 controls), two lifted bodies without a
# template (ORX, ADDX: they read g[] directly), a constant set inside the loop after a store (an early exit point precedes it),
# live-in counters, a read-only invariant (r5, r40) and registers set before every exit point (r30..r33).
SELF_LOOP_ITERATIONS = 20
SELF_LOOP = [_ri10(0x34,0,3,30),_rr(0x0c0,5,30,31),_rr(0x241,6,31,32),_rrr(0xb,33,30,32,40),_rr(0x0c8,34,33,34),_rr(0x1f0,0,34,35),
             _rr(0x340,31,35,36),_rr(0x0c0,31,6,6),_ri10(0x24,0,9,33),_ri16(0x081,-3,37),_ri10(0x1c,16,3,3),_ri10(0x1c,16,9,9),
             _ri10(0x1c,-1,4,4),_ri16(0x042,-13,4),_ri16(0x064,0,0)]


def self_loop_initial():
    regs={3:_bytes([0x2000,0xdead0001,0xdead0002,0xdead0003]),9:_bytes([0x3000,7,8,9]),4:_bytes([SELF_LOOP_ITERATIONS,0x55,0x66,0x77]),
          5:_bytes([0x01020304*(w+1) for w in range(4)]),6:_bytes([0x9e3779b9+w for w in range(4)]),34:_bytes([0x7fff8001^w for w in range(4)]),
          36:_bytes([2,5,4,7]),37:_bytes([0x11111111]*4),
          40:bytes([0x03,0x12,0x80,0x1f,0xc0,0x05,0xe0,0x14,0x0a,0x9f,0x1b,0xdf,0x00,0x11,0xff,0x0e])}
    data=bytes((i*37+11)&255 for i in range(0x200))
    return regs,data


def self_loop_model():
    # Independent model of the loop from the SPU ISA v1.2 instruction definitions (a/ah/xor/shufb/orx/addx/lqd/stqd/il/ai/brnz).
    regs,data=self_loop_initial()
    r={k:bytearray(v) for k,v in regs.items()}
    ls=bytearray(0x40000);ls[0x2000:0x2200]=data
    word=lambda b:_words(bytes(b))
    while True:
        r[30]=bytearray(ls[(word(r[3])[0])&0x3fff0:][:16])
        r[31]=bytearray(_bytes([x+y for x,y in zip(word(r[30]),word(r[5]))]))
        r[32]=bytearray(_bytes([x^y for x,y in zip(word(r[31]),word(r[6]))]))
        both=bytes(r[32])+bytes(r[30])
        r[33]=bytearray((0x80 if c>=0xe0 else 0xff if c>=0xc0 else 0) if c&0x80 else both[c&0x1f] for c in r[40])
        half=lambda b:[int.from_bytes(b[i:i+2],'big') for i in range(0,16,2)]
        r[34]=bytearray(b''.join(((x+y)&0xffff).to_bytes(2,'big') for x,y in zip(half(r[33]),half(r[34]))))
        w=word(r[34]);r[35]=bytearray(_bytes([w[0]|w[1]|w[2]|w[3],0,0,0]))
        r[36]=bytearray(_bytes([x+y+(z&1) for x,y,z in zip(word(r[35]),word(r[31]),word(r[36]))]))
        r[6]=bytearray(_bytes([x+y for x,y in zip(word(r[6]),word(r[31]))]))
        a=word(r[9])[0]&0x3fff0;ls[a:a+16]=r[33]
        r[37]=bytearray(_bytes([0xfffffffd]*4))
        for reg,k in ((3,16),(9,16),(4,-1)):r[reg]=bytearray(_bytes([x+k for x in word(r[reg])]))
        if not word(r[4])[0]:break
    return {k:bytes(v) for k,v in r.items()},bytes(ls[0x3000:0x3200])


# Self-store exit in the SECOND iteration of a self loop (spu_block.self_loop early exits): STQD r7 hits the loop's own quadword
# then; the stored bytes put ADD r13,r13,r12 right behind the store, which the interpreter runs after the exit and which reads r12
# from the register file.  r12 is set by IL after the store in every iteration (not live-in); the exit must store the value of the
# first iteration (77).  Then AI r4; AI r14; BRNZ falls through to BR self at 0x414: 5 + 5 instructions.
SELF_STORE_LOOP = [_ri10(0x24,0,8,7),_ri10(0x1c,0x1f0,8,8),_ri10(0x1c,-1,4,4),_ri16(0x081,77,12),_ri16(0x042,-4,4),_ri16(0x064,0,0)]
SELF_STORE_QUAD = [_ri10(0x24,0,8,7),_rr(0x0c0,12,13,13),_ri10(0x1c,-1,4,4),_ri10(0x1c,1,14,14)]

SELF_LOOP_COMMON = r'''
 struct State {std::array<SpuRegister,128> gpr;std::uint32_t pc;std::uint64_t steps;std::vector<std::uint8_t> ls;};
 auto snap=[&]{return State{t.gpr,t.pc,t.steps,std::vector<std::uint8_t>(ls,ls+0x40000)};};
 auto restore=[&](const State& x){t.gpr=x.gpr;t.pc=x.pc;t.steps=x.steps;std::copy(x.ls.begin(),x.ls.end(),ls);};
 const auto initial=snap();
 auto reset=[&]{restore(initial);t.stopped=false;t.reloc_delta=0;t.dispatch_cache.clear();};
 // oracle: the exact interpreter (trace mode, no native code) retires exactly k instructions from the same initial state
 auto oracle=[&](std::uint64_t k){reset();if(k)spu_step(s,t,ls,unsigned(k),UINT64_MAX,true);assert(t.steps==k);return snap();};
 auto check=[&](const char* what){
  const auto mine=snap();const auto want=oracle(mine.steps);
  if(mine.gpr!=want.gpr || mine.pc!=want.pc || mine.ls!=want.ls){
   unsigned reg=0;while(reg<128 && mine.gpr[reg]==want.gpr[reg])++reg;
   std::fprintf(stderr,"MISMATCH %s steps=%llu pc %x/%x first differing gpr %u ls %d\n",what,(unsigned long long)mine.steps,mine.pc,want.pc,reg,int(mine.ls!=want.ls));
   throw std::runtime_error("self-loop state differs from the exact interpreter");
  }
  restore(mine);};
'''


@unittest.skipUnless(shutil.which('c++'),'Requires native C++ compiler')
class SpuBlockTests(unittest.TestCase):
    def test_chained_calls_keep_exact_steps_registers_and_fallbacks(self):
        # 5 + 39*9+4 (B) + 39*7 (A) + 1 = 634 instructions reach the final self-loop at 0x118; r20 = 2*40 + 39 = 119.
        # Exact budgets (7 < one superblock) retire exactly 7 per slice; chained calls must not change that, nor the results of
        # one large slice. Then B's first word becomes AI r10,r10,-2: the chained callee no longer validates, the generic path
        # interprets the new bytes: 20 B runs, 19 A runs, r20 = 2*20 + 19 = 59, 5 + (19*9+4) + 19*7 + 1 = 314 instructions.
        self.native(chained_recursion(),r'''
 auto reset=[&]{t.pc=0x100;t.steps=0;t.gpr={};};
 auto check=[&](unsigned r20,std::uint64_t steps){
  assert(t.pc==0x118 && spu_word(t.gpr[20],0)==r20 && spu_word(t.gpr[21],0)==1 && spu_word(t.gpr[10],0)==0 && spu_word(t.gpr[1],0)==0x3ff00);
  assert(t.steps>=steps);};
 reset();while(t.steps<634){const auto before=t.steps;assert(spu_run(s,7) && t.steps==before+7);}
 check(119,634);
 const auto chained=spu_chain_returns;
 reset();assert(spu_run(s,4096) && t.steps>=4096 && t.steps<4096+64);check(119,634);
 assert(spu_chain_returns>=chained+16);   // the large slice continues callers after chained calls (bounded nesting)
 reset();for(unsigned budget=1;t.steps<634;budget=budget%13+1){const auto before=t.steps;assert(spu_run(s,budget) && t.steps==before+budget);}
 check(119,634);
 ls[0x1100]=0x1c;ls[0x1101]=0xff;ls[0x1102]=0x85;ls[0x1103]=0x0a;   // AI r10,r10,-2
 reset();while(t.steps<314){const auto before=t.steps;assert(spu_run(s,7) && t.steps==before+7);}
 check(59,314);
 reset();assert(spu_run(s,4096));check(59,314);
''')


    def test_chained_call_scopes_commit_what_the_per_call_protocol_commits(self):
        # HIRES9 shared chained calls (one scope origin for caller and callee) against the per-call protocol written out by hand:
        # the caller's retired count is committed before the call, the callee's by its own scope unless the control generation
        # changed under it, the caller then starts from zero.  Initial steps 1000, budget 100, caller retired 10 before the call,
        # callee 7 unless stated.  Each case runs in the default (shared) form and with ANYPS3_SPU_CHAIN_SHARED=0 (per-call).
        body=r'''
 static unsigned run_n=0,calls=0;static int mode=0;static std::uint64_t sampled=0;
 static SpuNativeFunction inner=[](PpuState&,NativeSpu::Thread& t,std::uint8_t*){SpuBlockScope block(t);block.executed+=4;++t.control_generation;};
 // callee: retire run_n, then 1 = generation change, 2 = throw, 3 = both, 4 = sample the access origin, 5 = nested chained call
 // into `inner` (retires 4, changes the generation), 6 = no progress (dynamic miss)
 SpuNativeFunction callee=[](PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){++calls;SpuBlockScope block(t);
  if(mode==6){t.dynamic_miss=true;return;}
  block.executed+=run_n;
  if(mode==4)sampled=HostAccessContext::sample().operation_id;
  if(mode==5){assert(!spu_chain_call(s,t,ls,inner,0,block));return;}
  if(mode&1)++t.control_generation;
  if(mode&2)throw std::runtime_error("callee");};
 auto start=[&](unsigned budget,int m,unsigned n=7){t.steps=1000;t.block_budget=budget;t.block_limit=UINT64_MAX;t.block_exact=false;t.block_trace=false;
  t.block_control_exit=false;t.chain_depth=0;mode=m;run_n=n;calls=0;};
 auto expect=[&](std::uint64_t steps,unsigned budget){assert(t.steps==steps && t.block_budget==budget && t.chain_scope==nullptr && t.chain_depth==0);};
 // A: normal call, the caller retires 3 more: 10+7+3
 start(100,0);{SpuBlockScope caller(t);caller.executed=10;assert(spu_chain_call(s,t,ls,callee,0,caller));caller.executed+=3;}expect(1020,80);
 // B: generation change inside the callee: only the caller's 10
 start(100,1);{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller));}expect(1010,90);
 // C: generation change in the caller after the call returned: 10+7, the caller's later 3 are dropped
 start(100,0);{SpuBlockScope caller(t);caller.executed=10;assert(spu_chain_call(s,t,ls,callee,0,caller));caller.executed+=3;++t.control_generation;}expect(1017,83);
 // D/E: exception in the callee, without/with a generation change
 start(100,2);try{SpuBlockScope caller(t);caller.executed=10;spu_chain_call(s,t,ls,callee,0,caller);assert(false);}catch(const std::runtime_error&){}expect(1017,83);
 start(100,3);try{SpuBlockScope caller(t);caller.executed=10;spu_chain_call(s,t,ls,callee,0,caller);assert(false);}catch(const std::runtime_error&){}expect(1010,90);
 // F: the callee overshoots the slice (budget 12): the caller returns; budget 0
 start(12,0,5);{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller) && calls==1);}expect(1015,0);
 // budget used up before the call: no callee
 start(10,0);{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller) && calls==0);}expect(1010,0);
 // G: generation changed in the caller before the call: its 10 are dropped, the callee's fresh scope commits 7
 start(100,0);{SpuBlockScope caller(t);caller.executed=10;++t.control_generation;assert(!spu_chain_call(s,t,ls,callee,0,caller) && calls==1);}expect(1007,93);
 // H: nested chained call whose callee changes the generation: 10 and the middle function's 7 stay
 start(100,5);{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller));}expect(1017,83);
 // I: the callee's access origin samples the retired instruction number 1000+10+7
 start(100,4);{SpuBlockScope caller(t);caller.executed=10;assert(spu_chain_call(s,t,ls,callee,0,caller));}expect(1017,83);assert(sampled==1017);
 // depth bound and control exit: no callee
 start(100,0);t.chain_depth=16;{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller) && calls==0);}assert(t.steps==1010 && t.chain_depth==16);
 start(100,0);t.block_control_exit=true;{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller) && calls==0);}expect(1010,90);
 // no progress in the callee (miss): the caller returns with its 10
 start(100,6);{SpuBlockScope caller(t);caller.executed=10;assert(!spu_chain_call(s,t,ls,callee,0,caller) && calls==1);}expect(1010,90);
'''
        self.native({0x100:0x40200000},body+' assert(spu_chain_shared>0);\n')
        with mock.patch.dict(os.environ,{'ANYPS3_SPU_CHAIN_SHARED':'0'}):
            self.native({0x100:0x40200000},body+' assert(spu_chain_shared==0);\n')

    def test_shufb_of_word_and_halfword_lanes_matches_the_isa(self):
        # HIRES9: SHUFB whose sources are both held as word (A, XOR) or halfword (AH) lanes selects with a flipped control instead of
        # converting the sources.  Expectation: SPU ISA v1.2 SHUFB/A/XOR/AH written out in Python; the control covers both
        # sources, every byte index class and the 0x80/0xc0/0xe0 specials.
        prog=[_ri10(0x34,0,3,30),_ri10(0x34,1,3,31),_rr(0x0c0,31,30,32),_rr(0x241,31,32,33),_rrr(0xb,34,32,33,40),
              _rr(0x0c8,31,30,35),_rr(0x0c8,30,35,36),_rrr(0xb,37,36,35,40),_ri10(0x24,0,9,34),_ri10(0x24,1,9,37),_ri16(0x064,0,0)]
        words={0x100+4*i:w for i,w in enumerate(prog)}
        data=bytes((i*73+29)&255 for i in range(32))
        control=bytes([0x03,0x12,0x80,0x1f,0xc0,0x05,0xe0,0x14,0x0a,0x9f,0x1b,0xdf,0x00,0x11,0xff,0x0e])
        word=lambda b:_words(bytes(b));half=lambda b:[int.from_bytes(b[i:i+2],'big') for i in range(0,16,2)]
        hadd=lambda x,y:b''.join(((p+q)&0xffff).to_bytes(2,'big') for p,q in zip(half(x),half(y)))
        shufb=lambda a,b,c:bytes((0x80 if k>=0xe0 else 0xff if k>=0xc0 else 0) if k&0x80 else (a+b)[k&0x1f] for k in c)
        r30,r31=data[:16],data[16:]
        r32=_bytes([x+y for x,y in zip(word(r30),word(r31))]);r33=_bytes([x^y for x,y in zip(word(r32),word(r31))])
        r34=shufb(r33,r32,control);r35=hadd(r30,r31);r36=hadd(r35,r30);r37=shufb(r35,r36,control)
        _,functions,_=generate_parts((image(words),))
        text=functions[0][1]
        self.assertIn('_mm_set1_epi8(3)',text)
        self.assertIn('_mm_set1_epi8(1)',text)
        body=f't.gpr[3]={_cpp_reg(_bytes([0x2000,0,0,0]))};t.gpr[9]={_cpp_reg(_bytes([0x3000,0,0,0]))};t.gpr[40]={_cpp_reg(control)};'
        body+='{const std::uint8_t d[]={'+','.join(map(str,data))+'};std::copy(d,d+sizeof(d),ls+0x2000);}'
        body+='const std::uint8_t want[]={'+','.join(map(str,r34+r37))+'};'
        body+=f'assert(spu_run(s,4096) && t.pc==0x128 && (t.gpr[34]=={_cpp_reg(r34)}) && (t.gpr[37]=={_cpp_reg(r37)}));'
        body+='assert(std::equal(want,want+32,ls+0x3000));'
        self.native(words,body)

    def test_self_loop_keeps_exact_state_at_every_slice_boundary(self):
        # The self-loop block continues without guard/register-file round trip only while the guard would pass; every slice end
        # (exact budgets < 64 decline the block, soft budgets end at ex>=lim inside the loop, hard step limits) must show the
        # exact interpreter's full state (all GPRs, pc, steps, all LS).  The final registers and output also match an
        # independent Python model of the instruction definitions.
        regs,data=self_loop_initial()
        final,out=self_loop_model()
        words={0x100+4*i:w for i,w in enumerate(SELF_LOOP)}
        body=''.join(f't.gpr[{k}]={_cpp_reg(v)};' for k,v in regs.items())
        body+='{const std::uint8_t d[]={'+','.join(map(str,data))+'};std::copy(d,d+sizeof(d),ls+0x2000);}'
        body+=SELF_LOOP_COMMON
        body+='const std::uint8_t out[]={'+','.join(map(str,out))+'};'
        body+='auto final_check=[&]{assert(t.pc==0x138 && std::equal(out,out+sizeof(out),ls+0x3000));'
        body+=''.join(f'assert((t.gpr[{k}]=={_cpp_reg(final[k])}));' for k in (3,4,6,9,30,31,32,33,34,35,36,37))+'};'
        body+=f'const std::uint64_t total={14*SELF_LOOP_ITERATIONS};'
        body+=r'''
 const unsigned budgets[]={1,2,3,5,7,11,13,14,15,16,17,27,28,29,30,41,63,64,65,100,127,200};
 for(unsigned phase=0;phase<3;++phase){
  reset();unsigned i=phase;
  while(t.steps<total+20){
   const unsigned b=budgets[i++%(sizeof(budgets)/sizeof(budgets[0]))];const auto before=t.steps;
   spu_step(s,t,ls,b);
   assert(b<64?t.steps==before+b:t.steps>=before+b && t.steps<before+b+64);
   check("slice");
  }
  final_check();
 }
 for(const std::uint64_t limit:{150u,157u,169u,279u,280u,281u}){
  reset();spu_step(s,t,ls,4096,limit);assert(t.steps==limit);check("hard limit");
 }
 reset();spu_step(s,t,ls,4096);assert(t.steps>=4096 && t.steps<4096+64);check("one slice");final_check();
'''
        self.native(words,body)

    def test_self_loop_early_exit_stores_registers_of_the_previous_iteration(self):
        words={0x400+4*i:w for i,w in enumerate(SELF_STORE_LOOP)}
        quad=_bytes(SELF_STORE_QUAD)
        body=f't.gpr[7]={_cpp_reg(quad)};spu_word(t.gpr[8],0,0x210);spu_word(t.gpr[4],0,2);t.pc=0x400;'
        body+=SELF_LOOP_COMMON+r'''
 spu_step(s,t,ls,100);assert(t.pc==0x414 && t.steps>=100);
 assert(spu_word(t.gpr[13],0)==77 && spu_word(t.gpr[12],0)==77 && spu_word(t.gpr[14],0)==1 && spu_word(t.gpr[4],0)==0 && spu_word(t.gpr[8],0)==0x400);
 check("self store");
 reset();for(unsigned b=1;t.steps<12;b=b%5+1)spu_step(s,t,ls,b);check("exact budgets");
'''
        self.native(words,body)

    def test_self_loop_block_keeps_registers_in_locals(self):
        # Generated form: one guarded entry, carried locals, a back edge that continues only while the guard would pass, no
        # register-file access inside the iteration of a loop without lifted bodies, the flush only on the exit path.
        loop=[_ri10(0x34,0,3,30),_rr(0x0c0,5,30,31),_ri10(0x24,0,9,31),_ri10(0x1c,16,3,3),_ri10(0x1c,16,9,9),_ri10(0x1c,-1,4,4),
              _ri16(0x042,-6,4),_ri16(0x064,0,0)]
        _,functions,_=generate_parts((image({0x100+4*i:w for i,w in enumerate(loop)}),))
        text=functions[0][1].split('spu_b_100:')[1].split('spu_b_11c:')[0]
        head,rest=text.split('for(;;){')
        iteration,tail=rest.split('continue;}')
        self.assertIn('c3w=spu_vswap32(spu_vld(g[3]))',head)
        self.assertNotIn('c30',head)          # set before every exit point: not carried
        self.assertNotIn('g[',iteration.replace('spu_vst(g[','EXIT['))
        self.assertIn('llim=(vm&0x1ull)==0x1ull?lim:0u;',head)   # back-edge bound fixed before the loop (lim, vm invariant)
        self.assertIn('ex<llim){c3w=',iteration)
        self.assertIn('spu_vst(g[30],',tail)
        self.assertIn('goto spu_b_100;',tail)

    def test_self_loop_without_valid_region_runs_the_exact_guard_every_iteration(self):
        # AI r3,r3,1; AI r4,r4,-1; BRNZ r4 back (self loop); STOP 0x100.  A changed word at 0x13c (same 64-byte region, never
        # executed) keeps the region unproven, so every entry of the loop block runs the exact guard (spu_block_enter, stat 26):
        # 10 loop entries + the STOP block = 11 by hand.  With the bytes unchanged the region is proven once: no exact guard.
        words={0x100:_ri10(0x1c,1,3,3),0x104:_ri10(0x1c,-1,4,4),0x108:_ri16(0x042,-2,4),0x10c:0x100,0x13c:0x00200000}
        with mock.patch.dict(os.environ,{'ANYPS3_SPU_STATS':'1'}):
            self.native(words,r'''
 auto go=[&]{t.pc=0x100;t.steps=0;spu_word(t.gpr[3],0,0);spu_word(t.gpr[4],0,10);t.stats[26].value.store(0);
   assert(spu_run(s,4096) && t.steps==31 && t.pc==0x110 && spu_word(t.gpr[3],0)==10 && spu_word(t.gpr[4],0)==0);
   return t.stats[26].value.load();};
 assert(go()==0);
 ls[0x13f]^=1;t.dispatch_cache.clear();assert(go()==11);
''')

    def native(self,words,body,overlays=()):
        img=image(words)
        test_runtime_objects.RuntimeObjectsTests().run_native(setup(img)+body,spu_images=(img,),spu_overlays=(overlays,))

    def test_overlay_replacement_and_unknown_word_preserve_retired_prefix(self):
        # AI r3,r3,1; AI r4,r4,2; BR -8. Replacement AI r4,r4,7.
        self.native({0x100:0x1c004183,0x104:0x1c008204,0x108:0x327fff00},r'''
 assert(spu_run(s,4) && t.steps==4 && t.pc==0x104);
 assert(spu_word(t.gpr[3],0)==2 && spu_word(t.gpr[4],0)==2);
 const std::uint8_t replacement[]={0x1c,0x01,0xc2,0x04};std::copy_n(replacement,4,ls+0x104);
 assert(spu_run(s,2) && t.steps==6 && t.pc==0x100 && spu_word(t.gpr[4],0)==9);
 ls[0x108]=2;ls[0x109]=ls[0x10a]=ls[0x10b]=0;
 fault([&]{spu_run(s,10);});
 assert(t.steps==8 && t.pc==0x108 && spu_word(t.gpr[3],0)==3 && spu_word(t.gpr[4],0)==16);
''',((0x104,bytes.fromhex('1c01c204')),))

    def test_store_quad_to_upcoming_code_uses_new_word_immediately(self):
        # STQA r5,0x110 overwrites code in the very same native function.
        self.native({0x100:0x1c004183,0x104:0x20802205,0x108:0x1c004183,0x10c:0x40200000,
                     0x110:0x1c004387,0x114:1,0x118:0x40200000,0x11c:0x40200000},r'''
 const std::uint8_t replacement[]={0x1c,0x01,0xc3,0x87,0,0,0,1,0x40,0x20,0,0,0x40,0x20,0,0};
 std::copy_n(replacement,16,t.gpr[5].begin());
 assert(spu_run(s,100) && t.steps==6 && t.pc==0x118);
 assert(spu_word(t.gpr[3],0)==2 && spu_word(t.gpr[7],0)==7);
 assert(std::equal(replacement,replacement+16,ls+0x110));
''',((0x110,bytes.fromhex('1c01c387000000014020000040200000')),))

    def test_dma_to_upcoming_code_and_unknown_dma_payload(self):
        words={0x100:0x1c004183,0x104:0x21a00a86,0x108:0x1c004183,0x10c:0x40200000,
               0x110:0x1c004387,0x114:1,0x118:0x40200000,0x11c:0x40200000}
        self.native(words,r'''
 const std::uint8_t replacement[]={0x1c,0x01,0xc3,0x87,0,0,0,1,0x40,0x20,0,0,0x40,0x20,0,0};
 std::copy_n(replacement,16,mem.access(0x10100,16,true));
 t.dma={0x110,0,0x10100,16,0};spu_word(t.gpr[6],0,0x40);
 assert(spu_run(s,100) && t.steps==6 && t.pc==0x118 && spu_word(t.gpr[7],0)==7);
 t.pc=0x100;mem.store(0x10100,4,0x02000000);
 fault([&]{spu_run(s,100);});assert(t.steps==10 && t.pc==0x110);
 assert(spu_word(t.gpr[3],0)==4 && spu_word(t.gpr[7],0)==7);
''',((0x110,bytes.fromhex('1c01c387000000014020000040200000')),))

    def test_blocking_read_retries_once_and_stop_retires_exactly_once(self):
        # AI; RDCH inbound mailbox; AI; STOP yield. Channel operand is 29.
        self.native({0x100:0x1c004183,0x104:0x01a00e84,0x108:0x1c004183,0x10c:0x100},r'''
 assert(spu_run(s,100) && t.steps==1 && t.pc==0x104 && spu_word(t.gpr[3],0)==1);
 assert(!spu_run(s,100) && t.steps==1 && t.pc==0x104);
 t.in_mailbox={0x12345678};assert(spu_run(s,100));
 assert(t.steps==4 && t.pc==0x110 && t.in_mailbox.empty());
 assert(spu_word(t.gpr[3],0)==2 && spu_word(t.gpr[4],0)==0x12345678);
''')

    def test_hard_and_soft_limits_inside_direct_loop_are_exact(self):
        self.native({0x100:0x1c004183,0x104:0x1c004204,0x108:0x327fff00},r'''
 assert(setenv("ANYPS3_SPU_MAX_STEPS","7",1)==0);
 assert(!spu_run(s,0) && !t.steps && t.pc==0x100);
 assert(spu_run(s,5) && t.steps==5 && t.pc==0x108);
 fault([&]{spu_run(s,100);});
 assert(t.steps==7 && t.pc==0x104 && spu_word(t.gpr[3],0)==3 && spu_word(t.gpr[4],0)==2);
 fault([&]{spu_run(s,1);});assert(t.steps==7 && t.pc==0x104);
 t.steps=UINT64_MAX-2;spu_step(s,t,ls,3,UINT64_MAX);
 assert(t.steps==UINT64_MAX && t.pc==0x100 && spu_word(t.gpr[4],0)==3);
 fault([&]{spu_step(s,t,ls,1,UINT64_MAX);});assert(t.steps==UINT64_MAX);
''')

    def test_unsupported_stop_keeps_exact_pc_and_completed_prefix(self):
        self.native({0x100:0x1c004183,0x104:0},r'''
 fault([&]{spu_run(s,20);});assert(t.steps==1 && t.pc==0x104 && !t.stopped);
 fault([&]{spu_run(s,20);});assert(t.steps==1 && t.pc==0x104);
''')

    def test_direct_link_crosses_page_and_indirect_return_resumes_exactly(self):
        # AI +1; BRSL r5,+8; STOP 1; AI +2; BI r5.
        self.native({0xff8:0x1c004183,0xffc:0x33000105,0x1000:1,
                     0x1004:0x1c008183,0x1008:0x35000280},r'''
 assert(spu_run(s,100) && t.steps==5 && t.pc==0x1004);
 assert(spu_word(t.gpr[3],0)==3 && spu_word(t.gpr[5],0)==0x1000);
''')

    def test_shuffle_all_controls_and_unaligned_big_endian_words(self):
        # Fixed 16-byte groups: four repetitions of the two source-register
        # selections, then four zero groups, two FF groups and two 80 groups.
        expected=(list(range(16))+list(range(64,80)))*4+[0]*64+[255]*32+[128]*32
        body='(void)call;const std::uint8_t expected[]={'+','.join(map(str,expected))+'};'
        body+=r'''
 SpuRegister a,b,c;for(unsigned i=0;i<16;++i){a[i]=i;b[i]=64+i;}
 for(unsigned base=0;base<256;base+=16){
     for(unsigned i=0;i<16;++i)c[i]=base+i;
     const auto scalar=spu_shuffle_scalar(a,b,c),native=spu_shuffle(a,b,c);
     for(unsigned i=0;i<16;++i){assert(scalar[i]==expected[base+i]);assert(native[i]==expected[base+i]);}
     auto alias=a;alias=spu_shuffle(alias,b,c);assert(alias==native);
     alias=b;alias=spu_shuffle(a,alias,c);assert(alias==native);
     alias=c;alias=spu_shuffle(a,b,alias);assert(alias==native);
 }
 const std::uint8_t unaligned[]={255,1,0x23,0x45,0x67,0x89,0xab,0xcd,0xef};
 assert(spu_load_word(unaligned+1)==0x01234567 && spu_load_word(unaligned+5)==0x89abcdef);
 SpuRegister reg;reg.fill(0xa5);spu_word(reg,3,0x89abcdef);
 for(unsigned i=0;i<12;++i)assert(reg[i]==0xa5);
 assert(reg[12]==0x89 && reg[13]==0xab && reg[14]==0xcd && reg[15]==0xef);
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body)

    def test_generated_flow_uses_direct_labels_and_retains_word_guards(self):
        # SPUFAST superblock: one guard over all original bytes at entry, a direct
        # edge back to the guarded entry, no per-instruction fetch or dispatch.
        _,functions,_=generate_parts((image({0x100:0x1c004183,0x104:0x327fff80}),))
        text=functions[0][1]
        self.assertIn('goto spu_b_100;',text)
        self.assertIn('spu_region_eq<8>(ls,(flo+0x0u)&0x3ffffu,code+0x0u)',text)
        self.assertIn('spu_block_slow(s,t,block,ls,flo,code,0x8u,a,0x0u,8,2,lim,vm,',text)
        self.assertNotIn('spu_load_word',text)
        self.assertNotIn('switch(',text.split('spu_b_100:')[1])
