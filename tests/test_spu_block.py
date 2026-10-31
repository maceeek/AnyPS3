"""Native SPU threaded-block regressions with fixed independent results."""
import shutil
import unittest
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
