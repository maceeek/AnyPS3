"""GPU vertex programs with control flow (BRI/BRB/CAL/CLB/RET) against the CPU interpreter and hand-computed oracles.

The CPU interpreter (run_vertex_program) is the differential oracle: random structured programs (forward skips by a CC predicate or a bool
constant, bounded counter loops with backward BRI, nested subroutines) run on the GPU compute path and on the CPU for 40 divergent
vertices each and must agree on every output register. Directed programs have independently computed results (a loop sum, a call tree, a bool
branch) and the error conditions (call stack overflow, endless loop, taken branch outside the program, falling off the program, an unwritten
constant read only on the executed path, BRA/CLI/unwritten reachable instruction rejected) must raise the GPU error flag exactly when the
interpreter throws (or leave the program to the CPU path).
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES

MAIN = r'''
using namespace rsxdraw;
static std::uint32_t lcg(std::uint32_t& x){x=x*1664525u+1013904223u;return x>>10;}
// ---- tiny VP assembler (bit layout of run_vertex_program / rsxvk::vertex_shader) ----
struct Src {unsigned type=0,index=0,swz=0x1b;bool neg=false,abs=false;
 unsigned raw()const{return type|(index<<2)|(swz<<8)|(neg?1u<<16:0);}};
static Src T(unsigned i,unsigned swz=0x1b){Src s;s.type=1;s.index=i;s.swz=swz;return s;}
static Src I(unsigned i,unsigned swz=0x1b){Src s;s.type=2;s.index=i;s.swz=swz;return s;}
static Src C(unsigned i,unsigned swz=0x1b){Src s;s.type=3;s.index=i;s.swz=swz;return s;}
struct Ins {
 unsigned vec=0,sca=0;Src s[3];unsigned vtmp=63,vmask=0,stmp=63,smask=0,dst=31;bool to_output=false;
 unsigned condition=7,cc_index=0,cc_swz=0x1b;bool predicated=false;bool end=false;unsigned imm=0,bool_index=0,bool_sense=1;
 unsigned input=0,constant=0;
 std::array<unsigned,4> words()const{
  unsigned ccs=0;for(unsigned i=0;i<4;++i)ccs|=((cc_swz>>(6-2*i))&3)<<(8-2*i);
  // An instruction carries one input register and one constant register index for all its sources (d1 bits 8-11 / 12-21).
  unsigned input=this->input,constant=this->constant;
  for(unsigned n=0;n<3;++n){if(s[n].type==2)input=s[n].index;if(s[n].type==3)constant=s[n].index;}
  const bool branch=sca==8||sca==9||sca==10||sca==11||sca==12||sca==17||sca==18;
  unsigned d0=(vtmp<<15)|(condition<<10)|ccs|(cc_index<<25)|(predicated?1u<<13:0)|(to_output?1u<<30:0);
  unsigned d1=((s[0].raw()>>9)&255)|(input<<8)|(constant<<12)|(vec<<22)|(sca<<27);
  unsigned d2=((s[2].raw()>>11)&63)|((s[1].raw()&0x1ffff)<<6)|((s[0].raw()&511)<<23);
  unsigned d3=(end?1u:0)|(dst<<2)|(stmp<<7)|(vmask<<13)|(smask<<17)|((s[2].raw()&2047)<<21);
  for(unsigned n=0;n<3;++n)if(s[n].abs)d0|=1u<<(21+n);
  if(branch){   // a control instruction has no ALU part: the source fields carry the immediate target / the bool selector
   d0=(vtmp<<15)|(condition<<10)|ccs|(cc_index<<25)|(((imm>>9)&1)<<23);
   d1=sca<<27;d2=(imm>>3)&63;
   d3=(end?1u:0)|((imm&7)<<29)|(bool_index<<23)|((bool_sense&1)<<28);}
  return {d0,d1,d2,d3};
 }
};
static Ins alu(unsigned vec,unsigned tmp,Src a,Src b=Src{},Src c=Src{},unsigned mask=15){Ins i;i.vec=vec;i.vtmp=tmp;i.vmask=mask;i.s[0]=a;i.s[1]=b;i.s[2]=c;
 if(vec==3){i.s[2]=b;i.s[1]=Src{};}   // ADD reads src0 and src2
 return i;}
static Ins out(unsigned reg,Src a,unsigned mask=15){Ins i;i.vec=1;i.s[0]=a;i.to_output=true;i.dst=reg;i.vmask=mask;return i;}   // masks: x=8 y=4 z=2 w=1
static Ins set_cc(unsigned vec,Src a,Src b,unsigned mask=8,unsigned cc=0){Ins i=alu(vec,63,a,b,Src{},mask);i.cc_index=cc;return i;}
static Ins bri(unsigned target,unsigned condition=7,unsigned swz=0x00){Ins i;i.sca=9;i.imm=target;i.condition=condition;i.cc_swz=swz;return i;}
static Ins brb(unsigned target,unsigned bit,unsigned sense){Ins i;i.sca=17;i.imm=target;i.bool_index=bit;i.bool_sense=sense;return i;}
static Ins cal(unsigned target,unsigned condition=7,unsigned swz=0x00){Ins i;i.sca=10;i.imm=target;i.condition=condition;i.cc_swz=swz;return i;}
static Ins clb(unsigned target,unsigned bit,unsigned sense){Ins i;i.sca=18;i.imm=target;i.bool_index=bit;i.bool_sense=sense;return i;}
static Ins ret(unsigned condition=7,unsigned swz=0x00){Ins i;i.sca=12;i.condition=condition;i.cc_swz=swz;return i;}
static void put(NativeRsx& g,unsigned pc,const Ins& i){const auto w=i.words();for(unsigned c=0;c<4;++c){g.transform_program[pc*4+c]=w[c];g.transform_program_written[pc*4+c]=true;}}
static void put_all(NativeRsx& g,const std::vector<Ins>& p,unsigned at=0){for(unsigned n=0;n<p.size();++n)put(g,at+n,p[n]);}
// ---- GPU run ----
struct Run {bool gpu_rejected=false;unsigned flag=0;std::vector<std::array<Vec4,16>> outputs;};
static void replace_text(std::string& s,const std::string& a,const std::string& b){for(std::size_t at=0;(at=s.find(a,at))!=std::string::npos;at+=b.size())s.replace(at,a.size(),b);}
// Layout: [0]=count, [1+64*id+4*n]=input n of vertex id, outputs behind the inputs, constants at w[2], written masks behind the constants, bool register behind the masks.
// (w[2] and w[7] alias input 0.y and input 1.z of vertex 0: the programs below never read inputs 0 and 1.)
static Run run_gpu(NativeVulkan& vk,const NativeRsx& g,unsigned start,const std::vector<std::array<Vec4,16>>& inputs,std::uint32_t bools){
 Run run;auto source=rsxvk::vertex_shader(g,start,true,true);if(!source){run.gpu_rejected=true;return run;}
 const unsigned count=unsigned(inputs.size()),base=1+128*count,masks=base+1872,bool_word=masks+468;
 std::vector<std::uint32_t> words(bool_word+4,0);words[0]=count;words[2]=base;
 for(unsigned id=0;id<count;++id)for(unsigned n=2;n<16;++n)for(unsigned c=0;c<4;++c)words[1+id*64+n*4+c]=float_bits(inputs[id][n][c]);
 for(unsigned i=0;i<468*4;++i)words[base+i]=g.transform_constants[i];
 for(unsigned n=0;n<468;++n){unsigned m=0;for(unsigned c=0;c<4;++c)m|=unsigned(g.transform_constants_written[n*4+c])<<c;words[masks+n]=m;}
 words[bool_word]=bools;replace_text(*source,"R(0x1ff8u)","w["+std::to_string(bool_word)+"]");
 vk.compute(*source,words,(count+63)/64);
 run.flag=words[7];run.outputs.resize(count);
 for(unsigned id=0;id<count;++id)for(unsigned n=0;n<16;++n)for(unsigned c=0;c<4;++c)run.outputs[id][n][c]=bits_float(words[1+count*64+id*64+n*4+c]);
 return run;
}
static bool same_float(float a,float b){return a==b || (std::isnan(a) && std::isnan(b));}
static std::vector<std::array<Vec4,16>> make_inputs(std::uint32_t& r,unsigned count){
 static const float values[]={-2,-1,-.5f,0,.5f,1,2,3};
 std::vector<std::array<Vec4,16>> in(count);
 for(auto& v:in)for(unsigned n=0;n<16;++n)for(unsigned c=0;c<4;++c)v[n][c]=n<2?0.f:values[lcg(r)%8];
 return in;
}
static void set_constants(NativeRsx& g,std::uint32_t& r,unsigned count){
 static const float values[]={-2,-1,-.5f,.5f,1,2,3,.25f};
 for(unsigned i=0;i<count*4;++i){g.transform_constants[i]=float_bits(values[lcg(r)%8]);g.transform_constants_written[i]=true;}
 // c0 = 0, c1 = 1, c2 = 2, c3 = 3, c4 = 4 (loop bounds and increments)
 for(unsigned c=0;c<4;++c){g.transform_constants[c]=float_bits(0.f);g.transform_constants[4+c]=float_bits(1.f);g.transform_constants[8+c]=float_bits(2.f);
  g.transform_constants[12+c]=float_bits(3.f);g.transform_constants[16+c]=float_bits(4.f);}
}
// Compare GPU and interpreter on every vertex; returns the number of vertices compared.
static unsigned compare(NativeVulkan& vk,NativeRsx& g,unsigned start,const std::vector<std::array<Vec4,16>>& inputs,std::uint32_t bools,const char* what){
 g.registers[0x1ff8]=bools;
 const auto gpu=run_gpu(vk,g,start,inputs,bools);
 if(gpu.gpu_rejected)throw std::runtime_error(std::string(what)+": GPU translation rejected the program");
 if(gpu.flag)throw std::runtime_error(std::string(what)+": GPU error flag "+std::to_string(gpu.flag));
 for(unsigned id=0;id<inputs.size();++id){
  const auto cpu=run_vertex_program(g,start,inputs[id]);
  for(unsigned n=0;n<16;++n)for(unsigned c=0;c<4;++c)if(!same_float(cpu.o[n][c],gpu.outputs[id][n][c]))
   throw std::runtime_error(std::string(what)+": vertex "+std::to_string(id)+" o"+std::to_string(n)+"."+"xyzw"[c]+" GPU "+std::to_string(gpu.outputs[id][n][c])+" CPU "+std::to_string(cpu.o[n][c]));
 }
 return unsigned(inputs.size());
}
// ---- random structured programs ----
struct Builder {
 std::vector<Ins> code;std::uint32_t& r;std::vector<std::vector<Ins>> subs;std::vector<std::pair<unsigned,unsigned>> call_sites;std::vector<unsigned> entries;
 explicit Builder(std::uint32_t& rr):r(rr){}
 Src source(){const unsigned k=lcg(r)%10;static const unsigned swz[]={0x1b,0x00,0x55,0xaa,0xff,0xe4,0xb1,0x4e};
  Src s=k<5?T(lcg(r)%6,swz[lcg(r)%8]):k<8?I(2+lcg(r)%8,swz[lcg(r)%8]):C(5+lcg(r)%10,swz[lcg(r)%8]);
  s.neg=lcg(r)%5==0;s.abs=lcg(r)%7==0;return s;}
 void unify(Ins& i){const unsigned ci=5+lcg(r)%10,ii=2+lcg(r)%8;for(auto& x:i.s){if(x.type==3)x.index=ci;if(x.type==2)x.index=ii;}}
 Ins alu_op(){static const unsigned ops[]={1,2,3,4,9,10,11,12,16,18,19,20,14,15,5,7};
  const unsigned vec=ops[lcg(r)%16];Ins i=alu(vec,lcg(r)%6,source(),source(),source(),1+lcg(r)%15);
  if(vec==3){i.s[2]=source();i.s[1]=Src{};}
  unify(i);
  if(lcg(r)%6==0){i.predicated=true;i.condition=1+lcg(r)%7;i.cc_swz=lcg(r)&255;i.cc_index=lcg(r)%2;}
  if(lcg(r)%8==0){i.to_output=true;i.vtmp=63;i.dst=6+lcg(r)%8;}   // direct output write
  return i;}
 void straight(unsigned n){for(unsigned k=0;k<n;++k)code.push_back(alu_op());}
 // forward skip over a block guarded by a CC predicate computed from random operands
 void skip_cc(unsigned inner){
  {Ins c=set_cc(lcg(r)%2?11:18,source(),source(),1+lcg(r)%15,0);unify(c);code.push_back(c);}
  const unsigned at=unsigned(code.size());code.push_back(bri(0,1+lcg(r)%7,lcg(r)&255));
  straight(inner);code[at].imm=unsigned(code.size());}
 void skip_bool(unsigned inner){
  const unsigned at=unsigned(code.size());code.push_back(brb(0,lcg(r)%32,lcg(r)%2));
  straight(inner);code[at].imm=unsigned(code.size());}
 // t6 = 0; do { body } while(++t6.x < n+1)   (c[n+1] = n+1)
 void loop(unsigned n,unsigned body){
  code.push_back(alu(1,6,C(0)));
  const unsigned head=unsigned(code.size());
  straight(body);
  if(lcg(r)%2)skip_bool(1+lcg(r)%2);
  code.push_back(alu(3,6,T(6),C(1),Src{},8));
  code.push_back(set_cc(11,T(6,0x00),C(1+n,0x00),8,1));
  Ins back=bri(head,4,0x00);back.cc_index=1;code.push_back(back);}
 void call(){const unsigned id=unsigned(subs.size());subs.emplace_back();
  const unsigned at=unsigned(code.size());Ins c=lcg(r)%3?cal(0):cal(0,1+lcg(r)%7,lcg(r)&255);if(lcg(r)%4==0)c=clb(0,lcg(r)%32,lcg(r)%2);code.push_back(c);
  subs[id].push_back(alu_op());subs[id].push_back(alu_op());call_sites.push_back({at,id});}
 std::vector<Ins> finish(){
  for(unsigned k=0;k<4;++k)code.push_back(out(k,T(k)));
  code.push_back(out(4,T(6),12));
  code.push_back(out(5,T(5)));code.back().end=true;
  for(unsigned id=0;id<subs.size();++id){entries.push_back(unsigned(code.size()));
   for(const auto& i:subs[id])code.push_back(i);
   if(id+1<subs.size() && lcg(r)%3==0){call_sites.push_back({unsigned(code.size()),id+1});code.push_back(cal(0));}   // nested call, never recursive
   if(lcg(r)%4==0){code.push_back(ret(1+lcg(r)%7,lcg(r)&255));code.push_back(alu_op());}   // conditional RET falls through when its predicate fails
   code.push_back(ret());}
  for(const auto& site:call_sites)code[site.first].imm=entries[site.second];
  return code;}
};
static void random_programs(NativeVulkan& vk){
 unsigned compared=0,programs=0,with_loop=0,with_call=0,with_bool=0;
 for(unsigned seed=1;seed<=120;++seed){
  std::uint32_t r=seed*7919u;NativeRsx g;set_constants(g,r,40);
  Builder b(r);b.straight(2+lcg(r)%4);
  const unsigned items=2+lcg(r)%5;
  for(unsigned k=0;k<items;++k){switch(lcg(r)%5){
   case 0:b.straight(1+lcg(r)%4);break;case 1:b.skip_cc(1+lcg(r)%3);break;case 2:b.skip_bool(1+lcg(r)%3);++with_bool;break;
   case 3:b.loop(1+lcg(r)%3,1+lcg(r)%3);++with_loop;break;default:b.call();++with_call;}}
  const auto program=b.finish();if(program.size()>500)throw std::runtime_error("generated program too long");
  put_all(g,program);
  auto inputs=make_inputs(r,40);
  compared+=compare(vk,g,0,inputs,lcg(r)|(lcg(r)<<16),("random program "+std::to_string(seed)).c_str());++programs;
 }
 assert(with_loop>20 && with_call>20 && with_bool>20);
 std::printf("PASS %u random control-flow programs (%u loops, %u calls, %u bool branches in total) equal the interpreter on %u vertices\n",programs,with_loop,with_call,with_bool,compared);
}
// ---- directed programs with independently computed results ----
// Constants: c0 = 0, c1 = 1, c2 = 2, c3 = 3, c4 = 4 (set_constants).
static void directed(NativeVulkan& vk){
 std::uint32_t r=1;
 {   // do { t0 += 1 } while(++t6.x < 4)  ->  o0 = 4 on every vertex (independent expectation)
  NativeRsx g;set_constants(g,r,40);
  std::vector<Ins> p;
  p.push_back(alu(1,0,C(0)));                              // 0: t0 = 0
  p.push_back(alu(1,6,C(0)));                              // 1: t6 = 0
  p.push_back(alu(3,0,T(0),C(1)));                         // 2: t0 += 1        (loop head)
  p.push_back(alu(3,6,T(6),C(1),Src{},8));                 // 3: t6.x += 1
  p.push_back(set_cc(11,T(6,0x00),C(4,0x00),8,1));         // 4: cc1.x = t6.x < 4
  p.push_back(bri(2,4,0x00));p.back().cc_index=1;          // 5: while cc1.x > 0
  p.push_back(out(0,T(0)));p.back().end=true;              // 6
  put_all(g,p);
  std::vector<std::array<Vec4,16>> in(3);
  const auto gpu=run_gpu(vk,g,0,in,0);assert(!gpu.gpu_rejected && gpu.flag==0);
  const auto cpu=run_vertex_program(g,0,in[0]);
  for(const auto& v:gpu.outputs)for(unsigned c=0;c<4;++c)if(v[0][c]!=4.f)throw std::runtime_error("loop sum: GPU "+std::to_string(v[0][c])+" CPU "+std::to_string(cpu.o[0][c])+" expected 4");
  compare(vk,g,0,in,0,"loop");
 }
 for(unsigned bit_set=0;bit_set<2;++bit_set){   // BRB with sense 1 skips "o0 = 2" when bool bit 5 is set
  NativeRsx g;set_constants(g,r,40);
  std::vector<Ins> p;
  p.push_back(out(0,C(1)));                              // 0: o0 = 1
  p.push_back(brb(3,5,1));                               // 1: if(bool[5]==1) goto 3
  p.push_back(out(0,C(2)));                              // 2: o0 = 2
  p.push_back(out(1,C(1)));p.back().end=true;            // 3
  put_all(g,p);std::vector<std::array<Vec4,16>> in(2);
  const std::uint32_t bools=bit_set?32u:0u;
  const auto gpu=run_gpu(vk,g,0,in,bools);assert(!gpu.gpu_rejected && gpu.flag==0);
  const float expected=bit_set?1.f:2.f;assert(gpu.outputs[0][0][0]==expected && gpu.outputs[1][0][3]==expected && gpu.outputs[1][1][0]==1.f);
  compare(vk,g,0,in,bools,"bool");
 }
 {   // main calls A twice, A calls B: every call adds 2 (A and B add 1 each) -> o0 = 4; the top-level RET (empty stack) ends the program
  NativeRsx g;set_constants(g,r,40);
  std::vector<Ins> p;
  p.push_back(alu(1,0,C(0)));                              // 0
  p.push_back(cal(6));                                     // 1: A
  p.push_back(cal(6));                                     // 2: A
  p.push_back(out(0,T(0)));                                // 3
  p.push_back(ret());                                      // 4: main RET: empty stack -> program ends
  p.push_back(out(7,C(2)));                                // 5: not reachable
  p.push_back(cal(9));                                     // 6: A: call B
  p.push_back(alu(3,0,T(0),C(1),Src{},8));                 // 7: t0.x += 1
  p.push_back(ret());                                      // 8
  p.push_back(alu(3,0,T(0),C(1),Src{},8));                 // 9: B: t0.x += 1
  p.push_back(ret());                                      // 10
  put_all(g,p);std::vector<std::array<Vec4,16>> in(2);
  const auto gpu=run_gpu(vk,g,0,in,0);assert(!gpu.gpu_rejected && gpu.flag==0);
  assert(gpu.outputs[0][0][0]==4.f && gpu.outputs[0][7][0]==0.f);
  compare(vk,g,0,in,0,"call tree");
 }
 {   // a predicated CAL: vertices with input[2].x < 0 call, others skip (per-vertex divergence)
  NativeRsx g;set_constants(g,r,40);
  std::vector<Ins> p;
  p.push_back(alu(1,0,C(0)));
  p.push_back(set_cc(11,I(2,0x00),C(0,0x00),8,0));         // cc0.x = in2.x < 0
  p.push_back(cal(5,4,0x00));                              // if(cc0.x > 0) call 5
  p.push_back(out(0,T(0)));p.back().end=true;
  p.push_back(out(7,C(2)));                                // not reachable
  p.push_back(alu(3,0,T(0),C(2),Src{},8));                 // 5: t0.x += 2
  p.push_back(ret());
  put_all(g,p);std::vector<std::array<Vec4,16>> in(4);
  for(unsigned i=0;i<4;++i)in[i][2]=Vec4{i%2?-1.f:1.f,0,0,0};
  const auto gpu=run_gpu(vk,g,0,in,0);assert(!gpu.gpu_rejected && gpu.flag==0);
  for(unsigned i=0;i<4;++i)assert(gpu.outputs[i][0][0]==(i%2?2.f:0.f));
  compare(vk,g,0,in,0,"predicated call");
 }
 {   // a start inside the program with an unconditional backward branch below it
  NativeRsx g;set_constants(g,r,40);
  std::vector<Ins> p(3);
  p[0]=out(0,C(2));p[0].end=true;                          // 0: o0 = 2 (target of the backward branch, END)
  p[1]=alu(1,0,C(4));                                      // 1: start here
  p[2]=bri(0);                                             // 2: unconditional goto 0
  put_all(g,p);std::vector<std::array<Vec4,16>> in(2);
  const auto gpu=run_gpu(vk,g,1,in,0);assert(!gpu.gpu_rejected && gpu.flag==0 && gpu.outputs[0][0][0]==2.f);
  compare(vk,g,1,in,0,"start inside");
 }
 std::puts("PASS directed control flow: loop sum, bool branch both ways, nested calls, divergent predicated call, backward target below the start");
}
// ---- error conditions ----
static void expect_error(NativeVulkan& vk,NativeRsx& g,unsigned start,const char* what,std::uint32_t bools=0){
 g.registers[0x1ff8]=bools;std::vector<std::array<Vec4,16>> in(2);
 bool cpu_threw=false;try{(void)run_vertex_program(g,start,in[0]);}catch(const std::runtime_error&){cpu_threw=true;}
 const auto gpu=run_gpu(vk,g,start,in,bools);
 if(!cpu_threw)throw std::runtime_error(std::string(what)+": test bug, the interpreter did not stop");
 if(gpu.gpu_rejected || gpu.flag!=16)throw std::runtime_error(std::string(what)+": GPU did not raise the error flag (rejected="+std::to_string(gpu.gpu_rejected)+" flag="+std::to_string(gpu.flag)+")");
}
static void errors(NativeVulkan& vk){
 std::uint32_t r=3;
 {NativeRsx g;set_constants(g,r,40);std::vector<Ins> p;for(unsigned i=0;i<17;++i)p.push_back(cal(i+1));p.push_back(out(0,C(1)));p.back().end=true;put_all(g,p);
  expect_error(vk,g,0,"call stack overflow (17 nested calls)");}
 {NativeRsx g;set_constants(g,r,40);std::vector<Ins> p;p.push_back(alu(1,0,C(0)));p.push_back(bri(0));p.push_back(out(0,C(1)));p.back().end=true;put_all(g,p);
  expect_error(vk,g,0,"endless loop (budget)");}
 {NativeRsx g;set_constants(g,r,40);std::vector<Ins> p;p.push_back(bri(600));p.push_back(out(0,C(1)));p.back().end=true;put_all(g,p);
  expect_error(vk,g,0,"taken branch outside the program");}
 {NativeRsx g;set_constants(g,r,40);g.transform_constants_written[30*4+2]=false;   // constant 30 incomplete, read only when bool bit 1 is clear
  std::vector<Ins> p;p.push_back(brb(3,1,1));p.push_back(out(0,C(30)));p.push_back(out(1,C(1)));p.back().end=true;p.push_back(out(2,C(2)));p.back().end=true;put_all(g,p);
  std::vector<std::array<Vec4,16>> in(2);g.registers[0x1ff8]=2;(void)run_vertex_program(g,0,in[0]);   // branch taken: the interpreter never reads it
  const auto ok=run_gpu(vk,g,0,in,2);assert(!ok.gpu_rejected && ok.flag==0 && ok.outputs[1][2][0]==2.f);
  expect_error(vk,g,0,"unwritten constant on the executed path",0);}
 {NativeRsx g;set_constants(g,r,40);std::vector<Ins> p;p.push_back(bri(543));p.push_back(out(0,C(1)));p.back().end=true;put_all(g,p);   // pc 543 has no END: execution falls off the program
  put(g,543,alu(1,0,C(0)));
  expect_error(vk,g,0,"falling off the end of the program");}
 {NativeRsx g;set_constants(g,r,40);std::vector<Ins> p;p.push_back(bri(5));put_all(g,p);                              // reachable instruction 5 unwritten: not translated
  assert(!rsxvk::vertex_shader(g,0,true,true));}
 {NativeRsx g;set_constants(g,r,40);Ins bra;bra.sca=8;bra.end=true;put(g,0,bra);assert(!rsxvk::vertex_shader(g,0,true,true));      // BRA through an address register stays on the CPU
  Ins cli;cli.sca=11;cli.end=true;put(g,0,cli);assert(!rsxvk::vertex_shader(g,0,true,true));}                           // CLI has no verified semantics
 {NativeRsx g;set_constants(g,r,40);std::vector<Ins> p;p.push_back(bri(2));p.push_back(out(0,C(1)));p.push_back(out(1,C(1)));p.back().end=true;put_all(g,p);
  assert(!rsxvk::vertex_shader(g,0,true,false));}                                                                       // control flow needs the checked mode
 std::puts("PASS error conditions raise the GPU error flag exactly where the interpreter stops; BRA/CLI/unwritten reachable instructions and unchecked mode are not translated");
}
// ---- instruction budget: run_vertex_program executes at most 65,536 instructions; the 65,537th stops the program ----
// A counted loop whose executed-instruction count is known by construction (not by running either implementation):
//   t6 = 0; do { 4 x MOV (or CAL to a 2 x MOV + RET subroutine) ; t6.x += 1 ; cc1.x = t6.x < c30.x } while (cc1.x > 0); [extra x MOV]; o0 = t6, END
// plain loop: 1 + n*(4+3) + extra + 1 executed instructions; call loop: 1 + n*(1+2+1+3) + extra + 1. With n = 9362: 65,536 + extra.
// The old GPU budget counted switch dispatches (one per taken back branch = ~9362), so it accepted 65,537 and more.
static std::vector<Ins> counted_program(bool calls,unsigned extra){
 std::vector<Ins> p;p.push_back(alu(1,6,C(0)));
 const unsigned head=unsigned(p.size());unsigned call_at=0;
 if(calls){call_at=unsigned(p.size());p.push_back(cal(0));}
 else for(unsigned k=0;k<4;++k)p.push_back(alu(1,5,C(1)));
 p.push_back(alu(3,6,T(6),C(1),Src{},8));
 p.push_back(set_cc(11,T(6,0x00),C(30,0x00),8,1));
 Ins back=bri(head,4,0x00);back.cc_index=1;p.push_back(back);
 for(unsigned k=0;k<extra;++k)p.push_back(alu(1,5,C(1)));
 p.push_back(out(0,T(6),8));p.back().end=true;
 if(calls){p[call_at].imm=unsigned(p.size());p.push_back(alu(1,5,C(1)));p.push_back(alu(1,5,C(1)));p.push_back(ret());}
 return p;
}
static void budget_boundary(NativeVulkan& vk){
 for(const bool calls:{false,true}){
  std::uint32_t r=17;
  auto setup=[&](NativeRsx& g,unsigned extra){set_constants(g,r,40);for(unsigned c=0;c<4;++c){g.transform_constants[30*4+c]=float_bits(9362.f);g.transform_constants_written[30*4+c]=true;}
   put_all(g,counted_program(calls,extra));};
  {NativeRsx g;setup(g,0);   // exactly 65,536 instructions: both implementations finish with t6 = 9362
   const auto in=make_inputs(r,3);compare(vk,g,0,in,0,"65,536 executed instructions");
   const auto gpu=run_gpu(vk,g,0,in,0);if(gpu.outputs[0][0][0]!=9362.f)throw std::runtime_error("65,536-instruction loop: o0.x "+std::to_string(gpu.outputs[0][0][0])+" instead of 9362");}
  {NativeRsx g;setup(g,1);expect_error(vk,g,0,calls?"65,537 executed instructions (call loop)":"65,537 executed instructions (fall-through loop)");}   // 65,537th instruction: both stop
  {NativeRsx g;setup(g,3);expect_error(vk,g,0,"65,539 executed instructions");}
 }
 std::puts("PASS the GPU instruction budget counts every executed instruction (fall-through, calls, RET): 65,536 run, 65,537 stop, like the interpreter");
}
// ---- hardware vertex cache key covers branch targets after the first END ----
static void cache_key(){
 std::uint32_t r=5;NativeRsx g;set_constants(g,r,40);
 std::vector<Ins> p;p.push_back(bri(3));p.push_back(out(0,C(1)));p.back().end=true;p.push_back(out(1,C(1)));p.back().end=true;p.push_back(out(2,C(2)));p.back().end=true;put_all(g,p);
 const auto before=rsxgraphics::hardware_vertex(g);assert(before);
 Ins other=out(2,C(3));other.end=true;put(g,3,other);   // changes only the code behind the first END, reached by the branch
 const auto after=rsxgraphics::hardware_vertex(g);assert(after && *after!=*before);
 std::puts("PASS the hardware vertex cache key covers instructions reached only by a branch behind the first END");
}
int main(){try{NativeVulkan vk;directed(vk);errors(vk);budget_boundary(vk);random_programs(vk);cache_key();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0] + MAIN


class RsxVpFlowTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_gpu_vertex_program_control_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, LP_NUM_THREADS='4', ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=600)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 5, result.stdout)


if __name__ == '__main__':
    unittest.main()
