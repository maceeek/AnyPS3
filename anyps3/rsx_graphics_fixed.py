"""Packed integer attachments: exact CPU format, masks, logic and sRGB blend.

One immutable image snapshot per primitive avoids attachment feedback races.
Hardware still rasterizes and performs fragment/depth/stencil operations.
"""

GLSL = r'''
float srgb_decode(float x){return x<=.04045?x/12.92:pow((x+.055)/1.055,2.4);}
float srgb_encode(float x){return x<=.0031308?x*12.92:1.055*pow(x,1.0/2.4)-.055;}
vec4 packed_read(uint v,uint f){
 if(f==13u)return vec4(uintBitsToFloat(v));
 if(f<=3u){if(f==3u)return vec4(float((v>>11u)&31u)/31.0,float((v>>5u)&63u)/63.0,float(v&31u)/31.0,1);
  return vec4(vec3((v>>10u)&31u,(v>>5u)&31u,v&31u)/31.0,f==2u?1:0);}
 if(f==9u)return vec4(vec3(float(v)/255.0),1);
 if(f==10u)return vec4(v&255u,v>>8u,v&255u,v>>8u)/255.0;
 vec4 c=argb(v);if(f>=14u)c=c.bgra;
 if(f==4u || f==14u)c.a=0;if(f==5u || f==15u)c.a=1;return c;
}
uint packed_write(uint old,uint f,vec4 c,uint mask){
 uint value=0u,enabled=0u;
 if(f<=3u){uvec3 v=uvec3(rnd(clamp(c.r,0,1)*31.0),rnd(clamp(c.g,0,1)*(f==3u?63.0:31.0)),rnd(clamp(c.b,0,1)*31.0));
  value=f==3u?(v.r<<11u)|(v.g<<5u)|v.b:(f==2u?0x8000u:0u)|(v.r<<10u)|(v.g<<5u)|v.b;
  if((mask&0x10000u)!=0u)enabled|=f==3u?0xf800u:0x7c00u;
  if((mask&0x100u)!=0u)enabled|=f==3u?0x7e0u:0x3e0u;
  if((mask&1u)!=0u)enabled|=31u;if(f!=3u && (mask&0x1000000u)!=0u)enabled|=0x8000u;
 }else if(f==9u){value=rnd(clamp(c.b,0,1)*255.0);if((mask&1u)!=0u)enabled=255u;
 }else if(f==10u){value=(rnd(clamp(c.g,0,1)*255.0)<<8u)|rnd(clamp(c.b,0,1)*255.0);
  if((mask&0x100u)!=0u)enabled|=0xff00u;if((mask&1u)!=0u)enabled|=255u;
 }else{
  uvec4 v=uvec4(rnd(clamp(c.r,0,1)*255.0),rnd(clamp(c.g,0,1)*255.0),rnd(clamp(c.b,0,1)*255.0),rnd(clamp(c.a,0,1)*255.0));
  if(f==4u || f==14u)v.a=0u;if(f==5u || f==15u)v.a=255u;
  for(uint i=0u;i<4u;++i){uint shift=i==3u?24u:i==1u?8u:i==0u?(f>=14u?0u:16u):(f>=14u?16u:0u);
   value|=v[i]<<shift;uint m=i==3u?0x1000000u:i==0u?0x10000u:i==1u?0x100u:1u;
   if((mask&m)!=0u)enabled|=255u<<shift;}
 }
 if((R(0x374u)&1u)!=0u){uint op=R(0x378u);value=op==0x150fu && f<=3u?255u:logic_(op,value,old);}
 return (old&~enabled)|(value&enabled);
}
uint packed_finish(vec4 src,uint old,uint target){
 uint f=R(0x208u)&31u;if(f!=13u)src=cpu_clamp(src,vec4(0),vec4(1));
 bool blend=(R(0x374u)&1u)==0u && (target==0u?(R(0x310u)&1u)!=0u:(R(0x36cu)&(1u<<target))!=0u);
 precise vec4 dst=packed_read(old,f),result=src;
 if(R(0x1fecu)!=0u && blend)for(uint c=0u;c<3u;++c)dst[c]=srgb_decode(dst[c]);
 if(blend){vec4 k=argb(R(0x31cu));for(uint c=0u;c<4u;++c){
  uint sf=c==3u?R(0x314u)>>16u:R(0x314u)&65535u,df=c==3u?R(0x318u)>>16u:R(0x318u)&65535u;
  uint eq=c==3u?R(0x320u)>>16u:R(0x320u)&65535u;
  precise float a=src[c]*blendFactor(sf,src,dst,k,c),b=dst[c]*blendFactor(df,src,dst,k,c);
  float v=eq==0x8006u?a+b:eq==0x800au?a-b:eq==0x800bu?b-a:eq==0x8007u?min(src[c],dst[c]):max(src[c],dst[c]);result[c]=f==13u?v:clamp(v,0,1);
 }}
 if(R(0x1fecu)!=0u)for(uint c=0u;c<3u;++c)result[c]=clamp(srgb_encode(result[c]),0,1);
 uint mask=R(0x324u);if(target!=0u){uint n=(R(0x370u)>>(target*4u))&15u;
  mask=((n&1u)!=0u?0x1000000u:0u)|((n&2u)!=0u?0x10000u:0u)|((n&4u)!=0u?0x100u:0u)|((n&8u)!=0u?1u:0u);}
 if(f==13u)return (mask&0x10000u)!=0u?floatBitsToUint(result.r):old;
 return packed_write(old,f,result,mask);
}
'''
