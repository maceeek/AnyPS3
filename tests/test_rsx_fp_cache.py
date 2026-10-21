"""Fragment program loader: exact observed spans, content sharing and bounded eviction equal the original 16-byte loader (native oracles).

The reference below is the literal original algorithm (one Range read per 16-byte block, no content cache). Random programs
(constant blocks, lengths 1..40, VRAM and IO locations, 256-byte-chunk and 1 MiB-page straddles, a program ending at the last
readable bytes, a program without END) must decode identically; equal bytes at different addresses share one prepared program;
more than 1024 distinct programs evict without ever returning a wrong program. A miss may only touch the bytes the original parser
reads (instructions + constant blocks up to END): an observer records every read and a watched region directly behind END (a stand-in
for an adjacent dirty surface) must never be reached.
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
struct Ref {unsigned d,s0,s1,s2;Vec4 constant;bool has_constant;unsigned word_offset;};
// The original loader, verbatim apart from returning plain records.
static std::vector<Ref> reference(PpuState& s,unsigned location,std::uint32_t start){
 std::vector<Ref> program;std::uint32_t offset=start;
 auto block=[&](std::uint32_t at){
  std::array<std::uint8_t,16> bytes{};rsxdraw::read_fragment_bytes(s,location,at,bytes.data(),16);
  std::array<unsigned,4> words{};
  for(unsigned i=0;i<4;++i){const auto w=read_be(bytes.data()+4*i,4);words[i]=(w<<16)|(w>>16);}return words;
 };
 for(unsigned n=0;n<4096;++n){
  const auto words=block(offset);Ref inst{words[0],words[1],words[2],words[3],{},false,(offset-start)/4};
  offset+=16;
  const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
  if(opcode<64 && (((inst.s0&3)==2) || ((inst.s1&3)==2) || ((inst.s2&3)==2))){
   const auto constants=block(offset);for(unsigned i=0;i<4;++i)inst.constant[i]=bits_float(constants[i]);inst.has_constant=true;offset+=16;}
  program.push_back(inst);
  if(inst.d&1)return program;
 }
 throw std::runtime_error("RSX fragment program has no end");
}
static void put_word(PpuState& s,std::uint64_t address,unsigned w){s.memory.store(address,4,(w<<16)|(w>>16));}
// Writes a random program; returns its byte length. `end` false leaves the last instruction without the END bit.
static unsigned write_program(PpuState& s,std::uint64_t address,std::uint32_t& r,unsigned instructions,bool end,unsigned salt,bool constants=true){
 unsigned at=0;
 for(unsigned i=0;i<instructions;++i){
  const bool constant=lcg(r)%3==0 && constants;
  unsigned d=(lcg(r)&0xffffff00u)&~(63u<<24)&~1u;d|=(lcg(r)%40)<<24;if(i+1==instructions && end)d|=1;
  unsigned s0=(lcg(r)&~3u)|(constant?2u:1u),s1=(lcg(r)&0x7fffffffu)&~3u,s2=(lcg(r)&~3u)|1u;   // s1 bit 31 clear: opcode < 64
  put_word(s,address+at,d);put_word(s,address+at+4,s0);put_word(s,address+at+8,s1);put_word(s,address+at+12,s2);at+=16;
  if(constant){for(unsigned k=0;k<4;++k)put_word(s,address+at+4*k,rsxdraw::float_bits(float(int(lcg(r)%2000)-1000)/8.f+float(salt)));at+=16;}
 }
 return at;
}
static void same(const std::vector<Ref>& expected,const std::vector<FpInst>& actual){
 assert(expected.size()==actual.size());
 for(std::size_t i=0;i<expected.size();++i){const auto& a=expected[i];const auto& b=actual[i];
  assert(a.d==b.d && a.s0==b.s0 && a.s1==b.s1 && a.s2==b.s2 && a.has_constant==b.has_constant && a.word_offset==b.word_offset);
  for(unsigned k=0;k<4;++k){std::uint32_t x,y;std::memcpy(&x,&a.constant[k],4);std::memcpy(&y,&b.constant[k],4);assert(x==y);}}
}
static std::uint32_t value_of(unsigned location,std::uint32_t offset){return (offset&~3u)|location;}
static void random_programs(){
 PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;auto& R=s.rsx.registers;
 m.map(0x70010000,0xf0000,6,nullptr,0);m.map(0x70100000,0x100000,6,nullptr,0);s.rsx.io[1]=0x70100000;
 std::uint32_t r=4242;unsigned compared=0,erred=0;
 for(unsigned round=0;round<300;++round){
  const unsigned location=1+lcg(r)%2;                                   // 1: VRAM, 2: main memory through the IO table
  std::uint32_t offset=0x2000+16*(lcg(r)%2000);
  if(location==2 && round%7==0)offset=0x100000-16*(1+lcg(r)%20);       // straddle the 1 MiB IO page edge
  const bool end=round%23!=0;
  const unsigned base=location==1?0xc0000000u:0x70000000u;
  const unsigned length=write_program(s,base+offset,r,1+lcg(r)%40,end,round);
  R[0x8e4]=value_of(location,offset);
  std::vector<Ref> expected;std::string expected_error,actual_error;
  try{expected=reference(s,location,offset);}catch(const std::runtime_error& e){expected_error=e.what();}
  std::shared_ptr<const std::vector<FpInst>> actual;
  try{actual=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});}catch(const std::runtime_error& e){actual_error=e.what();}
  if(expected_error!=actual_error)throw std::runtime_error("round "+std::to_string(round)+": error '"+actual_error+"' vs reference '"+expected_error+"'");
  if(expected_error.empty()){same(expected,*actual);++compared;(void)length;}else ++erred;
 }
 std::printf("INFO random programs: %u equal, %u identical errors\n",compared,erred);
 assert(compared>200 && erred>=1);
 std::puts("PASS fragment program loader equals the original 16-byte loader (random programs, page and chunk straddles)");
}
static std::uintptr_t host_of(PpuState& s,std::uint64_t address){auto r=s.memory.range(address,1,false);return reinterpret_cast<std::uintptr_t>(r.parts.at(0).data);}
// Coverage of the observed reads over [lo,hi) host bytes (one flag per byte); reads outside are reported through `outside`.
struct Coverage {
 std::uintptr_t lo,hi;std::vector<char> seen;unsigned outside=0;std::vector<std::pair<std::uintptr_t,std::uint64_t>> raw;
 Coverage(std::uintptr_t l,std::uintptr_t h):lo(l),hi(h),seen(h-l,0){}
 void add(const void* p,std::uint64_t n){const auto a=reinterpret_cast<std::uintptr_t>(p);raw.push_back({a,n});
  for(std::uint64_t i=0;i<n;++i){if(a+i>=lo && a+i<hi)seen[a+i-lo]=1;else ++outside;}}
 bool equals(const Coverage& o)const{return seen==o.seen && outside==o.outside;}
};
// Only the bytes the original parser reads may be observed; a watched region directly behind END (an adjacent dirty surface) is never reached.
static void observer_spans(){
 unsigned cases=0;
 for(unsigned location=1;location<=2;++location)for(unsigned variant=0;variant<6;++variant){
  PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;auto& R=s.rsx.registers;
  m.map(0x70010000,0xf0000,6,nullptr,0);m.map(0x70100000,0x100000,6,nullptr,0);s.rsx.io[1]=0x70100000;
  const unsigned base=location==1?0xc0000000u:0x70000000u;
  std::uint32_t r=9000+location*100+variant;
  const std::uint32_t offset=0x20000+0x1000*(location*10+variant);
  const unsigned length=write_program(s,base+offset,r,1+variant*2,true,variant,variant%2==0);
  // valid-looking continuation behind END (instructions without END/constants), so a read-ahead would decode fine
  std::uint32_t tail=r+1;write_program(s,base+offset+length,tail,40,false,variant,false);
  R[0x8e4]=value_of(location,offset);
  const auto start=host_of(s,base+offset),behind=host_of(s,base+offset+length);
  assert(behind==start+length);
  // 1. coverage of the reference (original loader) and of the miss path must agree exactly and equal [start,start+length)
  Coverage ref(start-256,start+length+1024),got(start-256,start+length+1024);
  m.host_access_observer=[&](const void* p,std::uint64_t n,bool write){if(!write)ref.add(p,n);};
  (void)reference(s,location,offset);m.host_access_observer=nullptr;
  m.host_access_observer=[&](const void* p,std::uint64_t n,bool write){if(!write)got.add(p,n);};
  std::shared_ptr<const std::vector<FpInst>> loaded;
  try{loaded=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});}catch(...){m.host_access_observer=nullptr;throw;}
  m.host_access_observer=nullptr;
  for(unsigned i=0;i<length;++i)if(!ref.seen[256+i])throw std::runtime_error("reference missed a program byte (test bug)");
  for(unsigned i=256+length;i<ref.seen.size();++i)if(ref.seen[i])throw std::runtime_error("reference read behind END (test bug)");
  if(!got.equals(ref)){unsigned first=0;for(unsigned i=0;i<got.seen.size();++i)if(got.seen[i]!=ref.seen[i]){first=i;break;}
   throw std::runtime_error("location "+std::to_string(location)+" variant "+std::to_string(variant)+": observed bytes differ from the original parser (program "+std::to_string(length)+
    " bytes, first differing byte at program offset "+std::to_string(long(first)-256)+")");}
  same(reference(s,location,offset),*loaded);
  // 2. an observer watching the 256 bytes behind END must never fire; the same observer on the last program block must fire (it sees reads)
  for(unsigned watch_behind=0;watch_behind<2;++watch_behind){
   PpuState t;setup_draw(t,8,8,32,false);auto& n=t.memory;
   n.map(0x70010000,0xf0000,6,nullptr,0);n.map(0x70100000,0x100000,6,nullptr,0);t.rsx.io[1]=0x70100000;
   const std::uint32_t second=offset+0x40000+watch_behind*0x800;std::uint32_t q=r;                 // fresh address: a miss
   const unsigned len2=write_program(t,base+second,q,1+variant*2,true,variant,variant%2==0);
   std::uint32_t q2=q+1;write_program(t,base+second+len2,q2,40,false,variant,false);
   t.rsx.registers[0x8e4]=value_of(location,second);
   const auto at=host_of(t,base+second+(watch_behind?len2-16:len2));
   unsigned fired=0;
   n.host_access_observer=[&](const void*,std::uint64_t,bool write){if(!write)++fired;};
   n.host_access_observer.filtered=true;n.host_access_observer.supports_exact_bytes=true;
   n.host_access_observer.watch(reinterpret_cast<const void*>(at),watch_behind?16:256);
   try{(void)rsxdraw::load_fragment_program_shared(t,rsxdraw::Reg{t.rsx});}catch(...){n.host_access_observer=nullptr;throw;}
   n.host_access_observer.watch(reinterpret_cast<const void*>(at),watch_behind?16:256,false);n.host_access_observer=nullptr;
   if(watch_behind==0 && fired)throw std::runtime_error("a watched region directly behind END was read ("+std::to_string(fired)+" notifications) at location "+std::to_string(location));
   if(watch_behind==1 && !fired)throw std::runtime_error("the watched last block of the program was not observed (test bug)");
  }
  ++cases;
 }
 assert(cases==12);
 std::puts("PASS a miss reads exactly the original parser's bytes (VRAM and IO, with/without constants); a watched region behind END is never touched");
}
// A block whose second half faults must notify NO observer for its readable first half: the original parser validated the complete
// 16-byte Range before any observer saw a byte (RSXCPU3's two scalar loads notified the first half and then faulted).
// Spans are recorded as (host offset, size) relative to the first mapped page; the expectation is the literal Range-based reference (observed bytes).
using SpanList=std::vector<std::pair<long,std::uint64_t>>;
static void put_block(PpuState& s,std::uint64_t address,unsigned d,std::uint64_t mapped_end){   // instruction with plain input operands (no constant block)
 const unsigned w[4]={d,1,0,1};
 for(unsigned i=0;i<4;++i)if(address+4*i+4<=mapped_end)put_word(s,address+4*i,w[i]);
}
struct Admission {
 std::string error;SpanList spans;std::vector<FpInst> program;
};
static void admission_case(bool concurrent,unsigned flags_second,const char* name,unsigned blocks_before_fault,bool second_region){
 // IO page 0 -> guest 0x70200000; first region [0x70200000,+0x1000) readable; the second region (if any) continues it, or nothing does.
 // Fresh states: reference and loader never share observer state; the loader caches successful programs by register value, so the
 // two modes use different IO pages (value = page<<20 | offset | location).
 auto run=[&](bool use_loader){
  PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
  m.map(0x70200000,0x1000,6,nullptr,0);if(second_region)m.map(0x70201000,0x1000,flags_second,nullptr,0);
  const unsigned page=concurrent?1:0;s.rsx.io[page]=0x70200000;
  const std::uint64_t mapped_end=0x70201000+(second_region && (flags_second&2)?0x1000:0);   // bytes the test may store (the permission case is writable, not readable)
  // blocks_before_fault readable blocks (no END) precede the block at 0xff8, which has END
  const std::uint32_t first=0xff8-16*blocks_before_fault;
  for(unsigned i=0;i<blocks_before_fault;++i)put_block(s,0x70200000+first+16*i,0,mapped_end);
  put_block(s,0x70200000+0xff8,1,mapped_end);
  s.rsx.registers[0x8e4]=value_of(2,(page<<20)|first);
  const auto base=host_of(s,0x70200000);
  if(concurrent)m.enable_concurrency();
  Admission out;
  m.host_access_observer=[&](const void* p,std::uint64_t n,bool write){if(!write)out.spans.push_back({long(reinterpret_cast<std::uintptr_t>(p))-long(base),n});};
  try{
   if(use_loader){const auto loaded=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});out.program=*loaded;}
   else{auto ref=reference(s,2,(page<<20)|first);for(const auto& r:ref){FpInst f{r.d,r.s0,r.s1,r.s2,r.constant,r.has_constant};f.word_offset=r.word_offset;out.program.push_back(f);}}
  }catch(const std::exception& e){out.error=e.what();}
  m.host_access_observer=nullptr;
  return out;
 };
 const auto expected=run(false),actual=run(true);
 const std::string label=std::string(name)+(concurrent?" [concurrent]":" [serial]");
 if(expected.error!=actual.error)throw std::runtime_error(label+": error '"+actual.error+"' vs reference '"+expected.error+"'");
 // Range notifies at creation and again at read, so multiplicity differs by design: the observed BYTES must be equal.
 auto covered=[](const SpanList& spans){std::set<long> bytes;for(const auto& p:spans)for(std::uint64_t i=0;i<p.second;++i)bytes.insert(p.first+long(i));return bytes;};
 if(covered(expected.spans)!=covered(actual.spans)){
  std::string text=label+": observed bytes differ; reference";for(const auto& p:expected.spans)text+=" ("+std::to_string(p.first)+","+std::to_string(p.second)+")";
  text+=" loader";for(const auto& p:actual.spans)text+=" ("+std::to_string(p.first)+","+std::to_string(p.second)+")";throw std::runtime_error(text);}
 if(!expected.error.empty() && covered(actual.spans).size()%16)throw std::runtime_error(label+": a faulting block leaked a partial span");
 if(expected.error.empty()){
  if(expected.program.size()!=actual.program.size())throw std::runtime_error(label+": program length differs");
  for(std::size_t i=0;i<expected.program.size();++i)if(expected.program[i].d!=actual.program[i].d || expected.program[i].s0!=actual.program[i].s0)throw std::runtime_error(label+": program differs");
 }
 std::printf("INFO %s: %s, %zu observed spans\n",label.c_str(),expected.error.empty()?"program loaded":("error '"+expected.error.substr(0,48)+"'").c_str(),expected.spans.size());
}
static void partial_block_admission(){
 for(const bool concurrent:{false,true}){
  // 1. the first block of the program is half readable (8 bytes at the end of the only region): error, no notification at all
  admission_case(concurrent,0,"first block half readable",0,false);
  // 2. two readable blocks, then the half readable block: exactly the two blocks (16 bytes each) were observed, nothing of the faulting one
  admission_case(concurrent,0,"second block half readable",2,false);
  // 3. the block continues into an adjacent readable region: loads, observed as the two parts Range::read reports
  admission_case(concurrent,6,"block across two regions",1,true);
  // 4. the second half is mapped but not readable: permission fault, nothing observed for the first half
  admission_case(concurrent,2,"second half not readable",1,true);
 }
 // an admitted block whose observer fails is not an admission fault: the failure propagates and the observer is not called again
 for(const bool concurrent:{false,true}){
  PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
  m.map(0x70200000,0x2000,6,nullptr,0);const unsigned page=2+concurrent;s.rsx.io[page]=0x70200000;
  put_block(s,0x70200000+0x40,1,0x70202000);
  s.rsx.registers[0x8e4]=value_of(2,(page<<20)|0x40);
  if(concurrent)m.enable_concurrency();
  unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){++calls;throw std::runtime_error("observer failure");};
  std::string error;
  try{(void)rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});}catch(const std::exception& e){error=e.what();}
  m.host_access_observer=nullptr;
  if(error!="observer failure" || calls!=1)throw std::runtime_error(std::string(concurrent?"[concurrent] ":"[serial] ")+"observer failure was replayed or masked: error '"+error+"' after "+std::to_string(calls)+" observer calls");
 }
 // a block that continues into an adjacent mapping INSIDE one guest page: the scalar admission of a concurrent transfer resolves one mapping
 // and fails, but every byte is readable and Range reads both parts. The loader must take the Range path (not rethrow), loading the same
 // program and observing the same bytes as the reference, on a miss and on the following cache hit.
 for(const bool concurrent:{false,true}){
  auto run=[&](bool use_loader){
   PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
   m.map(0x70200000,0x48,6,nullptr,0);m.map(0x70200048,0xfb8,6,nullptr,0);const unsigned page=4+concurrent;s.rsx.io[page]=0x70200000;
   put_block(s,0x70200040,1,0x70201000);                      // END block at 0x40..0x4f straddles the boundary at 0x48
   s.rsx.registers[0x8e4]=value_of(2,(page<<20)|0x40);
   const auto base=host_of(s,0x70200000);if(concurrent)m.enable_concurrency();
   Admission out;
   m.host_access_observer=[&](const void* p,std::uint64_t n,bool write){if(!write)out.spans.push_back({long(reinterpret_cast<std::uintptr_t>(p))-long(base),n});};
   try{
    if(use_loader){out.program=*rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});out.spans.clear();   // miss: parser; the hit below re-reads through the bulk check
     const auto again=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});if(again->size()!=out.program.size())throw std::runtime_error("cache hit changed the program");}
    else{auto ref=reference(s,2,(page<<20)|0x40);for(const auto& r:ref){FpInst f{r.d,r.s0,r.s1,r.s2,r.constant,r.has_constant};f.word_offset=r.word_offset;out.program.push_back(f);}}
   }catch(const std::exception& e){out.error=e.what();}
   m.host_access_observer=nullptr;return out;
  };
  const auto expected=run(false),actual=run(true);
  const std::string label=std::string("same-page split mapping")+(concurrent?" [concurrent]":" [serial]");
  if(!expected.error.empty())throw std::runtime_error(label+": the reference failed (test bug): "+expected.error);
  if(!actual.error.empty())throw std::runtime_error(label+": loader error '"+actual.error+"' although Range reads the block");
  if(expected.program.size()!=1 || actual.program.size()!=1 || actual.program[0].d!=1)throw std::runtime_error(label+": program differs");
 }
 // a block split over two mappings INSIDE one guest page whose admitted transfer fails in an observer (once): the failure propagates on
 // the parser path (miss) and on the cache-hit check, and is never swallowed into a Range retry that succeeds
 for(const bool concurrent:{false,true})for(const bool hit:{false,true}){
  PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
  m.map(0x70200000,0x48,6,nullptr,0);m.map(0x70200048,0xfb8,6,nullptr,0);const unsigned page=8+2*concurrent+hit;s.rsx.io[page]=0x70200000;
  put_block(s,0x70200040,1,0x70201000);                      // END block at 0x40..0x4f straddles the mapping boundary at 0x48
  s.rsx.registers[0x8e4]=value_of(2,(page<<20)|0x40);
  if(concurrent)m.enable_concurrency();
  if(hit)(void)rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});
  unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){if(!calls++)throw std::runtime_error("observer failure");};
  std::string error;
  try{(void)rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});}catch(const std::exception& e){error=e.what();}
  const unsigned failed_calls=calls;m.host_access_observer=nullptr;
  if(error!="observer failure" || failed_calls!=1)throw std::runtime_error(std::string(concurrent?"[concurrent] ":"[serial] ")+(hit?"hit":"miss")+
     ": split-block observer failure was swallowed or replayed: error '"+error+"' after "+std::to_string(failed_calls)+" observer calls");
 }
 // a CACHE HIT whose bulk read fails in an observer: the failure propagates once and is not swallowed into a re-parse that succeeds
 for(const bool concurrent:{false,true}){
  PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
  m.map(0x70200000,0x2000,6,nullptr,0);const unsigned page=6+concurrent;s.rsx.io[page]=0x70200000;
  put_block(s,0x70200000+0x40,1,0x70202000);
  s.rsx.registers[0x8e4]=value_of(2,(page<<20)|0x40);
  if(concurrent)m.enable_concurrency();
  const auto first=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});          // populates the cache
  unsigned calls=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){if(!calls++)throw std::runtime_error("observer failure");};   // fails once, then succeeds
  std::string error;
  try{(void)rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});}catch(const std::exception& e){error=e.what();}
  const unsigned failed_calls=calls;
  m.host_access_observer=nullptr;
  if(error!="observer failure" || failed_calls!=1)throw std::runtime_error(std::string(concurrent?"[concurrent] ":"[serial] ")+"cache-hit observer failure was swallowed or replayed: error '"+error+"' after "+std::to_string(failed_calls)+" observer calls");
  if(rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx})!=first)throw std::runtime_error("the cache entry did not survive a failed validation");
 }
 // readable_span: a pure probe with the same verdict as Range admission
 for(const bool concurrent:{false,true}){
  PpuState s;setup_draw(s,8,8,32,false);auto& m=s.memory;
  m.map(0x70200000,0x1000,6,nullptr,0);m.map(0x70201000,0x1000,6,nullptr,0);m.map(0x70203000,0x1000,2,nullptr,0);
  m.map(0x70204000,0x48,6,nullptr,0);m.map(0x70204048,0xfb8,6,nullptr,0);
  if(concurrent)m.enable_concurrency();
  unsigned fired=0;m.host_access_observer=[&](const void*,std::uint64_t,bool){++fired;};
  assert(m.readable_span(0x70200ff8,16) && m.readable_span(0x70200000,0x2000) && m.readable_span(0x70200ff8,0));
  assert(!m.readable_span(0x70201ff8,16) && !m.readable_span(0x70202000,1) && !m.readable_span(0x70203000,4) && !m.readable_span(0x70202ff8,16));
  assert(!m.readable_span(0xfffffff8u,16) && !m.readable_span(0x70200000,0x3000));
  // the verdict of transfer() admission: two mappings inside one page, across pages, inside one mapping, unmapped tail
  assert(m.readable_span(0x70204040,16) && m.readable_span(0x70200ff8,16) && m.readable_span(0x70204048,16) && !m.readable_span(0x70204ff8,16));
  m.host_access_observer=nullptr;
  if(fired)throw std::runtime_error("readable_span notified an observer");
 }
 std::puts("PASS a half readable FP block notifies no observer and raises the original error; split blocks observe the Range parts; readable_span is side-effect free");
}
static void tail_of_memory(){
 // The END block is the last readable block of VRAM: nothing may be read past it.
 PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;std::uint32_t r=7;
 const std::uint32_t offset=0x800000-16*5;const auto length=write_program(s,0xc0000000u+offset,r,5,true,1,false);
 (void)length;R[0x8e4]=value_of(1,offset);
 const auto expected=reference(s,1,offset);const auto actual=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});same(expected,*actual);
 std::puts("PASS a program ending at the last readable bytes still loads");
}
static void sharing_and_eviction(){
 PpuState s;setup_draw(s,8,8,32,false);auto& R=s.rsx.registers;std::uint32_t r=99;
 // Equal bytes at five addresses share one prepared program.
 std::vector<std::shared_ptr<const std::vector<FpInst>>> shared;
 for(unsigned copy=0;copy<5;++copy){std::uint32_t again=555;write_program(s,0xc0000000u+0x4000+copy*0x400,again,12,true,3);R[0x8e4]=value_of(1,0x4000+copy*0x400);
  shared.push_back(rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx}));}
 for(unsigned copy=1;copy<5;++copy)assert(shared[copy]==shared[0]);
 // A different program at a new address is not conflated with them.
 {std::uint32_t other=556;write_program(s,0xc0000000u+0x5000,other,12,true,3);R[0x8e4]=value_of(1,0x5000);
  const auto p=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});assert(p!=shared[0]);same(reference(s,1,0x5000),*p);}
 // 3000 distinct programs (more than the 1024-entry caches), then the early ones again.
 const unsigned count=3000;std::vector<std::uint32_t> seeds(count);
 for(unsigned i=0;i<count;++i){seeds[i]=1000+i;const std::uint32_t offset=0x10000+i*0x80;
  std::uint32_t seed=seeds[i];write_program(s,0xc0000000u+offset,seed,3+i%6,true,i);R[0x8e4]=value_of(1,offset);
  const auto p=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});if(i%97==0)same(reference(s,1,offset),*p);}
 for(unsigned i=0;i<count;i+=11){const std::uint32_t offset=0x10000+i*0x80;R[0x8e4]=value_of(1,offset);
  same(reference(s,1,offset),*rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx}));}
 // Rewriting bytes at a cached address is seen by the next load.
 {std::uint32_t seed=seeds[count-1];const std::uint32_t offset=0x10000+(count-1)*0x80;R[0x8e4]=value_of(1,offset);
  const auto before=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});
  std::uint32_t changed=seed+7;write_program(s,0xc0000000u+offset,changed,3+(count-1)%6,true,count-1);
  const auto after=rsxdraw::load_fragment_program_shared(s,rsxdraw::Reg{s.rsx});assert(after!=before);same(reference(s,1,offset),*after);}
 (void)r;
 std::puts("PASS equal programs share one result; eviction past 1024 entries never returns a wrong program; rewrites are seen");
}
int main(){try{random_programs();observer_spans();partial_block_admission();tail_of_memory();sharing_and_eviction();}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    return graphics_source().split('int main(int argc,char** argv)')[0].replace('class NativeVulkan {', 'class NativeVulkan {\npublic:', 1) + MAIN


class RsxFpCacheTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('c++'), 'Requires native compiler')
    def test_fragment_program_loader_equals_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'test.cpp').write_text(native_source())
            command = ['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                       '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')]
            result = subprocess.run(command, capture_output=True, text=True, timeout=900)
            self.assertEqual(result.returncode, 0, result.stderr)
            env = dict(os.environ, ANYPS3_VK_DEVICE='lavapipe', VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json')
            result = subprocess.run([str(root / 'test')], env=env, capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.count('PASS '), 5, result.stdout)


if __name__ == '__main__':
    unittest.main()
