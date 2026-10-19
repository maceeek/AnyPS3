"""RSX raw texture addressing/conversion on GPU; filtering stays in texture GLSL.

Byte streams preserve guest endianity (BC blocks retain their LE layout).
No native host sampler substitution changes the existing remap/PCF contract.
"""
GLSL = r'''
uint raw_be(uint unit,uint at,uint n){uint v=0u;for(uint i=0u;i<n;++i)v=(v<<8u)|raw_byte(unit,at+i);return v;}
uint raw_le(uint unit,uint at,uint n){uint v=0u;for(uint i=0u;i<n;++i)v|=raw_byte(unit,at+i)<<(8u*i);return v;}
uint pot_(uint n){uint p=1u;while(p<n)p*=2u;return p;}
uint morton_(uint bw,uint bh,uint dep,uvec3 p,uint bpp,uint byte_){
 uint block=min(bpp,4u),words=bpp/block;bw=pot_(bw*words);bh=pot_(bh);dep=pot_(dep);p.x=p.x*words+byte_/block;
 uint index=0u,bit=0u;for(uint i=0u;(bw>>i)>1u || (bh>>i)>1u || (dep>>i)>1u;++i){
 if((bw>>i)>1u)index|=((p.x>>i)&1u)<<bit++;
 if((bh>>i)>1u)index|=((p.y>>i)&1u)<<bit++;
 if((dep>>i)>1u)index|=((p.z>>i)&1u)<<bit++;}return index*block+byte_%block;
}
vec3 bc_rgb(uint c){return vec3((c>>11u)&31u,(c>>5u)&63u,c&31u)/vec3(31,63,31);}
vec4 raw_texel(uint unit,uint level,uint face,int x,int y,int z){
 uint d=2064u+32u*unit,table=w[d+31u],l=table+(face*w[d+1u]+level)*8u;
 uint base=w[l],pitch=w[l+1u],bw=w[l+2u],bh=w[l+3u],border=w[l+4u],f=raw_format(unit),swz=w[l+6u],dep=w[l+7u];
 uvec3 p=uvec3(x,y,z);p.xy+=border;
 precise vec4 c;
 if(f==0x86u || f==0x87u || f==0x88u){
  uint block=f==0x86u?8u:16u,group=(p.z/4u)*4u,count=min(4u,dep-group);
  uint b=base+group*pitch*bh+((p.y/4u)*pitch+(p.x/4u)*block)*count+(p.z%4u)*block;
  uint color=b+(f==0x86u?0u:8u),a0=raw_le(unit,color,2u),a1=raw_le(unit,color+2u,2u);
  uint index=(raw_byte(unit,color+4u+p.y%4u)>>(2u*(p.x%4u)))&3u;
  precise vec3 first=bc_rgb(a0),last=bc_rgb(a1),rgb;float alpha=1;
  if(index==0u)rgb=first;else if(index==1u)rgb=last;
  else if(a0>a1 || f!=0x86u)rgb=index==2u?(2.0*first+last)/3.0:(first+2.0*last)/3.0;
  else if(index==2u)rgb=(first+last)/2.0;else{rgb=vec3(0);alpha=0;}
  if(f==0x87u)alpha=float((raw_byte(unit,b+(p.y%4u)*2u+(p.x%4u)/2u)>>(4u*(p.x&1u)))&15u)/15.0;
  else if(f==0x88u){uint lo=raw_byte(unit,b),hi=raw_byte(unit,b+1u),bit=3u*((p.y%4u)*4u+p.x%4u),at=b+2u+bit/8u;
   uint ai=(raw_le(unit,at,min(2u,8u-(at-b)))>>(bit%8u))&7u;float a;
   if(ai==0u)a=float(lo);else if(ai==1u)a=float(hi);else if(lo>hi)a=float((8u-ai)*lo+(ai-1u)*hi)/7.0;
   else if(ai<6u)a=float((6u-ai)*lo+(ai-1u)*hi)/5.0;else a=ai==6u?0:255;alpha=a/255.0;}
  c=vec4(rgb,alpha);
 }else{
  uint bpp=f==0x81u?1u:f==0x9bu?16u:f==0x9au?8u:(f==0x85u || f==0x90u || f==0x91u || f==0x95u || f==0x9cu || f==0x9eu || f==0x9fu || f==0x8du || f==0x8eu)?4u:2u;
  uint at=base+(p.z*bh+p.y)*pitch+p.x*bpp;
  if(f==0x8du || f==0x8eu)at=base+(p.z*bh+p.y)*pitch+(p.x/2u)*4u;
  if(swz!=0u)at=base+morton_(bw,bh,dep,p,bpp,0u);
  if(f==0x9au || f==0x9bu){uint n=f==0x9au?2u:4u;for(uint i=0u;i<4u;++i){uint addr=swz!=0u?base+morton_(bw,bh,dep,p,bpp,i*n):at+i*n,v=raw_be(unit,addr,n);c[i]=n==2u?unpackHalf2x16(v).x:uintBitsToFloat(v);}}
  else if(f==0x9cu)c=vec4(uintBitsToFloat(raw_be(unit,at,4u)));
  else if(f==0x94u){float v=float(raw_be(unit,at,2u))/65535.0;c=vec4(1,v,1,v);}
  else if(f==0x95u){float a=float(raw_be(unit,at,2u))/65535.0,b=float(raw_be(unit,at+2u,2u))/65535.0;c=vec4(a,b,a,b);}
  else if(f==0x9fu){float a=unpackHalf2x16(raw_be(unit,at,2u)).x,b=unpackHalf2x16(raw_be(unit,at+2u,2u)).x;c=vec4(b,a,b,a);}
  else if(f==0x98u || f==0x99u){vec2 v=vec2(raw_byte(unit,at),raw_byte(unit,at+1u));if(f==0x99u)v=max(vec2(-1),(v-vec2(greaterThanEqual(v,vec2(128)))*256.0)/127.0);else v/=255.0;c=v.xyxy;}
  else if(f==0x8du)c=vec4(vec3(raw_byte(unit,at+((p.x&1u)!=0u?3u:1u)),raw_byte(unit,at+2u),raw_byte(unit,at))/255.0,1);
  else if(f==0x8eu)c=vec4(vec3(raw_byte(unit,at+((p.x&1u)!=0u?2u:0u)),raw_byte(unit,at+3u),raw_byte(unit,at+1u))/255.0,1);
  else if(f==0x91u || f==0x93u)c=vec4(f==0x91u?uintBitsToFloat((raw_be(unit,at,4u)>>8u)<<7u):uintBitsToFloat(raw_be(unit,at,2u)<<11u)*uintBitsToFloat(0x77000000u));
  else if(f==0x90u || f==0x92u)c=vec4(f==0x90u?float(raw_be(unit,at,4u)>>8u)/16777215.0:float(raw_be(unit,at,2u))/65535.0);
  else if(f==0x85u || f==0x9eu)c=vec4(vec3(raw_byte(unit,at+1u),raw_byte(unit,at+2u),raw_byte(unit,at+3u))/255.0,f==0x85u?float(raw_byte(unit,at))/255.0:1.0);
  else if(f==0x81u)c=vec4(vec3(float(raw_byte(unit,at))/255.0),1);
  else if(f==0x8bu)c=vec4(raw_byte(unit,at+1u),raw_byte(unit,at),raw_byte(unit,at+1u),raw_byte(unit,at))/255.0;
  else{uint v=raw_be(unit,at,2u);
   if(f==0x8fu)c=vec4(vec3((v>>10u)&63u,(v>>5u)&31u,v&31u)/vec3(63,31,31),1);
   else if(f==0x84u)c=vec4(vec3((v>>11u)&31u,(v>>5u)&63u,v&31u)/vec3(31,63,31),1);
   else if(f==0x97u)c=vec4(vec3((v>>11u)&31u,(v>>6u)&31u,(v>>1u)&31u)/31.0,float(v&1u));
   else if(f==0x9du || f==0x82u)c=vec4(vec3((v>>10u)&31u,(v>>5u)&31u,v&31u)/31.0,f==0x82u?float(v>>15u):1.0);
   else c=vec4((v>>8u)&15u,(v>>4u)&15u,v&15u,v>>12u)/15.0;}
 }
 // SNORM conversion precedes filtering; border conversion stays host metadata.
 if(w[d+29u]==0u && (w[d+11u]&1u)!=0u){uint bits=(w[d+11u]&8u)!=0u?16u:8u,full=(1u<<bits)-1u,sign=1u<<(bits-1u);
  for(uint i=0u;i<4u;++i)if((w[d+12u]&(1u<<((i+1u)%4u)))!=0u){int raw=int(floor(clamp(c[i],0,1)*float(full)+.5));int v=raw>=int(sign)?raw-int(full)-1:raw;c[i]=max(-1.0,float(v)/float(sign-1u));}}
 return c;
}
'''
