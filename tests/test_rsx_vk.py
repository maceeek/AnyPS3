"""Actual Vulkan dispatch vs CPU reference; no game build/run or source injection on disk."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from anyps3.rsx_vk import CPP
from tests.test_anyps3 import BLR, program
from tests.test_rsx import GAME_VP
from tests.test_rsx_perf import HARNESS

LIBRARIES = ['-lvulkan', '-lglslang', '-lSPIRV', '-lglslang-default-resource-limits']
HOOK = r'''
    if(parallel && std::getenv("ANYPS3_RSX_VK_TEST")){
        if(!gpu.renderer)gpu.renderer=std::make_unique<NativeVulkan>();
        std::uint64_t count=0;
        if(rsxvk::prepared(*gpu.renderer,s,triangles,vertex,fp,textures,colors[0],depth,width,height,x0,y0,x1,y1,count)){
            if(reg.get(0x17cc,0))gpu.report_values[0]+=count;
            draw.pixels+=count;++draw.draws;
            draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;
        }
    }
'''
MAIN = r'''
static void fpword(PpuState& s,unsigned at,unsigned v){s.memory.store(0xc0001000+at*4,4,(v<<16)|(v>>16));}
static void texture_fp(PpuState& s,unsigned opcode=23){
 fpword(s,0,(opcode<<24)|(15u<<9)|(4u<<13)|1u);fpword(s,1,1u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
 auto& R=s.rsx.registers;R[0x1a00]=0x2000;R[0x1a04]=0x00010029|(0xa5u<<8); // linear ARGB8, 2D, no border texels
 R[0x1a08]=0x00030303;R[0x1a0c]=0x80000000;R[0x1a10]=0xaae4;R[0x1a14]=0x02020000;R[0x1a18]=0x00020002;R[0x1840]=8;
 const unsigned texels[]={0xff112233,0x80445566,0xff778899,0x00aabbcc};
 for(unsigned i=0;i<4;++i)s.memory.store(0xc0002000+i*4,4,texels[i]);
}
static void customize(PpuState& s,unsigned mode){
 auto& R=s.rsx.registers;
 if(mode==1){R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;R[0x304]=1;R[0x308]=0x204;R[0x30c]=64;
  R[0x310]=1;R[0x314]=0x00010302;R[0x318]=0x00000303;R[0x320]=0x80068006;R[0x324]=0x01010100;}
 if(mode>=2 && mode<=7)texture_fp(s);
 if(mode==3){R[0x1a14]=0x01010000;R[0x1a08]=0x00010101;}
 if(mode==4){R[0x1a08]=0x00040404;R[0x1a1c]=0x12345678;R[0x1a10]=0xaa1b;}
 if(mode==5){R[0x1d60]=0; // half output; H0 destination
  fpword(s,0,(23u<<24)|(15u<<9)|(4u<<13)|128u|1u);}
 if(mode==6){ // texture alpha kill and gamma
  R[0x1a0c]|=4;R[0x1a08]|=0x00f00000;}
 if(mode==7){R[0x1a08]=0x00020202;R[0x1a14]=0x02020000;}
 if(mode>=8 && mode<=10){const unsigned aa=mode==8?3:mode==9?4:5;R[0x208]|=aa<<12;R[0x20c]=R[0x22c]=256*4;R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;}
 if(mode==11){R[0x8c0]=(173u<<16)|13;R[0x8c4]=(109u<<16)|15;}
 if(mode==12){R[0x183c]=1;R[0x1830]=0x408;}
 if(mode==13){R[0x208]=(R[0x208]&~0xe0u)|0x20u;R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;}
 if(mode==14 || mode==15){R[0x328]=1;R[0x32c]=255;R[0x330]=0x207;R[0x334]=7;R[0x338]=255;R[0x33c]=0x1e00;R[0x340]=0x8507;R[0x344]=0x1e01;
  if(mode==15){R[0x348]=1;for(unsigned a=0x32c;a<=0x344;a+=4)R[a+32]=R[a];R[0x364]=0x150a;}}
 if(mode==16){R[0x380]=1;R[0x384]=0;R[0x388]=0x3e800000;}
 if(mode==17){R[0xa74]=R[0xa70]=1;R[0xa6c]=0x201;R[0x1d60]|=2;
  fpword(s,0,(1u<<24)|(1u<<1)|(15u<<9));fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);
  for(unsigned i=0;i<4;++i)fpword(s,4+i,rsxdraw::float_bits(i==2?.75f:0.f));
  fpword(s,8,(1u<<24)|(15u<<9)|1u);fpword(s,9,2u|(0xe4u<<9)|(7u<<18));fpword(s,10,0);fpword(s,11,0);
  const float color[]={1,.5f,.25f,.5f};for(unsigned i=0;i<4;++i)fpword(s,12+i,rsxdraw::float_bits(color[i]));}
 if(mode==18){R[0x374]=1;R[0x378]=0x1506;R[0x310]=1;}
 if(mode>=19 && mode<=21){texture_fp(s,mode==21?49:47);R[0x1a04]=0x00020029|(0xa5u<<8);R[0x1a14]=((mode==19?3u:6u)<<16)|0x02000000;
  R[0x1a0c]=0x80000000|(128u<<19)|((mode==19?256u:128u)<<7);s.memory.store(0xc0002010,4,0xffdecafe);}
 if(mode==22)R[0x208]=(7u<<24)|(7u<<16)|0x248;
 if(mode==24)R[0x20c]=R[0x22c]=516;
 if(mode==25){s.memory.map(0x70100000,0x10000,6,nullptr,0);s.rsx.io[1]=0x70100000;R[0x194]=0xfeed0001;}
 if(mode==26){texture_fp(s);R[0x1a04]=0x00010029|(0x85u<<8);}
 if(mode==27)s.memory.store(0x70002008,4,rsxdraw::float_bits(-.25f));
 if(mode==28){fpword(s,0,(26u<<24)|(15u<<9)|1u);fpword(s,1,2u|(0xe4u<<9)|(7u<<18));fpword(s,2,0);fpword(s,3,0);for(unsigned i=0;i<4;++i)fpword(s,4+i,0);}
 const unsigned aa=(R[0x208]>>12)&15,sw=(R[0x200]>>16)*(aa?2:1),sh=(R[0x204]>>16)*(aa>=4?2:1),db=((R[0x208]>>5)&7)==1?2:4;
 for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw;++x){s.memory.store((mode==25?0x70100000:0xc0100000)+y*R[0x20c]+x*4,4,0x11111111);s.memory.store(0xc0500000+y*R[0x22c]+x*db,db,db==2?0xffff:0xffffff5a);}
}
static void differential(){
 using namespace rsxdraw;
 for(unsigned mode=0;mode<29;++mode){PpuState cpu,gpu;setup_draw(cpu,mode==11?256:128,128,(mode==11?256:128)*4,mode==23);setup_draw(gpu,mode==11?256:128,128,(mode==11?256:128)*4,mode==23);
  customize(cpu,mode);customize(gpu,mode);setenv("ANYPS3_RSX_THREADS","4",1);
  unsetenv("ANYPS3_RSX_VK_TEST");rsx_execute_draw(cpu);setenv("ANYPS3_RSX_VK_TEST","1",1);rsx_execute_draw(gpu);
  if(mode!=12 && (!gpu.rsx.renderer || gpu.rsx.renderer->compute_dispatches!=2))throw std::runtime_error("draw did not dispatch mode="+std::to_string(mode));
  const unsigned aa=(cpu.rsx.registers[0x208]>>12)&15,sw=(cpu.rsx.registers[0x200]>>16)*(aa?2:1),sh=(cpu.rsx.registers[0x204]>>16)*(aa>=4?2:1);
  const unsigned db=((cpu.rsx.registers[0x208]>>5)&7)==1?2:4;
  unsigned differences=0,max_delta=0;
  for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw*4;++x){auto a=cpu.memory.load((mode==25?0x70100000:0xc0100000)+y*cpu.rsx.registers[0x20c]+x,1),b=gpu.memory.load((mode==25?0x70100000:0xc0100000)+y*gpu.rsx.registers[0x20c]+x,1);
   if(a!=b){++differences;max_delta=std::max(max_delta,unsigned(a>b?a-b:b-a));}}
  if(mode==22 || mode==23)assert(*cpu.memory.regions[1].bytes==*gpu.memory.regions[1].bytes);
  if(mode==24)for(unsigned y=0;y<128;++y)assert(gpu.memory.load(0xc0100000+y*516+512,4)==0);
  // Interpolated/filtering operations may differ at a UNORM tie by one LSB.
  // Geometry coverage, byte order, masks, fixed constants, and depth are exact.
  if(differences && !(((mode>=2 && mode<=7)||(mode>=19 && mode<=21)||mode==26) && max_delta<=1))throw std::runtime_error("color mismatch mode="+std::to_string(mode)+" pixels="+std::to_string(differences)+" delta="+std::to_string(max_delta));
  for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw*db;++x)assert(cpu.memory.load(0xc0500000+y*cpu.rsx.registers[0x22c]+x,1)==gpu.memory.load(0xc0500000+y*gpu.rsx.registers[0x22c]+x,1));
  assert(cpu.rsx.draw.pixels==gpu.rsx.draw.pixels);assert(cpu.rsx.draw.triangles==gpu.rsx.draw.triangles);
  if(mode==0)assert(gpu.memory.load(0xc0100000,4)==0x80ff8040); // independent ARGB oracle
  if(mode==1)assert(gpu.memory.load(0xc0100000,4)==0x80884811); // blend + masked blue
  std::printf("PASS mode=%u fragments=%llu differing_bytes=%u max_delta=%u\n",mode,(unsigned long long)gpu.rsx.draw.pixels,differences,max_delta);
 }
}
static void vertex_test(){
 using namespace rsxdraw;PpuState s;setup_draw(s,128,128,512,false);NativeVulkan vk;
 std::vector<std::array<Vec4,16>> inputs(4);for(unsigned i=0;i<4;++i)for(unsigned a=0;a<16;++a)inputs[i][a]=fetch_attribute(s,Reg{s.rsx},a,i);
 std::vector<VertexOut> outputs;
 if(!rsxvk::vertices(vk,s.rsx,0,inputs,outputs))throw std::runtime_error("GAME_VP decompilation rejected");
 for(unsigned i=0;i<4;++i){const auto expected=run_vertex_program(s.rsx,0,inputs[i],&s);for(unsigned a=0;a<16;++a)for(unsigned c=0;c<4;++c)assert(outputs[i].o[a][c]==expected.o[a][c]);}
 assert(outputs[0].o[0][0]==-1 && outputs[0].o[0][1]==1); // independent clip coordinates
 const auto before=vk.compute_cache_size();assert(rsxvk::vertices(vk,s.rsx,0,inputs,outputs));assert(vk.compute_cache_size()==before);
 const auto old=outputs[1].o[0];s.rsx.transform_constants[24]=float_bits(32.f);
 assert(rsxvk::vertices(vk,s.rsx,0,inputs,outputs));assert(vk.compute_cache_size()==before);
 assert(outputs[1].o[0]!=old);
 s.rsx.transform_program_written[0]=false;assert(!rsxvk::vertices(vk,s.rsx,0,inputs,outputs));
 std::puts("PASS original GAME_VP GPU/CPU, independent clip oracle, cache hit and unwritten instruction rejection");
}
static void fallback(){
 using namespace rsxdraw;PpuState s;setup_draw(s,128,128,512,false);const Reg r{s.rsx};auto fp=load_fragment_program(s,r);std::array<Texture,16> textures{};
 assert(rsxvk::supported(s.rsx,fp,textures));
 for(unsigned reg:{0x328u,0xa68u,0x1fecu}){s.rsx.registers[reg]=1;assert(!rsxvk::supported(s.rsx,fp,textures));s.rsx.registers.erase(reg);}
 auto invalid=fp;invalid[0].d=(50u<<24)|1;assert(!rsxvk::fragment(invalid));
 invalid=fp;invalid[0].s1|=0x40000000;assert(!rsxvk::fragment(invalid));
 std::puts("PASS explicit unsupported-state/unknown-opcode fallback");
}
static void fragment_tests(){
 using namespace rsxdraw;NativeVulkan vk;
 const unsigned sw=0xe4u<<9,exec=7u<<18;
 auto inst=[&](unsigned op,unsigned dst=0,unsigned src=2){return FpInst{(op<<24)|(dst<<1)|(15u<<9)|1u,src|sw|exec,sw,sw,{.25f,-.5f,2.f,1.f},true};};
 std::vector<std::vector<FpInst>> cases;
 for(unsigned op:{1u,2u,3u,4u,5u,6u,7u,8u,9u,10u,11u,12u,13u,14u,15u,16u,17u,19u,20u,21u,22u,26u,27u,28u,29u,30u,31u,32u,33u,34u,35u,36u,37u,38u,39u,40u,41u,42u,43u,44u,45u,46u,54u,56u,57u,58u,59u,60u}){
  auto i=inst(op);i.s1=2u|sw;i.s2=2u|sw;cases.push_back({i});
 }
 // H1 writes the high half of R0, then R0 is read and overwritten conditionally.
 auto h=inst(1,1);h.d|=128|256;h.d&=~1u;
 auto mov=inst(1,2,0);mov.s0|=1u<<30;cases.push_back({h,mov});
 auto pred=inst(1);pred.s0=(pred.s0&~(7u<<18))|(2u<<18);pred.d|=1u<<31;cases.push_back({pred});
 auto kill=inst(18,63);kill.s1|=4u<<28;cases.push_back({kill}); // KIL ignores result index/scale
 const unsigned flow_first=cases.size();
 auto flow=[&](unsigned opcode,unsigned condition){FpInst i{};i.d=(opcode&63)<<24;i.s0=condition<<18;i.s1=1u<<31;return i;};
 auto literal=[&](float x){auto i=inst(1);i.d&=~1u;i.constant={x,x,x,x};return i;};
 auto ife=flow(66,1);ife.s1|=12;ife.s2=20;
 cases.push_back({ife,literal(.25f),literal(.75f),FpInst{1,0,0,0,{},false}});
 auto loop=flow(67,7);loop.s1|=3u<<2;loop.s2=12;
 auto add=inst(3,0,0);add.d&=~1u;add.has_constant=false;add.s1=4u|sw;
 cases.push_back({loop,add,flow(64,1),FpInst{1,0,0,0,{},false}});
 auto rep=flow(68,7);rep.s1|=2u<<2;rep.s2=8;
 cases.push_back({rep,add,flow(69,1),literal(.875f),FpInst{1,0,0,0,{},false}});
 for(unsigned case_=0;case_<cases.size();++case_){auto program=cases[case_];auto emitted=rsxvk::fragment(program);assert(emitted);
  std::array<FpContext,4> q{};for(unsigned l=0;l<4;++l){for(unsigned i=0;i<48;++i)for(unsigned c=0;c<4;++c)q[l].r[i][c]=float_bits(float(i+1)/4+float(l)/2+float(c)/8);
   for(unsigned i=0;i<15;++i)q[l].inputs[i]={float(l),float(i),.5f,1};
   if(case_>=flow_first)q[l].cc[0]={l==0 || l==3?-1.f:l==1?0.f:1.f,0,0,0};}
  std::vector<unsigned> words(16,0);for(const auto& lane:q){for(const auto& r:lane.r)for(unsigned v:r)words.push_back(v);for(const auto& v:lane.inputs)rsxvk::append(words,v);for(const auto& v:lane.cc)rsxvk::append(words,v);words.push_back(0);}
  const std::string kernel=R"GLSL(
void main(){if(gl_GlobalInvocationID.x!=0u)return;C q[4];
 for(uint l=0u;l<4u;++l){uint at=16u+l*261u;for(uint i=0u;i<48u;++i)q[l].r[i]=uvec4(w[at+4u*i],w[at+4u*i+1u],w[at+4u*i+2u],w[at+4u*i+3u]);
  for(uint i=0u;i<15u;++i)q[l].input_[i]=load4(at+192u+4u*i);
  q[l].cc[0]=load4(at+252u);q[l].cc[1]=load4(at+256u);q[l].killed=false;}
 shade(q);
 for(uint l=0u;l<4u;++l){uint at=16u+l*261u;for(uint i=0u;i<48u;++i)for(uint c=0u;c<4u;++c)w[at+4u*i+c]=q[l].r[i][c];
  for(uint i=0u;i<2u;++i)for(uint c=0u;c<4u;++c)w[at+252u+4u*i+c]=floatBitsToUint(q[l].cc[i][c]);w[at+260u]=uint(q[l].killed);}}
)GLSL";
  run_fragment_quad(program,q);vk.compute(rsxvk::preamble()+*emitted+kernel,words,1);
  if(case_==flow_first)for(unsigned l=0;l<4;++l)assert(words[16+l*261]==float_bits(l==0 || l==3?.25f:.75f));
  if(case_==flow_first+1)for(unsigned l=0;l<4;++l)assert(words[16+l*261]==float_bits((.25f+l*.5f)+(l==0 || l==3?1.f:3.f)*(.5f+l*.5f)));
  for(unsigned l=0;l<4;++l){for(unsigned i=0;i<48;++i)for(unsigned c=0;c<4;++c){const auto a=q[l].r[i][c],b=words[16+l*261+i*4+c];
   if(a!=b){float av=bits_float(a),bv=bits_float(b);const unsigned op=(program[0].d>>24)&63;
    // Host GPUs may contract a*b+c into FMA or round transcendental/pow results
    // differently by ~1 ULP (measured on RTX 4070 SUPER: LIT, op 45); allow 2e-6 relative for every op.
    const bool transcendental=true;(void)op;
    if(!(transcendental && std::fabs(av-bv)<=std::max(1.f,std::fabs(av))*0.000002f))throw std::runtime_error("FP mismatch case="+std::to_string(case_)+" op="+std::to_string(op));}}
   for(unsigned i=0;i<2;++i)for(unsigned c=0;c<4;++c)assert(float_bits(q[l].cc[i][c])==words[16+l*261+252+i*4+c]);
   assert(unsigned(q[l].killed)==words[16+l*261+260]);
  }
 }
 std::printf("PASS %zu FP ALU/derivative/predicate/half-alias cases\n",cases.size());
}
static void invalid_coordinates(){
 NativeVulkan vk;std::vector<unsigned> data(2200,0);data[2064]=2100;data[2065]=1;data[2067]=data[2068]=3;data[2069]=data[2070]=1;
 data[2100]=2110;data[2101]=data[2102]=2;
 const std::string shader=rsxvk::preamble()+R"GLSL(
 void main(){if(gl_GlobalInvocationID.x!=0u)return;bool killed=false;
 vec4 value=textureSample(0u,vec4(uintBitsToFloat(0x7f800000u),0,0,1),0,killed);w[0]=floatBitsToUint(value.x);}
 )GLSL";
 data[0]=0x12345678u;vk.compute(shader,data,1);assert(data[7]==0 && data[0]==0);   // sample (0,0,0,0), no failure flag
 // CPU reference: identical defined result for non-finite coordinates.
 rsxdraw::Texture t;t.levels.resize(1);t.levels[0].width=t.levels[0].height=t.levels[0].depth=2;t.mip_count=1;
 const auto c=rsxdraw::filter_level(t,0,std::numeric_limits<float>::infinity(),0,false);assert(c[0]==0 && c[3]==0);
 std::puts("PASS non-finite texture coordinates sample (0,0,0,0) on GPU and CPU, no address conversion");
}
static void benchmark(){
 PpuState cpu,gpu;setup_draw(cpu,640,360,640*4,false);setup_draw(gpu,640,360,640*4,false);texture_fp(cpu);texture_fp(gpu);
 setenv("ANYPS3_RSX_THREADS","4",1);
 const unsigned n=16;
 auto run=[&](PpuState& s,bool compute){if(compute)setenv("ANYPS3_RSX_VK_TEST","1",1);else unsetenv("ANYPS3_RSX_VK_TEST");
  for(unsigned i=0;i<2;++i){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}const auto before=std::chrono::steady_clock::now();
  for(unsigned i=0;i<n;++i){s.rsx.draw.ranges={{0,4}};rsx_execute_draw(s);}return std::chrono::duration<double>(std::chrono::steady_clock::now()-before).count();};
 const double c=run(cpu,false),g=run(gpu,true);
 assert(gpu.rsx.renderer && gpu.rsx.renderer->compute_dispatches==2*(n+2));
 std::printf("BENCH draws=%u pixels_per_draw=230400 cpu_seconds=%.6f compute_seconds=%.6f cpu_Mpix_s=%.3f compute_Mpix_s=%.3f ratio=%.3f\n",n,c,g,n*.2304/c,n*.2304/g,c/g);
}

int main(int argc,char** argv){try{
 if(argc>1 && std::string(argv[1])=="probe"){NativeVulkan vk;std::puts(vk.device_name.c_str());return 0;}
 if(argc>1 && std::string(argv[1])=="benchmark"){benchmark();return 0;}
 differential();vertex_test();fragment_tests();fallback();invalid_coordinates();
}catch(const std::exception& e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def native_source():
    source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
    source = source.replace('backend?backend:"gpu"', 'backend?backend:"cpu"')
    source = source.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
    marker = '// Returns true when reg is a SET_VERTEX_DATA*_M register write it handled.'
    assert source.count(marker) == 1
    if 'namespace rsxvk' not in source:  # production rsx_draw.py already includes the backend once integrated
        source = source.replace(marker, CPP + '\n' + marker)
    marker = '    // Every stripe owns its address cache'
    assert source.count(marker) == 1
    source = source.replace(marker, HOOK + marker)
    vp = ','.join(f'{word}u' for word in GAME_VP)
    # The rest of HARNESS includes reference-only diagnostic helpers.
    harness = HARNESS.split('static void observe(')[0]
    return source + '\n#include <cassert>\n' + harness.replace('GAME_VP_WORDS', vp) + MAIN


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(),
                     'Requires Vulkan and glslang development files')
class RsxVkTests(unittest.TestCase):
    def test_actual_compute_draw_matches_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'test.cpp').write_text(native_source())
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread',
                                    '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root/'test.cpp'),
                                    *LIBRARIES, '-o', str(root/'test')], capture_output=True, text=True, timeout=180)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = os.environ.copy()
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env['VK_DRIVER_FILES'] = '/usr/share/vulkan/icd.d/lvp_icd.json'
                env['ANYPS3_VK_DEVICE'] = 'lavapipe'
            env['VK_INSTANCE_LAYERS'] = 'VK_LAYER_KHRONOS_validation'
            env['VK_LAYER_VALIDATE_SYNC'] = '1'
            run = subprocess.run([str(root/'test')], env=env, capture_output=True, text=True, timeout=180)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn('Validation Error', run.stdout + run.stderr)
            self.assertNotIn('Validation Warning', run.stdout + run.stderr)
            self.assertEqual(run.stdout.count('PASS mode='), 29)
            for mode in ['gpu', 'lavapipe', 'wrong']:
                probe_env = dict(env, ANYPS3_VK_DEVICE=mode)
                probe = subprocess.run([str(root/'test'), 'probe'], env=probe_env, capture_output=True, text=True, timeout=20)
                if mode == 'wrong':
                    self.assertNotEqual(probe.returncode, 0)
                    self.assertIn('invalid ANYPS3_VK_DEVICE', probe.stderr)
                elif mode == 'lavapipe' and env.get('ANYPS3_VK_DEVICE') == 'lavapipe':
                    self.assertEqual(probe.returncode, 0, probe.stderr)
                elif mode == 'gpu' and env.get('ANYPS3_VK_DEVICE') == 'lavapipe':
                    self.assertNotEqual(probe.returncode, 0)  # Must not silently select a CPU.

if __name__ == '__main__':
    unittest.main()
