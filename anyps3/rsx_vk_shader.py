"""GLSL compute rasterizer, with binary32 order following rsx_draw.py.

Each invocation owns a complete 2x2 quad and visits primitives in FIFO order.
No inter-invocation atomics/order assumptions are needed for color or depth.
"""
COMMON = r'''
uint texture_offset(uint p){return p;}
uint R(uint a){return w[16u+a/4u];}
float F(uint a){return uintBitsToFloat(R(a));}
vec4 load4(uint a){return uintBitsToFloat(uvec4(w[a],w[a+1u],w[a+2u],w[a+3u]));}
vec4 cpu_min(vec4 a,vec4 b){return mix(a,b,lessThan(b,a));}
vec4 cpu_max(vec4 a,vec4 b){return mix(a,b,lessThan(a,b));}
vec4 cpu_clamp(vec4 x,vec4 lo,vec4 hi){return cpu_min(hi,cpu_max(lo,x));}
struct C {uvec4 r[48];vec4 input_[15];vec4 cc[2];bool killed;};
vec4 rd(C q,uint i,bool half_){
    if(!half_)return uintBitsToFloat(q.r[i]);
    uvec4 r=q.r[i/2u];uint base=(i&1u)*2u;
    return vec4(unpackHalf2x16(r[base]),unpackHalf2x16(r[base+1u]));
}
void wr(inout C q,uint i,bool half_,uint mask,vec4 v){
    if(!half_){for(uint c=0u;c<4u;++c)if((mask&(1u<<c))!=0u)q.r[i][c]=floatBitsToUint(v[c]);return;}
    for(uint c=0u;c<4u;++c)if((mask&(1u<<c))!=0u){
        uint at=(i&1u)*2u+c/2u, h=packHalf2x16(vec2(v[c],0))&65535u;
        uint old=q.r[i/2u][at];q.r[i/2u][at]=(c&1u)!=0u?(old&65535u)|(h<<16u):(old&0xffff0000u)|h;
    }
}
uint rnd(float x){return uint(x)+uint(fract(x)>=0.5);}
uint pack_u8(vec4 a){uint v=0u;for(uint c=0u;c<4u;++c)v|=rnd(clamp(a[c],0,1)*255.0)<<(8u*c);return v;}
uint pack_s8(vec4 a){uint v=0u;for(uint c=0u;c<4u;++c){float x=clamp(a[c],-1,1)*127.0;int n=int(rnd(abs(x)));if(x<0)n=-n;v|=(uint(n)&255u)<<(8u*c);}return v;}
uint pack_u16(vec2 a){return rnd(clamp(a.x,0,1)*65535.0)|(rnd(clamp(a.y,0,1)*65535.0)<<16u);}
bool cmp(uint f,float a,float b){
    switch(f){case 0x200u:return false;case 0x201u:return a<b;case 0x202u:return a==b;case 0x203u:return a<=b;
    case 0x204u:return a>b;case 0x205u:return a!=b;case 0x206u:return a>=b;default:return true;}
}
int wrap_(uint mode,int i,int n,inout bool border){
    if(mode==1u)return ((i%n)+n)%n;
    if(mode==2u){int k=((i%(2*n))+2*n)%(2*n);return k<n?k:2*n-1-k;}
    if(mode==4u){if(i<0 || i>=n){border=true;return 0;}return i;}
    if(mode==6u || mode==8u)i=i<0?-i-1:i;
    return clamp(i,0,n-1);
}
vec4 texel_(uint unit,uint level,int x,int y){
    uint d=2064u+32u*unit, l=texture_offset(w[d])+level*3u;bool border=false;
    x=wrap_(w[d+3u],x,int(w[l+1u]),border);y=wrap_(w[d+4u],y,int(w[l+2u]),border);
    if(border)return load4(d+23u);
    return load4(texture_offset(w[l])+4u*(uint(y)*w[l+1u]+uint(x)));
}
vec4 filter_(uint unit,uint level,vec2 uv,bool linear_){
    uint d=2064u+32u*unit, l=texture_offset(w[d])+level*3u;
    precise vec2 xy=uv*vec2(w[l+1u],w[l+2u]);
    // Avoid undefined float-to-int conversion and storage reads on invalid
    // coordinates. The caller will execute the CPU reference without writes.
    if(any(isnan(xy)) || any(isinf(xy)) || any(greaterThan(abs(xy),vec2(1048576))))return vec4(0); // see rsx_tex.py filter_level
    if(!linear_)return texel_(unit,level,int(floor(xy.x)),int(floor(xy.y)));
    xy-=0.5;ivec2 p=ivec2(floor(xy));precise vec2 a=xy-vec2(p);
    precise vec4 c00=texel_(unit,level,p.x,p.y),c10=texel_(unit,level,p.x+1,p.y);
    precise vec4 c01=texel_(unit,level,p.x,p.y+1),c11=texel_(unit,level,p.x+1,p.y+1);
    return (c00*(1.0-a.x)+c10*a.x)*(1.0-a.y)+(c01*(1.0-a.x)+c11*a.x)*a.y;
}
float gradient(uint unit,vec4 delta){
    uint d=2064u+32u*unit,l=texture_offset(w[d]);precise vec2 v=delta.xy;
    if(w[d+2u]==0u)v*=vec2(w[l+1u],w[l+2u]);
    return sqrt(v.x*v.x+v.y*v.y);
}
vec4 textureSample(uint unit,vec4 coord,float lambda,inout bool killed){
    uint d=2064u+32u*unit,l=texture_offset(w[d]);vec2 uv=coord.xy;
    if(w[d+2u]!=0u)uv/=vec2(w[l+1u],w[l+2u]);
    float lod=min(uintBitsToFloat(w[d+9u]),max(uintBitsToFloat(w[d+8u]),lambda+uintBitsToFloat(w[d+7u])));
    uint mi=w[d+5u],ma=w[d+6u],last=w[d+1u]-1u;precise vec4 c;
    if(!(lod>0))c=filter_(unit,0u,uv,ma==2u);
    else if(mi<=2u)c=filter_(unit,0u,uv,mi==2u);
    else if(mi<=4u)c=filter_(unit,min(last,rnd(lod)),uv,mi==4u);
    else {uint l0=min(last,uint(floor(lod))),l1=min(last,l0+1u);float f=lod-floor(lod);
        c=filter_(unit,l0,uv,mi==6u)*(1.0-f)+filter_(unit,l1,uv,mi==6u)*f;}
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
uint logic_(uint op,uint s,uint d){
 switch(op){case 0x1500u:return 0u;case 0x1501u:return s&d;case 0x1502u:return s&~d;case 0x1503u:return s;
 case 0x1504u:return ~s&d;case 0x1505u:return d;case 0x1506u:return s^d;case 0x1507u:return s|d;
 case 0x1508u:return ~(s|d);case 0x1509u:return ~(s^d);case 0x150au:return ~d;case 0x150bu:return s|~d;
 case 0x150cu:return ~s;case 0x150du:return ~s|d;case 0x150eu:return ~(s&d);default:return 0xffffffffu;}
}
uint stencil_(uint word,uint offset,uint op){
 uint old=word&255u,ref=R(0x334u+offset)&255u,mask=R(0x32cu+offset)&255u,value=old;
 switch(op){case 0u:value=0u;break;case 0x1e01u:value=ref;break;case 0x1e02u:value=min(255u,old+1u);break;
 case 0x1e03u:value=old!=0u?old-1u:0u;break;case 0x150au:value=old^255u;break;case 0x8507u:value=(old+1u)&255u;break;case 0x8508u:value=(old-1u)&255u;break;}
 return (word&~mask)|(value&mask);
}
float blendFactor(uint f,vec4 s,vec4 d,vec4 k,uint c){
    switch(f){case 0u:return 0;case 1u:return 1;case 0x300u:return s[c];case 0x301u:return 1.0-s[c];
    case 0x302u:return s.a;case 0x303u:return 1.0-s.a;case 0x304u:return d.a;case 0x305u:return 1.0-d.a;
    case 0x306u:return d[c];case 0x307u:return 1.0-d[c];case 0x308u:return c==3u?1.0:min(s.a,1.0-d.a);
    case 0x8001u:return k[c];case 0x8002u:return 1.0-k[c];case 0x8003u:return k.a;default:return 1.0-k.a;}
}
vec4 argb(uint v){return vec4((v>>16u)&255u,(v>>8u)&255u,v&255u,v>>24u)/255.0;}
uint packargb(vec4 c){uvec4 v=uvec4(rnd(c.r*255.0),rnd(c.g*255.0),rnd(c.b*255.0),rnd(c.a*255.0));return (v.a<<24u)|(v.r<<16u)|(v.g<<8u)|v.b;}

uint colorStride(uint f){return f==11u?2u:f==12u?4u:1u;}
uint colorBase(uint target){return target==0u?w[4]:w[11u+target];}
vec4 colorRead(uint base,uint f){
 uint v=w[base];
 if(f==11u)return vec4(unpackHalf2x16((v<<16u)|(v>>16u)),unpackHalf2x16((w[base+1u]<<16u)|(w[base+1u]>>16u)));
 if(f==12u)return load4(base);
 if(f==13u)return vec4(uintBitsToFloat(v));
 if(f==3u)return vec4(float((v>>11u)&31u)/31.0,float((v>>5u)&63u)/63.0,float(v&31u)/31.0,1);
 if(f<=2u)return vec4(vec3((v>>10u)&31u,(v>>5u)&31u,v&31u)/31.0,f==2u?1:0);
 if(f==9u)return vec4(vec3(float(v)/255.0),1);
 if(f==10u)return vec4(v&255u,v>>8u,v&255u,v>>8u)/255.0;
 vec4 c=argb(v);if(f>=14u)c=c.bgra;
 if(f==4u||f==14u)c.a=0;if(f==5u||f==15u)c.a=1;return c;
}
void colorWrite(uint base,uint f,vec4 c,uint mask,bool use_logic){
 uint masks[4]=uint[4](0x10000u,0x100u,1u,0x1000000u);
 if(f>=11u&&f<=13u){
  uint count=f==13u?1u:4u;
  for(uint i=0u;i<count;++i)if((mask&masks[i])!=0u){
   if(f==11u){uint at=base+i/2u,shift=(1u-i%2u)*16u,h=packHalf2x16(vec2(c[i],0))&65535u;
    w[at]=(w[at]&~(65535u<<shift))|(h<<shift);}
   else w[base+i]=floatBitsToUint(c[i]);
  }return;
 }
 uvec4 q=uvec4(rnd(clamp(c.r,0,1)*255.0),rnd(clamp(c.g,0,1)*255.0),rnd(clamp(c.b,0,1)*255.0),rnd(clamp(c.a,0,1)*255.0));
 uint value=0u,enabled=0u;
 if(f<=3u){uvec3 q5=uvec3(rnd(clamp(c.r,0,1)*31.0),rnd(clamp(c.g,0,1)*(f==3u?63.0:31.0)),rnd(clamp(c.b,0,1)*31.0));
  value=f==3u?(q5.r<<11u)|(q5.g<<5u)|q5.b:(f==2u?0x8000u:0u)|(q5.r<<10u)|(q5.g<<5u)|q5.b;
  if((mask&masks[0])!=0u)enabled|=f==3u?0xf800u:0x7c00u;
  if((mask&masks[1])!=0u)enabled|=f==3u?0x7e0u:0x3e0u;
  if((mask&masks[2])!=0u)enabled|=31u;if(f!=3u&&(mask&masks[3])!=0u)enabled|=0x8000u;
 }else if(f==9u){value=q.b;if((mask&masks[2])!=0u)enabled=255u;
 }else if(f==10u){value=(q.g<<8u)|q.b;if((mask&masks[1])!=0u)enabled|=0xff00u;if((mask&masks[2])!=0u)enabled|=255u;
 }else{
  if(f==4u||f==14u)q.a=0u;if(f==5u||f==15u)q.a=255u;
  for(uint i=0u;i<4u;++i){uint slot=i==3u?0u:i==1u?2u:i==0u?(f>=14u?3u:1u):(f>=14u?1u:3u),shift=(3u-slot)*8u;
   value|=q[i]<<shift;if((mask&masks[i])!=0u)enabled|=255u<<shift;}
 }
 uint old=w[base];if(use_logic)value=logic_(R(0x378u),value,old);w[base]=(old&~enabled)|(value&enabled);
}
float edge_(vec4 a,vec4 b,vec2 p,float sign_){return ((b.x-a.x)*(p.y-a.y)-(b.y-a.y)*(p.x-a.x))*sign_;}
bool tl(vec4 a,vec4 b,float s){float dx=(b.x-a.x)*s,dy=(b.y-a.y)*s;return (dy==0 && dx>0) || dy<0;}
bool hit(vec3 e,bvec3 top){return all(greaterThanEqual(e,vec3(0))) && !any(bvec3(e.x==0&&!top.x,e.y==0&&!top.y,e.z==0&&!top.z));}
'''

RASTER = r'''
void main(){
    uint width=w[0],height=w[1],qw=(width+1u)/2u,id=gl_GlobalInvocationID.x;
    if(id>=qw*((height+1u)/2u))return;
    uint qx=(id%qw)*2u,qy=(id/qw)*2u;
    uint aa=(R(0x208u)>>12u)&15u,nx=aa!=0u?2u:1u,ny=aa>=4u?2u:1u,samples=nx*ny,storage_w=width*nx;
    vec2 pos[4];pos[0]=vec2(0.5);pos[1]=pos[2]=pos[3]=vec2(0);
    if(aa==3u){pos[0]=vec2(.75);pos[1]=vec2(.25);}
    if(aa==4u){pos[0]=vec2(.25);pos[1]=vec2(.75,.25);pos[2]=vec2(.25,.75);pos[3]=vec2(.75);}
    if(aa==5u){pos[0]=vec2(.375,.125);pos[1]=vec2(.875,.375);pos[2]=vec2(.125,.625);pos[3]=vec2(.625,.875);}
    uint df=(R(0x208u)>>5u)&7u,depth_max=df==1u?65535u:16777215u;
    for(uint tri=0u;tri<w[2];++tri){
        uint at=w[3]+tri*204u;vec4 p[3];for(uint j=0u;j<3u;++j)p[j]=load4(at+j*68u);
        precise float area=(p[1].x-p[0].x)*(p[2].y-p[0].y)-(p[2].x-p[0].x)*(p[1].y-p[0].y);
        if(area==0)continue;
        float sg=area>0?1.0:-1.0;bvec3 top=bvec3(tl(p[1],p[2],sg),tl(p[2],p[0],sg),tl(p[0],p[1],sg));
        C q[4];uint coverage[4];precise vec3 bary[4];precise float lz[4];bool any_=false;
        for(uint l=0u;l<4u;++l){
            uint x=qx+(l&1u),y=qy+(l>>1u);vec2 center=vec2(x,y)+0.5;
            precise vec3 e=vec3(edge_(p[1],p[2],center,sg),edge_(p[2],p[0],center,sg),edge_(p[0],p[1],center,sg));
            precise float sum=e.x+e.y+e.z;bary[l]=e/sum;
            coverage[l]=0u;
            if(x>=w[8] && x<w[10] && y>=w[9] && y<w[11]){
                for(uint sample_=0u;sample_<samples;++sample_){vec2 location=vec2(x,y)+pos[sample_];
                    vec3 se=samples==1u?e:vec3(edge_(p[1],p[2],location,sg),edge_(p[2],p[0],location,sg),edge_(p[0],p[1],location,sg));
                    if(hit(se,top))coverage[l]|=1u<<sample_;
                }
            }
            any_=any_||coverage[l]!=0u;
            for(uint j=0u;j<48u;++j)q[l].r[j]=uvec4(0);
            for(uint j=0u;j<15u;++j)q[l].input_[j]=vec4(0);
            q[l].cc[0]=q[l].cc[1]=vec4(0);q[l].killed=false;
            precise vec3 b=bary[l];lz[l]=b.x*p[0].z+b.y*p[1].z+b.z*p[2].z;
            precise float iw=b.x*p[0].w+b.y*p[1].w+b.z*p[2].w;
            precise vec3 k=vec3(b.x*p[0].w/iw,b.y*p[1].w/iw,b.z*p[2].w/iw);
            q[l].input_[0]=vec4(center,lz[l],iw);
            for(uint input_=1u;input_<=13u;++input_){
                uint o=input_<=2u?input_:input_==3u?5u:input_<=11u?input_+3u:input_==12u?15u:6u;
                precise vec4 value;
                if(R(0x368u)==0x1d00u && o>=1u && o<=4u)value=load4(at+(R(0x1454u)!=0u?0u:2u)*68u+4u+o*4u);
                else value=k.x*load4(at+4u+o*4u)+k.y*load4(at+68u+4u+o*4u)+k.z*load4(at+136u+4u+o*4u);
                q[l].input_[input_]=input_==3u?vec4(value.x,0,0,0):value;
            }
            bool front=R(0x1834u)==0x901u?area<0:!(area<0);
            q[l].input_[14]=vec4(front?1:-1,0,0,0);
        }
        if(!any_)continue;
        shade(q);
        for(uint l=0u;l<4u;++l){
            if(coverage[l]==0u || q[l].killed)continue;
            uint x=qx+(l&1u),y=qy+(l>>1u);vec4 src=rd(q[l],0u,(R(0x1d60u)&0x40u)==0u);
            if((R(0x304u)&1u)!=0u && !cmp(R(0x308u),clamp(src.a,0,1),float(R(0x30cu)&255u)/255.0))continue;
            for(uint sample_=0u;sample_<samples;++sample_){if((coverage[l]&(1u<<sample_))==0u)continue;
                vec2 loc=vec2(x,y)+pos[sample_];
                precise float z=samples==1u?lz[l]:(edge_(p[1],p[2],loc,sg)*p[0].z+edge_(p[2],p[0],loc,sg)*p[1].z+edge_(p[0],p[1],loc,sg)*p[2].z)/abs(area);
                if(z<F(0x394u)||z>F(0x398u))continue;
                if((R(0x1d60u)&14u)!=0u)z=rd(q[l],1u,false).z;
                uint pixel=(y*ny+sample_/nx)*storage_w+x*nx+sample_%nx;
                uint old=w[w[5]+pixel],stored=df==1u?old:old>>8u;
                uint depth=rnd(clamp(z,0.0,1.0)*float(depth_max));
                if((R(0x380u)&1u)!=0u && (float(stored)/float(depth_max)<F(0x384u) || float(stored)/float(depth_max)>F(0x388u)))continue;
                bool stencil=(R(0x328u)&1u)!=0u && df==2u;
                bool front=R(0x1834u)==0x901u?area<0:!(area<0);
                uint offset=!front && R(0x348u)!=0u?32u:0u;
                uint ref=R(0x334u+offset)&255u,readmask=R(0x338u+offset)&255u;
                if(stencil && !cmp(R(0x330u+offset),float(ref&readmask),float(old&readmask))){w[w[5]+pixel]=stencil_(old,offset,R(0x33cu+offset));continue;}
                if((R(0xa74u)&1u)!=0u && !cmp(R(0xa6cu),float(depth),float(stored))){if(stencil)w[w[5]+pixel]=stencil_(old,offset,R(0x340u+offset));continue;}
                if(stencil)old=stencil_(old,offset,R(0x344u+offset));
                if((R(0xa70u)&1u)!=0u)old=df==1u?depth:(depth<<8u)|(old&255u);
                if(stencil || (R(0xa70u)&1u)!=0u)w[w[5]+pixel]=old;
                uint format_=R(0x208u)&31u;
                bool floating_=format_>=11u&&format_<=13u;
                for(uint target=0u;target<w[15];++target){
                uint output_=target==0u?0u:target+1u;
                src=rd(q[l],(R(0x1d60u)&0x40u)==0u?output_*2u:output_,(R(0x1d60u)&0x40u)==0u);
                if(!floating_)src=clamp(src,vec4(0),vec4(1));
                uint base=colorBase(target)+pixel*colorStride(format_);
                vec4 dst=colorRead(base,format_);precise vec4 result=src;
                bool blending_=target==0u?(R(0x310u)&1u)!=0u:(R(0x36cu)&(1u<<target))!=0u;
                if(blending_ && (R(0x374u)&1u)==0u){vec4 constant_=format_==11u?vec4(unpackHalf2x16(R(0x31cu)),unpackHalf2x16(R(0x37cu))):argb(R(0x31cu));
                    for(uint c=0u;c<4u;++c){uint sf=c==3u?R(0x314u)>>16u:R(0x314u)&65535u,df_=c==3u?R(0x318u)>>16u:R(0x318u)&65535u;
                        uint eq=c==3u?R(0x320u)>>16u:R(0x320u)&65535u;
                        precise float a=src[c]*blendFactor(sf,src,dst,constant_,c),b=dst[c]*blendFactor(df_,src,dst,constant_,c);
                        result[c]=eq==0x8006u?a+b:eq==0x800au?a-b:eq==0x800bu?b-a:eq==0x8007u?min(src[c],dst[c]):max(src[c],dst[c]);
                    }
                }
                uint mask=R(0x324u);
                if(target!=0u){uint n=(R(0x370u)>>(4u*target))&15u;mask=((n&1u)!=0u?0x1000000u:0u)|((n&2u)!=0u?0x10000u:0u)|((n&4u)!=0u?0x100u:0u)|((n&8u)!=0u?1u:0u);}
                colorWrite(base,format_,floating_?result:clamp(result,vec4(0),vec4(1)),mask,(R(0x374u)&1u)!=0u);
                }
                ++w[w[6]+y*width+x];
            }
        }
    }
}
'''
