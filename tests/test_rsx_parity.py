"""RSXVK5: state parity with packed oracles and real Lavapipe execution."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tests.test_rsx_graphics import native_source as graphics_source
from tests.test_rsx_vk import LIBRARIES, MAIN as COMPUTE_MAIN

MAIN = r'''
static std::array<unsigned,4> parity_vp(unsigned vec,unsigned sca,unsigned dst,unsigned input,bool end=true){
 const unsigned raw=0x1b02;
 return {(63u<<15)|(7u<<10)|(1u<<30),(vec<<22)|(sca<<27)|((raw>>9)&255)|(input<<8),(raw&511)<<23,(15u<<13)|(15u<<17)|(63u<<7)|(dst<<2)|unsigned(end)};
}
static void store_vp(PpuState& s,unsigned pc,const std::array<unsigned,4>& v){for(unsigned c=0;c<4;++c){s.rsx.transform_program[pc*4+c]=v[c];s.rsx.transform_program_written[pc*4+c]=true;}}
static void append_vp(PpuState& s,unsigned vec,unsigned dst,unsigned input){
 unsigned pc=0;while(!(s.rsx.transform_program[pc*4+3]&1))++pc;
 s.rsx.transform_program[pc*4+3]&=~1u;store_vp(s,pc+1,parity_vp(vec,0,dst,input));
}
static void state_parity(){
 for(unsigned mode=0;mode<28;++mode){
  PpuState cpu,gpu;
  for(auto* s:{&cpu,&gpu}){
   setup_draw(*s,16,12,128,false);auto& r=s->rsx.registers;
   for(unsigned y=0;y<16;++y)for(unsigned x=0;x<32;++x)s->memory.store(0xc0100000+128*y+4*x,4,0x12345678);
   if(mode<16){r[0x374]=1;r[0x378]=0x1500+mode;r[0x310]=1;}
   if(mode==16){r[0x147c]=1;for(unsigned i=0;i<32;++i)r[0x1480+4*i]=i&1?0xaaaaaaaa:0x55555555;}
   if(mode==17 || mode==18){r[0x2c0]=(10u<<16)|3;r[0x2c4]=(8u<<16)|2;r[0x2bc]=mode==18;r[0x2b8]=(2u<<16)|1;}
   if(mode==19 || mode==20){r[0x1fec]=1;if(mode==20){r[0x310]=1;r[0x314]=0x03020302;r[0x318]=0x03030303;r[0x320]=0x80068006;}}
   if(mode>=21 && mode<=26){const unsigned modes[]={0x2601,0x804,0x800,0x802,0x801,0x803};r[0x8cc]=modes[mode-21];r[0x8d0]=rsxdraw::float_bits(1.25f);r[0x8d4]=rsxdraw::float_bits(.5f);
    fpword(*s,0,(1u<<24)|(15u<<9)|(3u<<13)|1u);fpword(*s,1,1u|(0xe4u<<9)|(7u<<18));fpword(*s,2,0);fpword(*s,3,0);
   }
   if(mode==27)r[0x8cc]=0x800; // dormant cellGcmInit state must not cause fallback
  }
  setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);
  setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  if(!gpu.rsx.renderer || !gpu.rsx.renderer->graphics_draws)throw std::runtime_error("state did not execute on GPU mode="+std::to_string(mode));
  unsigned max=0;
  for(unsigned y=0;y<16;++y)for(unsigned x=0;x<128;++x){const unsigned a=cpu.memory.load(0xc0100000+128*y+x,1),b=gpu.memory.load(0xc0100000+128*y+x,1);max=std::max(max,a>b?a-b:b-a);}
  if(max>2)throw std::runtime_error("state mismatch mode="+std::to_string(mode)+" delta="+std::to_string(max));
  if(mode==6)assert(gpu.memory.load(0xc0100000,4)==0x92cbd638); // XOR 80ff8040 with 12345678
  if(mode==16){assert(near_argb(gpu.memory.load(0xc0100000,4),0x80ff8040));assert(gpu.memory.load(0xc0100004,4)==0x12345678);}
  if(mode==27)assert(near_argb(gpu.memory.load(0xc0100000,4),0x80ff8040));
  std::printf("PARITY state=%u delta=%u\n",mode,max);
 }
}
static void format_parity(){
 const unsigned formats[]={1,2,3,4,5,8,9,10,11,12,13,14,15,16};
 const unsigned oracle[]={0x7e08,0xfe08,0xfc08,0x00ff8040,0xffff8040,0x80ff8040,0x40,0x8040,0x3c003800,0x3f800000,0x3f800000,0x004080ff,0xff4080ff,0x804080ff};
 for(unsigned k=0;k<14;++k)for(unsigned mode=0;mode<7;++mode){unsigned f=formats[k];if(f>=11 && f<=13 && (mode==2 || mode==3))continue;
  PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){
   setup_draw(*s,8,8,512,false);auto& r=s->rsx.registers;const unsigned b=rsxdraw::color_bytes(f);
   r[0x208]=(r[0x208]&~31u)|f;r[0x20c]=8*b+b;
   if(mode>=4 && mode<=5){r[0x208]|=(mode==4?3u:5u)<<12;r[0x20c]=16*b+b;r[0x22c]=128;}
   if(mode==6)r[0x208]=(r[0x208]&~0xf00u)|0x200u|(3u<<16)|(3u<<24);
   if(mode>=1 && mode<=3){for(unsigned y=0;y<8;++y)for(unsigned x=0;x<8*b;++x)s->memory.store(0xc0100000+y*r[0x20c]+x,1,0x55);
    if(mode==1){r[0x310]=1;r[0x314]=0x03020302;r[0x318]=0x03030303;r[0x320]=0x80068006;}
    if(mode==2){r[0x374]=1;r[0x378]=0x1506;r[0x324]=0x10101;}
    if(mode==3)r[0x1fec]=1;
   }
  }
  setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
  if(!gpu.rsx.renderer || !gpu.rsx.renderer->graphics_draws)throw std::runtime_error("format fallback f="+std::to_string(f));
  const unsigned b=rsxdraw::color_bytes(f),nx=mode==4 || mode==5?2:1,ny=mode==5?2:1;unsigned delta=0;
  for(unsigned y=0;y<8*ny;++y)for(unsigned x=0;x<8*nx;++x){const auto at=0xc0100000+y*gpu.rsx.registers[0x20c]+x*b;
   if(f<=3){const unsigned a=cpu.memory.load(at,2),v=gpu.memory.load(at,2);if(a!=v && !(f==3 && near_565(a,v)))throw std::runtime_error("packed format mismatch");}
   else if(f>=11 && f<=13){for(unsigned c=0;c<b;c+=f==11?2:4){const unsigned a=cpu.memory.load(at+c,f==11?2:4),v=gpu.memory.load(at+c,f==11?2:4);
    const float af=f==11?rsxdraw::half_float(a):rsxdraw::bits_float(a),vf=f==11?rsxdraw::half_float(v):rsxdraw::bits_float(v);
    if(std::fabs(af-vf)>std::max(1.f,std::fabs(af))*.001f)throw std::runtime_error("float format mismatch f="+std::to_string(f));}}
   else for(unsigned c=0;c<b;++c){const unsigned a=cpu.memory.load(at+c,1),v=gpu.memory.load(at+c,1);delta=std::max(delta,a>v?a-v:v-a);}
  }
  if(delta>2)throw std::runtime_error("format mismatch f="+std::to_string(f)+" mode="+std::to_string(mode)+" delta="+std::to_string(delta));
  if(mode==0 || mode>=4){const auto v=gpu.memory.load(0xc0100000,std::min(4u,b));assert(f==3?near_565(v,oracle[k]):f==8?near_argb(v,oracle[k]):v==oracle[k]);}
  if(mode!=6)for(unsigned y=0;y<8*ny;++y)assert(gpu.memory.load(0xc0100000+y*gpu.rsx.registers[0x20c]+8*nx*b,b>8?8:b)==0);
  std::printf("PARITY format=%u mode=%u delta=%u\n",f,mode,delta);
 }
}
static void texture_parity(){
 for(unsigned mode=0;mode<13;++mode){PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){
  setup_draw(*s,8,8,128,false);texture_fp(*s,47);auto& r=s->rsx.registers;
  r[0x1a04]=0x1a529;r[0x1a08]=0x30303;r[0x1a14]=0x02020000;r[0x1840]=8;
  rsxdraw::Vec4 coord{.5,.5,.5,0};
  if(mode<6){r[0x1a04]|=4;const rsxdraw::Vec4 dirs[]={{1,0,0,0},{-1,0,0,0},{0,1,0,0},{0,-1,0,0},{0,0,1,0},{0,0,-1,0}};coord=dirs[mode];
   for(unsigned face=0;face<6;++face)for(unsigned p=0;p<4;++p)s->memory.store(0xc0002000+face*16+p*4,4,0xff000000|((face+1)*0x10101));
  }else if(mode==6 || mode==7){r[0x1a04]=(r[0x1a04]&~0xf0u)|0x30;r[0x1840]=(2u<<20)|8;
   for(unsigned i=0;i<8;++i)s->memory.store(0xc0002000+i*4,4,i>=4?0xffffffff:0xff000000);
   if(mode==7)r[0x1a08]=0x40404;
  }else if(mode==8){r[0x1a04]=(r[0x1a04]&~0xf0u)|0x10;r[0x1a18]=0x20001;
  }else{r[0x1a04]=0x10029|(0x91u<<8);r[0x1a08]|=(mode==9?1u:mode==10?3u:mode==11?4u:6u)<<28;
   for(unsigned p=0;p<4;++p)s->memory.store(0xc0002000+p*4,4,p<2?0x40000000:0xc0000000);}
  fpword(*s,0,(47u<<24)|(15u<<9)|1u);fpword(*s,1,2u|(0xe4u<<9)|(7u<<18));fpword(*s,2,0);fpword(*s,3,0);
  for(unsigned c=0;c<4;++c)fpword(*s,4+c,rsxdraw::float_bits(coord[c]));
 }
 setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
 if(!gpu.rsx.renderer->graphics_draws)throw std::runtime_error("texture fallback");
 for(unsigned y=0;y<8;++y)for(unsigned x=0;x<32;++x){const auto a=cpu.memory.load(0xc0100000+y*128+x,1),b=gpu.memory.load(0xc0100000+y*128+x,1);assert(near_byte(a,b));}
 if(mode<6)assert(gpu.memory.load(0xc0100000,4)==(0xff000000|((mode+1)*0x10101)));
 if(mode==6)assert(near_argb(gpu.memory.load(0xc0100000,4),0xff808080));
 std::printf("PARITY texture=%u\n",mode);
 }
}
static void primitive_parity(){
 for(unsigned mode=0;mode<12;++mode){PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){
  setup_draw(*s,16,16,128,false);auto& r=s->rsx.registers;
  r[0x1ff4]=0x3fffdf;r[0x183c]=1;r[0x1830]=0x408;r[0x147c]=1; // all ignored by points/lines
  if(mode<3){r[0x1ee0]=rsxdraw::float_bits(float(mode==0?1:mode==1?3:4));s->rsx.draw.primitive=1;s->rsx.draw.ranges={{0,1}};
   s->memory.store(0x70002000,4,rsxdraw::float_bits(7.2f));s->memory.store(0x70002004,4,rsxdraw::float_bits(8.6f));
  }else{s->rsx.draw.primitive=2;s->rsx.draw.ranges={{0,2}};r[0x3b8]=8*(mode<7?1:3);
   const float positions[4][4]={{1,3,14,3},{3,1,3,14},{1,1,14,14},{2,13,13,3}};const auto& p=positions[(mode-3)%4];
   for(unsigned v=0;v<2;++v)for(unsigned c=0;c<2;++c)s->memory.store(0x70002000+16*v+4*c,4,rsxdraw::float_bits(p[v*2+c]));
  }
  if(mode==11){texture_fp(*s);r[0x368]=0x1d00;}
 }
 setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
 if(!gpu.rsx.renderer || !gpu.rsx.renderer->graphics_draws)throw std::runtime_error("primitive fallback");
 for(unsigned y=0;y<16;++y)for(unsigned x=0;x<64;++x){const auto a=cpu.memory.load(0xc0100000+y*128+x,1),b=gpu.memory.load(0xc0100000+y*128+x,1);
  if(!near_byte(a,b))throw std::runtime_error("primitive mismatch mode="+std::to_string(mode)+" x="+std::to_string(x)+" y="+std::to_string(y));}
 if(mode<3)assert(gpu.rsx.draw.pixels==(mode==0?1u:mode==1?9u:16u));
 assert(cpu.rsx.draw.pixels==gpu.rsx.draw.pixels);
 std::printf("PARITY primitive=%u\n",mode);
 }
}
static void advanced_parity(){
 for(unsigned mode=0;mode<11;++mode){PpuState cpu,gpu;for(auto* s:{&cpu,&gpu}){
  setup_draw(*s,16,12,128,false);auto& r=s->rsx.registers;
  unsigned input=1;
  if(mode<3){r[0x142c]=1;r[0x1ff0]|=12;
   for(unsigned a=2;a<=3;++a){r[0x1740+4*a]=0;s->rsx.draw.immediate[a].count=4;const float col[4]={a==2?1.f:0.f,a==3?1.f:0.f,0,1};
    for(unsigned c=0;c<4;++c)s->rsx.draw.immediate[a].words[c]=rsxdraw::float_bits(col[c]);}
   append_vp(*s,1,1,2);append_vp(*s,1,3,3);if(mode==1)r[0x1834]=0x900;if(mode==2)r[0x368]=0x1d00;
  }else if(mode==3){append_vp(*s,1,5,1);r[0x1478]=2;input=4;
  }else if(mode==4){r[0xb40]=1;input=4;
  }else if(mode==5 || mode==6){r[0x1744]|=2u<<16;r[0x1fc0]=mode==6?2:0;input=4;
  }else if(mode==7){for(unsigned pc=14;pc>0;--pc)for(unsigned c=0;c<4;++c){s->rsx.transform_program[pc*4+c]=s->rsx.transform_program[(pc-1)*4+c];s->rsx.transform_program_written[pc*4+c]=true;}
   auto branch=parity_vp(0,9,31,0,false);branch[3]|=1u<<29;store_vp(*s,0,branch);input=0;
  }else if(mode==8){append_vp(*s,25,1,1);r[0x900]=0x9000;r[0x904]=0x10021|(0xbcu<<8);r[0x908]=0x303;r[0x90c]=0x80000000;r[0x910]=4;r[0x918]=0x10001;s->memory.store(0xc0009000,4,rsxdraw::float_bits(.25f));
  }else if(mode==9){append_vp(*s,1,1,1);r[0x368]=0x1d00;r[0x1454]=1;
  }else{r[0x220]=0;r[0x208]&=~31u;r[0xa70]=r[0xa74]=1;r[0xa6c]=0x207;r[0x8e4]=0xffffffff;}
  if(mode!=7){fpword(*s,0,(1u<<24)|(15u<<9)|(input<<13)|1u);fpword(*s,1,1u|(0xe4u<<9)|(7u<<18));fpword(*s,2,0);fpword(*s,3,0);}
 }
 setenv("ANYPS3_RSX_BACKEND","cpu",1);rsx_execute_draw(cpu);setenv("ANYPS3_RSX_BACKEND","gpu",1);rsx_execute_draw(gpu);
 if(!gpu.rsx.renderer || !gpu.rsx.renderer->graphics_draws)throw std::runtime_error("advanced fallback mode="+std::to_string(mode));
 for(unsigned y=0;y<12;++y)for(unsigned x=0;x<64;++x){const auto a=cpu.memory.load(0xc0100000+y*128+x,1),b=gpu.memory.load(0xc0100000+y*128+x,1);
  if(!near_byte(a,b))throw std::runtime_error("advanced mismatch mode="+std::to_string(mode)+" x="+std::to_string(x)+" y="+std::to_string(y));}
 if(mode<3)assert(gpu.memory.load(0xc0100000,4)==(mode==1?0xffff0000:0xff00ff00));
 if(mode==7)assert(near_argb(gpu.memory.load(0xc0100000,4),0x80ff8040));
 if(mode==8)assert(near_argb(gpu.memory.load(0xc0100000,4),0x40404040));
 if(mode==10)assert(gpu.memory.load(0xc0500000,4)==0x40000000);
 std::printf("PARITY advanced=%u\n",mode);
 }
}
int main(){setenv("ANYPS3_RSX_GPU_FALLBACK","1",1);state_parity();format_parity();texture_parity();primitive_parity();advanced_parity();return 0;}
'''


def native_source():
    graphics = graphics_source()
    source = graphics.split('static void graphics_differential(){')[0]
    for name in ('near_byte', 'near_565', 'customize'):
        source = source.replace('static bool ' + name, '[[maybe_unused]] static bool ' + name).replace('static void ' + name, '[[maybe_unused]] static void ' + name)
    fragment = COMPUTE_MAIN.split('static void fragment_tests(){')[1].split('static void invalid_coordinates(){')[0]
    fragment = 'static void fragment_tests(){' + fragment
    feedback = 'static void graphics_vertex_feedback_fallback(){' + graphics.split('static void graphics_vertex_feedback_fallback(){')[1].split('static void graphics_dynamic_constants(){')[0]
    return source + fragment + feedback + MAIN.replace('state_parity();format_parity();', 'fragment_tests();graphics_vertex_feedback_fallback();graphics_fragment_feedback_fallback();state_parity();format_parity();')


@unittest.skipUnless(shutil.which('c++') and Path('/usr/include/glslang/Include/glslang_c_interface.h').exists(), 'Requires Vulkan/glslang')
class GraphicsParityTests(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'test.cpp').write_text(native_source())
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', '-DANYPS3_VULKAN', '-DANYPS3_RSX_VK', str(root / 'test.cpp'), *LIBRARIES, '-o', str(root / 'test')], capture_output=True, text=True, timeout=180)
            self.assertEqual(build.returncode, 0, build.stderr)
            env = dict(os.environ, VK_INSTANCE_LAYERS='VK_LAYER_KHRONOS_validation', VK_LAYER_VALIDATE_SYNC='1', ANYPS3_RSX_FALLBACK_STATS='1', ANYPS3_RSX_FALLBACK_INTERVAL='7')
            if not env.get('VK_DRIVER_FILES') and not env.get('VK_ICD_FILENAMES'):
                env.update(VK_DRIVER_FILES='/usr/share/vulkan/icd.d/lvp_icd.json', ANYPS3_VK_DEVICE='lavapipe')
            run = subprocess.run([str(root / 'test')], capture_output=True, text=True, env=env, timeout=180)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertNotIn('Validation Error', run.stdout + run.stderr)
            self.assertNotIn('Validation Warning', run.stdout + run.stderr)
            self.assertIn('RSX FALLBACK STATS phase=final', run.stderr)
            self.assertIn('RSX FALLBACK STATS phase=periodic draws=7', run.stderr)
            self.assertIn('sequential_color_texture_feedback=1', run.stderr)
            self.assertIn('sequential_vertex_feedback_or_unmapped_input=1', run.stderr)
