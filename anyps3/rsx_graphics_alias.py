"""Ordered GPU rasterization over shared guest bytes for aliased attachments.

Independent physical-byte ownership components run in parallel. Each component
visits triangle, quad, lane, sample, MRT in CPU order.
Byte address tables retain pitch, tiling, swizzle and physical mapping aliases.
"""
from .rsx_vk_shader import RASTER

HELPERS = r'''
uint alias_bpp(uint target){uint f=R(0x208u)&31u;return target==4u?(((R(0x208u)>>5u)&7u)==1u?2u:4u):f==9u?1u:f<=3u||f==10u?2u:f==11u?8u:f==12u?16u:4u;}
uint alias_read(uint target,uint pixel,uint byte,uint n){
 uint table=w[w[4]+target]+pixel*alias_bpp(target),v=0u;
 for(uint i=0u;i<n;++i)v=(v<<8u)|(w[w[table+byte+i]]&255u);return v;
}
void alias_write(uint target,uint pixel,uint byte,uint n,uint v){
 uint table=w[w[4]+target]+pixel*alias_bpp(target);
 for(uint i=0u;i<n;++i)w[w[table+byte+i]]=256u|((v>>(8u*(n-1u-i)))&255u);
}
void alias_color(C q,uint target,uint pixel){
 uint f=R(0x208u)&31u,o=target==0u?0u:target+1u;bool half_=(R(0x1d60u)&64u)==0u;
 precise vec4 src=rd(q,half_?o*2u:o,half_);
 if(f!=11u && f!=12u){uint n=alias_bpp(target),old=alias_read(target,pixel,0u,n);
  alias_write(target,pixel,0u,n,packed_finish(src,old,target));return;}
 precise vec4 dst;
 for(uint c=0u;c<4u;++c){uint v=alias_read(target,pixel,c*(f==11u?2u:4u),f==11u?2u:4u);dst[c]=f==11u?unpackHalf2x16(v).x:uintBitsToFloat(v);}
 precise vec4 result=src;
 bool blend=target==0u?(R(0x310u)&1u)!=0u:(R(0x36cu)&(1u<<target))!=0u;
 if(blend){vec4 k=f==11u?vec4(unpackHalf2x16(R(0x31cu)),unpackHalf2x16(R(0x37cu))):argb(R(0x31cu));
  for(uint c=0u;c<4u;++c){uint sf=c==3u?R(0x314u)>>16u:R(0x314u)&65535u,df=c==3u?R(0x318u)>>16u:R(0x318u)&65535u,eq=c==3u?R(0x320u)>>16u:R(0x320u)&65535u;
   precise float a=src[c]*blendFactor(sf,src,dst,k,c),b=dst[c]*blendFactor(df,src,dst,k,c);
   result[c]=eq==0x8006u?a+b:eq==0x800au?a-b:eq==0x800bu?b-a:eq==0x8007u?cpu_min(src,dst)[c]:cpu_max(src,dst)[c];}}
 uint mask=R(0x324u);if(target!=0u){uint n=(R(0x370u)>>(target*4u))&15u;mask=((n&1u)!=0u?0x1000000u:0u)|((n&2u)!=0u?0x10000u:0u)|((n&4u)!=0u?0x100u:0u)|((n&8u)!=0u?1u:0u);}
 for(uint c=0u;c<4u;++c){uint m=c==3u?0x1000000u:c==0u?0x10000u:c==1u?0x100u:1u;
  if((mask&m)!=0u)alias_write(target,pixel,c*(f==11u?2u:4u),f==11u?2u:4u,f==11u?packHalf2x16(vec2(result[c],0))&65535u:floatBitsToUint(result[c]));}
}
'''


def replace(source, old, new):
    if source.count(old) != 1:
        raise ValueError('alias raster marker changed: ' + old)
    return source.replace(old, new, 1)


RASTER = replace(RASTER, '''uint width=w[0],height=w[1],qw=(width+1u)/2u,id=gl_GlobalInvocationID.x;
    if(id>=qw*((height+1u)/2u))return;
    uint qx=(id%qw)*2u,qy=(id/qw)*2u;''', '''uint width=w[0],height=w[1];
    if(gl_GlobalInvocationID.x!=0u)return;
    uint desc=w[4],x0=w[desc+5u],y0=w[desc+6u],x1=w[desc+7u],y1=w[desc+8u];''')
RASTER = replace(RASTER, '''        C q[4];uint coverage[4];''', '''        uint minx=max(x0,uint(max(0.0,floor(min(p[0].x,min(p[1].x,p[2].x)))))),miny=max(y0,uint(max(0.0,floor(min(p[0].y,min(p[1].y,p[2].y))))));
        uint maxx=min(x1,uint(max(0.0,ceil(max(p[0].x,max(p[1].x,p[2].x)))+1.0))),maxy=min(y1,uint(max(0.0,ceil(max(p[0].y,max(p[1].y,p[2].y)))+1.0)));
        float polygon_offset=0;
        if((R(0xa68u)&1u)!=0u){float dx=((p[1].z-p[0].z)*(p[2].y-p[0].y)-(p[2].z-p[0].z)*(p[1].y-p[0].y))/area;
         float dy=((p[1].x-p[0].x)*(p[2].z-p[0].z)-(p[2].x-p[0].x)*(p[1].z-p[0].z))/area;polygon_offset=max(abs(dx),abs(dy))*F(0xa78u)+F(0xa7cu)/float(depth_max);}
        for(uint qy=miny&~1u;qy<maxy;qy+=2u)for(uint qx=minx&~1u;qx<maxx;qx+=2u){
        C q[4];uint coverage[4];''')
RASTER = replace(RASTER, 'x>=w[8] && x<w[10] && y>=w[9] && y<w[11]', 'x>=minx && x<maxx && y>=miny && y<maxy')
RASTER = replace(RASTER, '            any_=any_||coverage[l]!=0u;', '''            bool inside=false;for(uint win=0u;win<8u;++win)if((w[8]&(1u<<win))!=0u){uint h=R(0x2c0u+8u*win),v=R(0x2c4u+8u*win);inside=inside || (x>=(h&65535u) && x<=(h>>16u) && y>=(v&65535u) && y<=(v>>16u));}
            if(w[8]!=0u && ((R(0x2bcu)&1u)!=0u?inside:!inside))coverage[l]=0u;
            if(R(0x147cu)!=0u && (R(0x1480u+4u*(y&31u))&(1u<<(x&31u)))==0u)coverage[l]=0u;
            any_=any_||coverage[l]!=0u;''')
RASTER = replace(RASTER, '            for(uint input_=1u;', '''            bool front=R(0x1834u)==0x901u?area<0:!(area<0);
            for(uint input_=1u;''')
RASTER = replace(RASTER, '                precise vec4 value;', '''                if((R(0x142cu)&1u)!=0u && !front && input_<=2u)o+=2u;
                precise vec4 value;''')
RASTER = replace(RASTER, '            bool front=R(0x1834u)==0x901u?area<0:!(area<0);\n            q[l].input_[14]', '''            for(uint t=0u;t<10u;++t)if((R(0xb40u+4u*t)&1u)!=0u){q[l].input_[4u+t].z=0;q[l].input_[4u+t].w=1.0/iw;}
            if(w[9]!=0u){float coord=q[l].input_[3].x,p0=F(0x8d0u),p1=F(0x8d4u),f=0;
             switch(R(0x8ccu)){case 0x2601u:f=p1*coord+p0-1.0;break;case 0x804u:f=p1*abs(coord)+p0-1.0;break;
              case 0x800u:f=exp(11.084*(p1*coord+p0-1.5));break;case 0x802u:f=exp(11.084*(p1*abs(coord)+p0-1.5));break;
              case 0x801u:case 0x803u:{float v=R(0x8ccu)==0x803u?abs(coord):coord,t=4.709*(p1*v+p0-1.5);f=exp(-t*t);break;}}
             q[l].input_[3]=vec4(coord,clamp(f,0,1),0,0);}
            q[l].input_[14]''')
RASTER = replace(RASTER, '/abs(area);\n                if(z<', '/abs(area))+polygon_offset;\n                if(z<')
# Open parenthesis enclosing interpolated depth for polygon bias.
RASTER = replace(RASTER, 'precise float z=samples==1u?', 'precise float z=(samples==1u?')
RASTER = replace(RASTER, 'uint old=w[w[5]+pixel],stored=', 'uint old=alias_read(4u,pixel,0u,df==1u?2u:4u),stored=')
RASTER = replace(RASTER, 'w[w[5]+pixel]=stencil_(old,offset,R(0x33cu+offset));', 'alias_write(4u,pixel,0u,4u,stencil_(old,offset,R(0x33cu+offset)));')
RASTER = replace(RASTER, 'w[w[5]+pixel]=stencil_(old,offset,R(0x340u+offset));', 'alias_write(4u,pixel,0u,4u,stencil_(old,offset,R(0x340u+offset)));')
RASTER = replace(RASTER, 'w[w[5]+pixel]=old;', 'alias_write(4u,pixel,0u,df==1u?2u:4u,old);')
begin = RASTER.index('                uint format_=')
end = RASTER.index('                ++w[w[6]+y*width+x];', begin)
RASTER = RASTER[:begin] + '                for(uint target=0u;target<w[15];++target)alias_color(q[l],target,pixel);\n' + RASTER[end:]
RASTER = replace(RASTER, '        uint minx=', '        uint meta=w[10]+tri*208u;vec4 segment=w[11]==2u?load4(meta):vec4(0);\n        uint minx=')
RASTER = replace(RASTER, '            precise float iw=b.x*p[0].w+b.y*p[1].w+b.z*p[2].w;', '''            precise float iw=b.x*p[0].w+b.y*p[1].w+b.z*p[2].w;
            if(w[11]==2u){float t=clamp(((center.x-segment.x)*segment.z+(center.y-segment.y)*segment.w)/(segment.z*segment.z+segment.w*segment.w),0,1);
             vec4 a=load4(meta+8u),c=load4(meta+76u);iw=(1.0-t)*a.w+t*c.w;lz[l]=(1.0-t)*a.z+t*c.z;b=vec3(1.0-t,t,0);}''')
RASTER = replace(RASTER, '            q[l].input_[0]=', '            if(w[11]==2u){vec4 a=load4(meta+8u),c=load4(meta+76u);k=vec3(b.x*a.w/iw,b.y*c.w/iw,0); }\n            q[l].input_[0]=')
RASTER = replace(RASTER, '                q[l].input_[input_]=', '''                if(w[11]==2u){if(R(0x368u)==0x1d00u && o>=1u && o<=4u)value=load4(meta+144u+o*4u);
                 else value=k.x*load4(meta+12u+o*4u)+k.y*load4(meta+80u+o*4u);}
                q[l].input_[input_]=''')
RASTER = replace(RASTER, '            any_=any_||coverage[l]!=0u;', '''            #ifdef ALIAS_LINES
            if(w[11]==2u && coverage[l]!=0u){bool hit_=false;bool xmajor=abs(segment.z)>=abs(segment.w);uint line_width=w[meta+4u];
             for(uint row=0u;row<line_width && !hit_;++row){double delta=double(row)-double(line_width-1u)*.5;
              double lx=double(segment.x)-double(center.x)-(xmajor?0.0:delta)-1.e-7,ly=double(segment.y)-double(center.y)-(xmajor?delta:0.0)-1.e-14;
              double enter=0,leave=1;for(int n=0;n<4;++n){int xs=(n&1)!=0?1:-1,ys=(n&2)!=0?1:-1;
               double q=.5-double(xs)*lx-double(ys)*ly,d=double(xs)*double(segment.z)+double(ys)*double(segment.w);
               if(d>0)leave=min(leave,q/d);else if(d<0)enter=max(enter,q/d);else if(q<0)leave=-1;}
              hit_=leave>enter && leave<1 && leave>0;}if(!hit_)coverage[l]=0u;}
            #endif
            any_=any_||coverage[l]!=0u;''')
RASTER = RASTER.replace('R(0x1834u)==0x901u?area<0:!(area<0)', 'w[11]!=0u || (R(0x1834u)==0x901u?area<0:!(area<0))').replace('if(R(0x147cu)!=0u', 'if(w[11]==0u && R(0x147cu)!=0u').replace('if((R(0xa68u)&1u)!=0u)', 'if(w[11]==0u && (R(0xa68u)&1u)!=0u)')
RASTER = replace(RASTER, '    if(gl_GlobalInvocationID.x!=0u)return;', '''    #ifdef ALIAS_PARALLEL
    uint id=gl_GlobalInvocationID.x,qw=(width+1u)/2u;if(id>=qw*((height+1u)/2u))return;
    uint own_x=(id%qw)*2u,own_y=(id/qw)*2u;
    #else
    uint component=gl_GlobalInvocationID.x,qw=(width+1u)/2u,component_desc=w[4];
    if(component>=w[component_desc+11u])return;
    uint component_begin=w[w[component_desc+9u]+component],component_end=w[w[component_desc+9u]+component+1u];
    #endif''')
RASTER = replace(RASTER, '        for(uint qy=miny&~1u;qy<maxy;qy+=2u)for(uint qx=minx&~1u;qx<maxx;qx+=2u){', '''        uint first_x=minx&~1u,first_y=miny&~1u,last_x=maxx,last_y=maxy;
        #ifdef ALIAS_PARALLEL
        if(own_x+2u<=minx || own_x>=maxx || own_y+2u<=miny || own_y>=maxy)continue;
        first_x=own_x;first_y=own_y;last_x=own_x+2u;last_y=own_y+2u;
        #endif
        #ifdef ALIAS_PARALLEL
        for(uint qy=first_y;qy<last_y;qy+=2u)for(uint qx=first_x;qx<last_x;qx+=2u){
        #else
        for(uint member=component_begin;member<component_end;++member){
        uint quad=w[w[desc+10u]+member],qx=(quad%qw)*2u,qy=(quad/qw)*2u;
        if(qx+2u<=minx || qx>=maxx || qy+2u<=miny || qy>=maxy)continue;
        #endif''')
RASTER = RASTER.rsplit('    }\n}', 1)[0] + '    }}\n}\n'

CPP = r'''
// The CPU model observes shared bytes after every sample. Distinct Vulkan
// attachments cannot express that feedback; preserve order within every
// connected component of helper quads sharing any physical byte.
static bool draw_alias(Session& session,std::vector<unsigned> data,const std::string& shade,
        const std::array<Target,4>& colors,const Target& depth,unsigned width,unsigned height,unsigned samples,unsigned count,unsigned vertices,
        unsigned x0,unsigned y0,unsigned x1,unsigned y1,bool& parallel){
    auto& vk=session.vk;auto& state=session.state;
    struct Span{std::uintptr_t first,last;unsigned at=0;};std::vector<Span> spans;
    auto add=[&](const Target& t){if(t.mapping)for(const auto& p:t.mapping->parts)spans.push_back({reinterpret_cast<std::uintptr_t>(p.data),reinterpret_cast<std::uintptr_t>(p.data)+p.size,0});
        else if(t.base)spans.push_back({reinterpret_cast<std::uintptr_t>(t.base),reinterpret_cast<std::uintptr_t>(t.base)+target_span(t,width*(samples>1?2:1),height*(samples==4?2:1)),0});};
    for(unsigned i=0;i<count;++i)add(colors[i]);
    add(depth);
    std::sort(spans.begin(),spans.end(),[](const Span& a,const Span& b){return a.first<b.first;});
    std::vector<Span> merged;for(const auto& p:spans){if(!merged.empty() && merged.back().last>=p.first)merged.back().last=std::max(merged.back().last,p.last);else merged.push_back(p);}
    const unsigned sw=width*(samples>1?2:1),sh=height*(samples==4?2:1);const auto pixels=std::uint64_t(sw)*sh;
    const unsigned quad_count=((width+1)/2)*((height+1)/2);
    std::uint64_t words=data.size()+12+std::uint64_t(width)*height+std::uint64_t(quad_count)*2+1;
    for(unsigned i=0;i<count;++i)words+=pixels*colors[i].bpp;
    words+=pixels*depth.bpp;for(const auto& p:merged)words+=p.last-p.first;
    if(words>UINT_MAX || !vk.compute_fits(words*4,1))return false;
    session.flush(true,nullptr,0,"alias_compute");session.clear_surfaces();
    GuestMemory::ObserverPause pause(state.memory);
    data[2]=vertices/3;data[15]=count;const unsigned desc=data.size();data[4]=desc;data.resize(desc+12,0);
    data[desc+5]=x0;data[desc+6]=y0;data[desc+7]=x1;data[desc+8]=y1;
    for(auto& p:merged){p.at=data.size();std::vector<std::uint8_t> bytes(p.last-p.first);
        pinned_host_read(state.memory,reinterpret_cast<const void*>(p.first),bytes.data(),bytes.size());
        for(auto byte:bytes)data.push_back(byte);}
    auto index=[&](const std::uint8_t* p){const auto at=reinterpret_cast<std::uintptr_t>(p);
        auto it=std::upper_bound(merged.begin(),merged.end(),at,[](std::uintptr_t value,const Span& span){return value<span.first;});
        if(it==merged.begin() || at>=(--it)->last)throw std::runtime_error("RSX alias byte outside certified backing");
        return it->at+unsigned(at-it->first);};
    // Prove ownership of every physical byte, including subword formats,
    // padding, wide swizzle words and sample planes. Independent helper quads
    // can run in parallel only when no two of them touch the same byte.
    std::vector<unsigned> owners;std::vector<unsigned> owner_base;unsigned owner_words=0;
    for(const auto& p:merged){owner_base.push_back(owner_words);owner_words+=p.last-p.first;}owners.resize(owner_words,UINT_MAX);parallel=true;
    std::vector<unsigned> parent(quad_count);for(unsigned i=0;i<quad_count;++i)parent[i]=i;
    auto root=[&](unsigned n){while(parent[n]!=n){parent[n]=parent[parent[n]];n=parent[n];}return n;};
    auto table=[&](const Target& t,unsigned slot){data[desc+slot]=data.size();
        for(unsigned y=0;y<sh;++y)for(unsigned x=0;x<sw;++x)for(unsigned byte=0;byte<t.bpp;++byte){const auto at=t.address(x,y,byte);const auto* p=t.mapping?range_identity(*t.mapping,at,1):t.base+at;
            if(!p)throw std::runtime_error("RSX alias byte unmapped");
            const auto mapped=index(p);data.push_back(mapped);
            const unsigned quad=(y/(samples==4?2:1)/2)*((width+1)/2)+(x/(samples>1?2:1)/2);
            auto span=std::upper_bound(merged.begin(),merged.end(),reinterpret_cast<std::uintptr_t>(p),[](std::uintptr_t value,const Span& entry){return value<entry.first;});--span;
            auto& owner=owners[owner_base[span-merged.begin()]+reinterpret_cast<std::uintptr_t>(p)-span->first];
            if(owner!=UINT_MAX && owner!=quad){parallel=false;const auto a=root(owner),b=root(quad);parent[std::max(a,b)]=std::min(a,b);}
            owner=quad;}};
    for(unsigned i=0;i<count;++i)table(colors[i],i);
    table(depth,4);
    unsigned component_count=quad_count;
    if(!parallel){
        std::map<unsigned,std::vector<unsigned>> components;
        for(unsigned q=0;q<quad_count;++q)components[root(q)].push_back(q);
        component_count=components.size();data[desc+9]=data.size();unsigned prefix=0,largest=0;
        for(const auto& c:components){data.push_back(prefix);prefix+=c.second.size();largest=std::max<unsigned>(largest,c.second.size());}
        data.push_back(prefix);data[desc+10]=data.size();data[desc+11]=component_count;
        for(const auto& c:components)data.insert(data.end(),c.second.begin(),c.second.end());
        if(fallback_stats().enabled)fallback_stats().detail("dependent_alias_components","components="+std::to_string(component_count)+" largest_quads="+std::to_string(largest)+" total_quads="+std::to_string(quad_count));
    }
    data[6]=data.size();data.resize(data.size()+std::uint64_t(width)*height,0);
    std::string common=rsxvk::common_glsl;
    common.replace(common.find("vec4 texel_("),common.find("uint logic_(")-common.find("vec4 texel_("),texture_glsl);
    std::string emitted=shade;
    for(unsigned unit=0;unit<16;++unit){const auto u=rsxvk::u(unit);
        replace_all(emitted,"gradient("+u+",b)","gradient("+u+",a,b)");replace_all(emitted,"gradient("+u+",c)","gradient("+u+",a,c)");
        replace_all(emitted,"gradient("+u+",aa[1]-aa[0])","gradient("+u+",aa[0],aa[1]-aa[0])");replace_all(emitted,"gradient("+u+",aa[2]-aa[0])","gradient("+u+",aa[0],aa[2]-aa[0])");}
    const unsigned groups=(component_count+63)/64;
    if(!vk.compute_fits(data.size()*4,groups))return false;
    std::string source=std::string("#version 450\n")+(data[11]==2u?"#extension GL_ARB_gpu_shader_fp64 : require\n#define ALIAS_LINES\n":"")+(parallel?"#define ALIAS_PARALLEL\n":"")+"layout(local_size_x=64) in;"+"layout(std430,binding=0) buffer Data {uint w[];};\n"+common+fixed_glsl+alias_helpers+emitted+alias_raster;
    vk.compute(source,data,groups);
    if(data[7]&4)throw std::runtime_error("RSX derivatives/implicit texture LOD in divergent flow are unsupported; no guest commit");
    if(data[7])throw std::runtime_error("RSX alias shader error; no guest commit");
    // Only bytes written by the shader are committed. Padding and untouched
    // portions of tiled/split backing are never copied back from a snapshot.
    for(const auto& p:merged){std::uintptr_t at=p.first;while(at<p.last){if(!(data[p.at+at-p.first]&256)){++at;continue;}
        const auto begin=at;while(at<p.last && (data[p.at+at-p.first]&256))++at;
        // ObserverPause protects the byte commit from recursive surface sync;
        // invalidate raw uploads explicitly before publishing these writes.
        session.invalidate_raw(reinterpret_cast<const void*>(begin),at-begin,"alias_commit");
        std::vector<std::uint8_t> bytes(at-begin);for(auto q=begin;q<at;++q)bytes[q-begin]=data[p.at+q-p.first]&255;
        pinned_host_write(state.memory,reinterpret_cast<const void*>(begin),bytes.data(),bytes.size());}}
    std::uint64_t shaded=0;for(unsigned i=0;i<width*height;++i)shaded+=data[data[6]+i];
    state.rsx.draw.pixels+=shaded;if(Reg{state.rsx}(0x17cc))state.rsx.report_values[0]+=shaded;
    state.rsx.draw.triangles+=vertices/3;return true;
}
'''
