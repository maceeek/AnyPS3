"""Graphics texture filtering following rsx_tex (decoded texels, GPU sampling).

The upload retains depth and all six faces; shadow comparison precedes PCF.
"""

GLSL = r'''
vec3 cube_(vec3 c){
 vec3 a=abs(c);float m,u,v;uint face;
 if(a.x>=a.y && a.x>=a.z){m=a.x;face=c.x>=0?0u:1u;u=c.x>=0?-c.z:c.z;v=-c.y;}
 else if(a.y>=a.z){m=a.y;face=c.y>=0?2u:3u;u=c.x;v=c.y>=0?c.z:-c.z;}
 else{m=a.z;face=c.z>=0?4u:5u;u=c.z>=0?c.x:-c.x;v=-c.y;}
 if(!(m>0) || isinf(m)){atomicOr(w[7],1u);return vec3(0);}
 return vec3((u/m+1.0)*.5,(v/m+1.0)*.5,float(face));
}
vec4 texel_(uint unit,uint level,int x,int y,int z,uint face,float ref){
 uint d=2064u+32u*unit,l=texture_offset(w[d])+(face*w[d+1u]+level)*4u;bool border=false;
 if(w[d+28u]!=0u){x=clamp(x,0,int(w[l+1u])-1);y=clamp(y,0,int(w[l+2u])-1);}
 else{x=wrap_(w[d+3u],x,int(w[l+1u]),border);y=wrap_(w[d+4u],y,int(w[l+2u]),border);}
 z=w[d+27u]==3u?wrap_(w[d+30u],z,int(w[l+3u]),border):0;
 vec4 c;
 if(border)c=load4(d+23u);
 else {
 // Resident-image substitution is inserted here, before the linear fetch.
 c=load4(texture_offset(w[l])+4u*((uint(z)*w[l+2u]+uint(y))*w[l+1u]+uint(x)));
 }
 if(w[d+29u]!=0u)c=vec4(cmp(0x200u+w[d+29u],ref,c.x)?1:0);
 return c;
}
vec4 filter_(uint unit,uint level,vec3 uv,bool linear_,uint face,float ref){
 uint d=2064u+32u*unit,l=texture_offset(w[d])+(face*w[d+1u]+level)*4u;
 precise vec3 xyz=uv*vec3(w[l+1u],w[l+2u],w[l+3u]);
 // Validate and convert only spatial lanes used by this texture dimension.
 // Cube directions have already become face-local UV coordinates.
 if(w[d+27u]==1u)xyz.y=.5;
 if(w[d+27u]!=3u)xyz.z=0;
 // Non-finite / huge texel coordinates: hardware result undefined; sample (0,0,0,0)
 // on both the GPU and the CPU reference (rsx_tex.py filter_level).
 if(any(isnan(xyz)) || any(isinf(xyz)) || any(greaterThan(abs(xyz),vec3(1048576))))return vec4(0);
 if(!linear_)return texel_(unit,level,int(floor(xyz.x)),int(floor(xyz.y)),int(floor(xyz.z)),face,ref);
 xyz-=.5;ivec3 p=ivec3(floor(xyz));precise vec3 a=xyz-vec3(p);
 precise vec4 planes[2];
 for(uint n=0u;n<2u;++n){int z=w[d+27u]==3u?p.z+int(n):0;
  precise vec4 c00=texel_(unit,level,p.x,p.y,z,face,ref),c10=texel_(unit,level,p.x+1,p.y,z,face,ref);
  precise vec4 c01=texel_(unit,level,p.x,p.y+1,z,face,ref),c11=texel_(unit,level,p.x+1,p.y+1,z,face,ref);
  planes[n]=(c00*(1.0-a.x)+c10*a.x)*(1.0-a.y)+(c01*(1.0-a.x)+c11*a.x)*a.y;
 }
 return w[d+27u]==3u?planes[0]*(1.0-a.z)+planes[1]*a.z:planes[0];
}
float gradient(uint unit,vec4 coord,vec4 delta){
 uint d=2064u+32u*unit,l=texture_offset(w[d]);
 if(w[d+28u]!=0u){vec3 q=cube_(coord.xyz);uint face=uint(q.z),axis=face/2u;
  float m=abs(coord[axis]),dm=coord[axis]>=0?delta[axis]:-delta[axis];
  float ds=face==0u?-delta.z:face==1u?delta.z:face==5u?-delta.x:delta.x;
  float dt=face==2u?delta.z:face==3u?-delta.z:-delta.y;
  vec2 g=vec2((ds-(2.0*q.x-1.0)*dm)*(.5*float(w[l+1u])/m),(dt-(2.0*q.y-1.0)*dm)*(.5*float(w[l+2u])/m));
  return length(g);
 }
 precise vec3 v=delta.xyz;if(w[d+2u]==0u)v*=vec3(w[l+1u],w[l+2u],w[l+3u]);
 if(w[d+27u]==1u)v.y=0;if(w[d+27u]!=3u)v.z=0;
 return sqrt(v.x*v.x+v.y*v.y+v.z*v.z);
}
vec4 textureSample(uint unit,vec4 coord,float lambda,inout bool killed){
 uint d=2064u+32u*unit,l=texture_offset(w[d]);vec3 uv=coord.xyz;uint face=0u;
 if(w[d+28u]!=0u){vec3 q=cube_(uv);uv.xy=q.xy;face=uint(q.z);}
 else if(w[d+2u]!=0u)uv/=vec3(w[l+1u],w[l+2u],w[l+3u]);
 float lod=min(uintBitsToFloat(w[d+9u]),max(uintBitsToFloat(w[d+8u]),lambda+uintBitsToFloat(w[d+7u])));
 uint mi=w[d+5u],ma=w[d+6u],last=w[d+1u]-1u;precise vec4 c;
 if(!(lod>0))c=filter_(unit,0u,uv,ma==2u,face,coord.z);
 else if(mi<=2u)c=filter_(unit,0u,uv,mi==2u,face,coord.z);
 else if(mi<=4u)c=filter_(unit,min(last,rnd(lod)),uv,mi==4u,face,coord.z);
 else{uint l0=min(last,uint(floor(lod))),l1=min(last,l0+1u);float f=lod-floor(lod);
  c=filter_(unit,l0,uv,mi==6u,face,coord.z)*(1.0-f)+filter_(unit,l1,uv,mi==6u,face,coord.z)*f;}
 vec4 input_=c.argb,result;
 for(uint i=0u;i<4u;++i){uint control=w[d+19u+i];result[i]=control==2u?input_[w[d+15u+i]]:control==1u?1.0:0.0;}
 result=result.gbar;
 if(w[d+14u]!=0u && result.a<0.000001){killed=true;return result;}
 for(uint i=0u;i<4u;++i){uint dest=(i+1u)%4u,src=w[d+15u+dest];bool input_c=w[d+19u+dest]==2u;
  bool snorm=input_c && (w[d+11u]&1u)!=0u && (w[d+12u]&(1u<<src))!=0u;
  if(!snorm && (w[d+11u]&2u)!=0u && (w[d+10u]&(1u<<i))!=0u)
   result[i]=result[i]<=0.04045?result[i]/12.92:pow((result[i]+0.055)/1.055,2.4);
  else if(!snorm && input_c && (w[d+11u]&4u)!=0u && w[d+13u]!=0u)result[i]=2.0*result[i]-1.0;
 }
 return result;
}
'''
