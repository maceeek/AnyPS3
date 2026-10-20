"""Synchronous RSX transfers; guest storage is authoritative.

Layouts: RPCS3 ffba2bf0 gcm_enums.h, rsx_decode.h, NV47/HW/nv0039.cpp,
nv308a.cpp, nv3089.cpp; confirmed common fields in envytools f102b823
rnndb/graph/nv_m2mf.xml, nv1_ifm.xml, nv1_ifc.xml. Unsupported operations,
notifications and formats stop before publishing any destination bytes.
NV0039 reads physical bytes; image transfers address logical rows before tile
permutation. MSAA is explicit expanded input geometry (no stale draw-state guess).
"""

CPP = r'''
namespace rsxblit {
static std::uint32_t state(const NativeRsx& gpu,unsigned reg){
    // Unwritten registers hold their reset value 0 (RPCS3 rsx_methods.cpp:137).
    const auto it=gpu.registers.find(reg);
    return it==gpu.registers.end()?0u:it->second;
}
static unsigned get(const NativeRsx& gpu,unsigned reg,unsigned fallback){
    const auto it=gpu.registers.find(reg);return it==gpu.registers.end()?fallback:it->second;
}
static unsigned location(unsigned context){
    if(context==0xfeed0000)return 0;
    if(context==0xfeed0001)return 1;
    throw std::runtime_error("unsupported RSX transfer DMA context "+std::to_string(context));
}
static std::uint64_t address(const NativeRsx& gpu,unsigned context,std::int64_t offset,std::uint64_t size){
    if(offset<0 || size>UINT32_MAX || std::uint64_t(offset)+size>UINT64_C(0x100000000))
        throw std::runtime_error("RSX transfer offset overflow");
    if(location(context)==0){
        if(std::uint64_t(offset)>gpu.local_size || size>gpu.local_size-std::uint64_t(offset))
            throw std::runtime_error("RSX transfer outside VRAM");
        return UINT64_C(0xc0000000)+offset;
    }
    if(std::uint64_t(offset)+size>0x10000000)throw std::runtime_error("RSX transfer outside IO aperture");
    const auto first=rsx_address(gpu,offset);
    // Checking only the endpoints can miss a discontiguous middle IO page.
    for(std::uint64_t at=(std::uint64_t(offset)|0xfffff)+1;at<std::uint64_t(offset)+size;at+=0x100000)
        if(rsx_address(gpu,at)!=first+at-offset)throw std::runtime_error("RSX transfer spans discontiguous IO pages");
    if(size && rsx_address(gpu,offset+size-1)!=first+size-1)
        throw std::runtime_error("RSX transfer spans discontiguous IO pages");
    return first;
}
static GuestMemory::Range access(PpuState& s,unsigned context,std::int64_t offset,unsigned size,bool write){
    const auto addr=address(s.rsx,context,offset,size);
    (void)s.memory.range(addr,size,false);
    return s.memory.range(addr,size,write);
}
static void copy(PpuState& s,unsigned notify){
    auto& gpu=s.rsx;
    if(notify)throw std::runtime_error("unsupported NV0039 notification request");
    const auto cols=state(gpu,0x231c),rows=state(gpu,0x2320),format=state(gpu,0x2324);
    const auto in_step=format&255,out_step=(format>>8)&255;
    if(format>>16 || (in_step<1 || in_step>4) || (out_step<1 || out_step>4))
        throw std::runtime_error("unsupported NV0039 byte strides");
    if(!cols || !rows)return;
    const auto m2mf_start=std::chrono::steady_clock::now();
    struct M2mfAccount {std::chrono::steady_clock::time_point start;std::uint64_t bytes;~M2mfAccount(){
        if(std::uncaught_exceptions())return;
        ::RsxPerfCounters::add(::RsxPerfCounters::m2mf_calls);::RsxPerfCounters::add(::RsxPerfCounters::m2mf_bytes,bytes);
        ::RsxPerfCounters::add(::RsxPerfCounters::m2mf_ns,std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count());}
    } m2mf_account{m2mf_start,std::uint64_t(cols)*rows};
    if(std::uint64_t(cols)*rows>0x10000000)throw std::runtime_error("NV0039 transfer exceeds native snapshot budget");
    const std::uint64_t in_span=std::uint64_t(cols-1)*in_step+1,out_span=std::uint64_t(cols-1)*out_step+1;
    if(in_span>UINT32_MAX || out_span>UINT32_MAX)throw std::runtime_error("NV0039 row span overflow");
    const auto in_context=state(gpu,0x2184),out_context=state(gpu,0x2188);
    const auto in_offset=state(gpu,0x230c),out_offset=state(gpu,0x2310);
    const auto in_pitch=std::int32_t(state(gpu,0x2314)),out_pitch=std::int32_t(state(gpu,0x2318));
#ifdef ANYPS3_RSX_VK
    if(gpu.graphics_session && in_step==1 && out_step==1 && in_pitch>0 && out_pitch>0){
        GuestMemory::ObserverPause acquire(s.memory);
        const auto from=access(s,in_context,in_offset,(rows-1)*std::uint64_t(in_pitch)+cols,false);
        const auto to=access(s,out_context,out_offset,(rows-1)*std::uint64_t(out_pitch)+cols,true);
        if(static_cast<rsxgraphics::Session*>(gpu.graphics_session)->copy(from,to,cols,rows,in_pitch,out_pitch))return;
    }
#endif
    std::vector<GuestMemory::Range> dst;std::vector<std::uint8_t> snapshot(std::size_t(cols)*rows);
    dst.reserve(rows);
    // Snapshot all input before any output, including host-aliased IO mappings
    // and negative row pitches. Validate every destination before committing.
    for(unsigned y=0;y<rows;++y){
        const auto src=access(s,in_context,std::int64_t(in_offset)+std::int64_t(y)*in_pitch,in_span,false);
        dst.push_back(access(s,out_context,std::int64_t(out_offset)+std::int64_t(y)*out_pitch,out_span,true));
        for(unsigned x=0;x<cols;++x)src.read(x*in_step,snapshot.data()+std::size_t(y)*cols+x,1);
    }
    for(unsigned y=0;y<rows;++y){
        for(unsigned x=0;x<cols;++x)dst[y].write(x*out_step,snapshot.data()+std::size_t(y)*cols+x,1);
    }
}
struct Image {
    unsigned context=0,offset=0,pitch=0,bpp=0,width=0,height=0,log_w=0,log_h=0;
    bool swizzled=false;
    // Same-32-byte-group memo of the tile permutation (batch path only): every byte of an aligned 32-byte group shares
    // all address bits above bit 4 (rsx_tiled_offset keeps byte_address&31 and derives the rest from linear>>5).
    struct Memo {const NativeRsx::Tile* tile=nullptr;std::uint64_t group=UINT64_MAX,base=0;};
    GuestMemory::Range pixel(PpuState& s,unsigned x,unsigned y,bool write)const{return access(s,context,locate(s.rsx,x,y,nullptr),bpp,write);}
    // Physical byte offset inside the DMA context of pixel (x,y) after swizzle and tile permutation; the single place
    // that validates blit addressing (the per-pixel reference and the batch path both call it).
    // Logical (pre-tile) byte address of pixel (x,y) inside the DMA context.
    std::uint64_t logical(unsigned x,unsigned y)const{
        if(x>=width || y>=height)throw std::runtime_error("RSX blit coordinate outside image");
        std::uint64_t at=0;
        if(swizzled){
            unsigned bit=0;
            for(unsigned i=0;i<std::max(log_w,log_h);++i){
                if(i<log_w)at|=std::uint64_t((x>>i)&1)<<bit++;
                if(i<log_h)at|=std::uint64_t((y>>i)&1)<<bit++;
            }
            at=offset+at*bpp;
        }else at=std::uint64_t(offset)+std::uint64_t(y)*pitch+std::uint64_t(x)*bpp;
        if(at>UINT32_MAX)throw std::runtime_error("RSX blit image offset overflow");
        return at;
    }
    // How many pixels of row y starting at x (at most `max`, at least 1) are guaranteed to be physically consecutive bytes whose
    // addressing locate() validates identically for each of them: the rest of the row of a linear image no bound tile of this
    // location touches, the pixels of one aligned 32-byte logical group inside a single bound tile (RSX tiling keeps each 32-byte
    // group), 1 for a swizzled image or whenever tiles overlap or start inside the run. The first and last pixel of a run are still
    // locate()d by the caller (which raises every configuration/bounds error), so a run never hides a fault.
    unsigned run(const NativeRsx& gpu,unsigned x,unsigned y,unsigned max)const{
        if(swizzled || max<2)return 1;
        const auto at=logical(x,y),end=at+std::uint64_t(max)*bpp;const auto place=location(context);
        const NativeRsx::Tile* first=nullptr;unsigned hits=0;std::uint64_t next_start=UINT64_MAX;
        for(const auto& tile:gpu.tiles){
            if(!tile.bound || tile.location!=place)continue;
            const std::uint64_t begin=tile.offset,limit=begin+tile.size;
            if(at<limit && at+bpp>begin){++hits;first=&tile;}
            else if(begin>=at+bpp && begin<end)next_start=std::min(next_start,begin);
        }
        if(hits>1)return 1;
        std::uint64_t pixels=max;
        if(hits==1){
            pixels=std::min<std::uint64_t>(pixels,(32-(at&31))/bpp);
            pixels=std::min<std::uint64_t>(pixels,(std::uint64_t(first->offset)+first->size-at)/bpp);
        }else if(next_start!=UINT64_MAX)pixels=std::min<std::uint64_t>(pixels,(next_start-at)/bpp);
        return unsigned(std::max<std::uint64_t>(pixels,1));
    }
    std::uint64_t locate(const NativeRsx& gpu,unsigned x,unsigned y,Memo* memo)const{
        const auto at=logical(x,y);
        // Byte lanes inside a word are preserved by rsx_tiled_offset. Tile
        // pitch describes the memory permutation, not necessarily this subimage's
        // row stride. Apply it also to main-memory tiles before IO translation.
        const NativeRsx::Tile* selected=nullptr;
        for(const auto& tile:gpu.tiles)if(tile.bound && tile.location==location(context) &&
            at<std::uint64_t(tile.offset)+tile.size && at+bpp>tile.offset){
            if(selected)throw std::runtime_error("overlapping RSX blit tiles");
            selected=&tile;
        }
        if(selected){
            const auto& tile=*selected;
            if(at<tile.offset || at+bpp>std::uint64_t(tile.offset)+tile.size)
                throw std::runtime_error("RSX blit pixel crosses tile boundary");
            // The tile permutation is a function of the logical byte address
            // only (RPCS3 ffba2bf0 Common/tiled_dma_copy.hpp:36-118), so a
            // swizzled image is permuted at its Morton address (nv3089.cpp:
            // 634-685 tiles swizzle2d output the same way). Compression modes
            // the runtime stores expanded (rsx_surface.py clear, rsx_display.py
            // scanout: color 7, depth 9-12) read and write as uncompressed;
            // other tag layouts are unknown here.
            if(tile.compression!=0 && tile.compression!=7 && (tile.compression<9 || tile.compression>12))
                throw std::runtime_error("unsupported RSX blit tile compression mode "+std::to_string(tile.compression));
            const std::uint64_t linear=(at-tile.offset)&~3u;
            std::uint64_t word;   // rsx_tiled_offset of the 4-byte aligned logical address
            if(memo && memo->tile==&tile && memo->group==(linear>>5)){
                word=memo->base+(linear&31);
                if(word<tile.offset || word+4>std::uint64_t(tile.offset)+tile.size)
                    throw std::runtime_error("RSX tiled sample outside configured tile");
            }else{
                word=rsx_tiled_offset(tile,std::uint32_t(linear));
                if(memo){memo->tile=&tile;memo->group=linear>>5;memo->base=word-(linear&31);}
            }
            const auto physical=word+(at&3);
            if(physical+bpp>std::uint64_t(tile.offset)+tile.size)
                throw std::runtime_error("RSX blit tiled pixel outside tile");
            // Unaligned pixels must not span a permuted 32-byte block.
            if((at&31)+bpp>32)throw std::runtime_error("RSX blit pixel crosses tiled block");
            return physical;
        }
        return at;
    }
};
static unsigned dest_bpp(unsigned format){
    if(format==4)return 2;
    if(format==10 || format==11)return 4;
    throw std::runtime_error("unsupported RSX blit destination format "+std::to_string(format));
}
static Image destination(const NativeRsx& gpu,unsigned surface,unsigned width,unsigned height,unsigned& format){
    Image image;
    if(surface==0x313371c3){
        format=state(gpu,0x6300);image.context=state(gpu,0x6188);image.offset=state(gpu,0x630c);
        image.bpp=dest_bpp(format);image.pitch=state(gpu,0x6304)>>16;
        if(!image.pitch)image.pitch=width*image.bpp;
        if(std::uint64_t(width)*image.bpp>image.pitch)throw std::runtime_error("RSX blit output pitch too small");
        image.width=width;image.height=height;
    }else if(surface==0x31337a73){
        const auto layout=state(gpu,0x8300);format=layout&0xffff;
        image.log_w=(layout>>16)&255;image.log_h=layout>>24;
        if(image.log_w>12 || image.log_h>12)throw std::runtime_error("unsupported RSX blit swizzle dimensions");
        image.width=1u<<image.log_w;image.height=1u<<image.log_h;image.swizzled=true;
        image.context=state(gpu,0x8184);image.offset=state(gpu,0x8304);image.bpp=dest_bpp(format);
        if(width>image.width || height>image.height)throw std::runtime_error("RSX blit exceeds swizzled image");
    }else throw std::runtime_error("unsupported RSX blit surface object");
    return image;
}
using Color=std::array<unsigned,4>; // A R G B, each in [0,255].
static unsigned source_bpp(unsigned format){
    switch(format){
    case 1:case 2:case 7:case 9:return 2;
    case 3:case 4:case 12:case 13:return 4;
    case 8:return 1;
    default:throw std::runtime_error("unsupported NV3089 source color format "+std::to_string(format));
    }
}
// NV308A uses IFC formats, not NV3089 SIFM formats. In particular 4/5
// are ARGB8/XRGB8; 3 is packed X1R5G5B5 (envytools nv1_ifc.xml:21-29,
// RPCS3 rsx_methods.cpp:553 sets the PS3 IFC default to 4).
static unsigned cpu_source_format(unsigned format){
    if(format==4)return 3;
    if(format==5)return 4;
    if(format>=1 && format<=3)
        throw std::runtime_error("NV308A packed input requires verified FIFO streaming semantics");
    throw std::runtime_error("unsupported NV308A source color format "+std::to_string(format));
}
// SRCCOPY_AND is source copy with zero-alpha discard. Other operations
// require bound ROP/pattern/beta object state which NativeRsx does not retain.
static void validate_operation(const NativeRsx& gpu,unsigned operation,unsigned pattern){
    if(operation!=0 && operation!=3)
        throw std::runtime_error("RSX blit ROP/blend/premult requires verified context object state");
    if(operation==0 && state(gpu,pattern))
        throw std::runtime_error("RSX blit SRCCOPY_AND pattern context is unsupported");
}
static void validate_conversion(unsigned conversion,unsigned dst_format,unsigned src_format,bool filtered=false){
    if(conversion>2)throw std::runtime_error("unsupported RSX blit color conversion");
    // Dither/subtract-truncate tables are not present in the PS3 reference.
    // Without channel reduction these modes have no quantization to perform.
    // RGB565 -> RGB565 already has exactly the destination precision.
    if(dst_format==4 && conversion!=1 && (src_format!=7 || filtered))
        throw std::runtime_error("RSX blit reduced-precision dither/subtract table is undocumented");
}
static Color read(const std::uint8_t* p,unsigned format){
    if(format==3 || format==4 || format==12 || format==13){
        Color c{p[0],p[1],p[2],p[3]};
        if(format==4 || format==13)c[0]=255;
        if(format>=12)std::swap(c[1],c[3]);
        return c;
    }
    if(format==8)return {255,p[0],p[0],p[0]};
    if(format==9)return {p[0],p[1],p[1],p[1]};
    const unsigned raw=(unsigned(p[0])<<8)|p[1];
    if(format==7){const unsigned r=raw>>11,g=(raw>>5)&63,b=raw&31;return {255,(r<<3)|(r>>2),(g<<2)|(g>>4),(b<<3)|(b>>2)};}
    if(format==1 || format==2){const unsigned r=(raw>>10)&31,g=(raw>>5)&31,b=raw&31;return {format==2 || raw&0x8000?255u:0u,(r<<3)|(r>>2),(g<<3)|(g>>2),(b<<3)|(b>>2)};}
    throw std::runtime_error("unsupported RSX blit source format");
}
static Color read(const GuestMemory::Range& source,unsigned format){
    std::array<std::uint8_t,4> bytes{};source.read(0,bytes.data(),source.size);
    return read(bytes.data(),format);
}
static std::array<std::uint8_t,4> packed(unsigned format,const Color& c){
    std::array<std::uint8_t,4> bytes{};
    if(format==4){const unsigned v=((c[1]>>3)<<11)|((c[2]>>2)<<5)|(c[3]>>3);bytes[0]=v>>8;bytes[1]=v;}
    else for(unsigned i=0;i<4;++i)bytes[i]=c[i];
    return bytes;
}
static void write(PpuState&,const GuestMemory::Range& destination,unsigned format,const Color& c){
    const auto bytes=packed(format,c);destination.write(0,bytes.data(),dest_bpp(format));
}
static std::int64_t floor_fixed(std::int64_t value){return value>=0?value/1048576:-((-value+1048575)/1048576);}
// The per-pixel path in scaled() is the reference implementation (one Range per pixel, bytes snapshotted before
// any write). ANYPS3_RSX_BLIT_PIXEL_PATH=1 (or a test setting this flag) forces it; the differential test compares both.
static std::uint64_t rows_executed=0;   // transfers completed by the row-wise path (diagnostics / differential test)
static bool force_pixel_path=[]{const char* v=std::getenv("ANYPS3_RSX_BLIT_PIXEL_PATH");return v && std::string(v)=="1";}();
// Row-wise SRCCOPY transfer: one Range per touched source row and per destination row instead of two per pixel
// (every pixel Range is a slow guest lease on the renderer thread). It applies to linear images whose touched bytes
// overlap no bound tile of the same location; the colour arithmetic is the reference's. Everything is read and every
// Range is created before the first byte is written, so a failure leaves the guest untouched: it returns false and the
// caller runs the per-pixel path, which raises the original error. Source bytes between the touched pixels of a row
// (downscale, row padding) are covered by the row Range but never used; the observer is only told about reads of them.
static bool scaled_rows(PpuState& s,const Image& src,const Image& dst,unsigned src_format,unsigned dst_format,bool raw_copy,unsigned filter,
    std::int64_t dx,std::int64_t dy,std::int64_t u,std::int64_t v,unsigned ox,unsigned oy,unsigned x0,unsigned y0,unsigned x1,unsigned y1){
    std::vector<std::vector<std::uint8_t>> output;std::vector<GuestMemory::Range> targets;unsigned dst_span=0;
    try{
        if(src.swizzled || dst.swizzled || x0>=x1 || y0>=y1)return false;
        auto clamp_to=[](std::int64_t value,std::int64_t last){return std::clamp<std::int64_t>(value,0,last);};
        const std::int64_t xa=floor_fixed(u+std::int64_t(x0-ox)*dx),xb=floor_fixed(u+std::int64_t(x1-1-ox)*dx);
        const std::int64_t ya=floor_fixed(v+std::int64_t(y0-oy)*dy),yb=floor_fixed(v+std::int64_t(y1-1-oy)*dy);
        const std::int64_t extra=filter?1:0;
        const std::int64_t cx0=clamp_to(std::min(xa,xb),src.width-1),cx1=clamp_to(std::max(xa,xb)+extra,src.width-1);
        const std::int64_t cy0=clamp_to(std::min(ya,yb),src.height-1),cy1=clamp_to(std::max(ya,yb)+extra,src.height-1);
        const std::uint64_t rows=std::uint64_t(cy1-cy0+1),span=std::uint64_t(cx1-cx0+1)*src.bpp;
        dst_span=(x1-x0)*dst.bpp;const unsigned out_rows=y1-y0;
        if(rows*span>(UINT64_C(64)<<20) || std::uint64_t(out_rows)*dst_span>(UINT64_C(64)<<20) || dst.bpp!=dest_bpp(dst_format))return false;
        auto src_at=[&](std::int64_t row){return std::uint64_t(src.offset)+std::uint64_t(row)*src.pitch+std::uint64_t(cx0)*src.bpp;};
        auto dst_at=[&](unsigned y){return std::uint64_t(dst.offset)+std::uint64_t(y)*dst.pitch+std::uint64_t(x0)*dst.bpp;};
        const auto src_lo=src_at(cy0),src_hi=src_at(cy1)+span,dst_lo=dst_at(y0),dst_hi=dst_at(y1-1)+dst_span;
        if(src_hi>UINT32_MAX || dst_hi>UINT32_MAX)return false;
        for(const auto& tile:s.rsx.tiles){
            if(!tile.bound)continue;
            const std::uint64_t first=tile.offset,last=first+tile.size;
            if(tile.location==location(src.context) && src_lo<last && src_hi>first)return false;
            if(tile.location==location(dst.context) && dst_lo<last && dst_hi>first)return false;
        }
        std::vector<std::vector<std::uint8_t>> source(rows);
        auto row_bytes=[&](std::int64_t row){auto& bytes=source[row-cy0];
            if(bytes.empty()){bytes.resize(span);access(s,src.context,std::int64_t(src_at(row)),unsigned(span),false).read(0,bytes.data(),unsigned(span));}
            return bytes.data();};
        auto sample=[&](std::int64_t x,std::int64_t y){x=clamp_to(x,src.width-1);y=clamp_to(y,src.height-1);
            const auto* p=row_bytes(y)+(x-cx0)*src.bpp;
            if(raw_copy)return Color{p[0],p[1],p[2],p[3]};
            return read(p,src_format);};
        output.resize(out_rows);
        for(unsigned y=y0;y<y1;++y){
            auto& row=output[y-y0];row.resize(dst_span);
            const auto fy=v+std::int64_t(y-oy)*dy,iy=floor_fixed(fy);
            for(unsigned x=x0;x<x1;++x){
                const auto fx=u+std::int64_t(x-ox)*dx,ix=floor_fixed(fx);
                Color c=sample(ix,iy);
                if(filter){
                    const std::uint64_t a=fx-ix*1048576,b=fy-iy*1048576;
                    const auto c10=sample(ix+1,iy),c01=sample(ix,iy+1),c11=sample(ix+1,iy+1);
                    for(unsigned k=0;k<4;++k)c[k]=(std::uint64_t(c[k])*(1048576-a)*(1048576-b)+std::uint64_t(c10[k])*a*(1048576-b)+
                        std::uint64_t(c01[k])*(1048576-a)*b+std::uint64_t(c11[k])*a*b+(UINT64_C(1)<<39))>>40;
                }
                const auto bytes=packed(dst_format,c);std::copy_n(bytes.data(),dst.bpp,row.data()+std::size_t(x-x0)*dst.bpp);
            }
        }
        targets.reserve(out_rows);
        for(unsigned y=y0;y<y1;++y)targets.push_back(access(s,dst.context,std::int64_t(dst_at(y)),dst_span,true));
    }catch(const std::exception&){return false;}
    for(std::size_t i=0;i<targets.size();++i)targets[i].write(0,output[i].data(),dst_span);
    ++rows_executed;return true;
}
// Batch paths for everything scaled_rows rejects: bound tiles, Morton-swizzled images and SRCCOPY_AND (zero-alpha discard).
// Every physical address comes from Image::locate (the validation of the per-pixel reference). The source is snapshotted with
// one Range read over its covering physical span (bytes between the touched pixels are covered but never used, like in
// scaled_rows), the output bytes are computed on the host, and only the touched destination bytes are written -- merged into
// runs in ascending physical order, through one writable Range created (and so validated) before the first byte is written.
// Anything unusual (sparse spans, tables over budget, two pixels on one byte, any exception) returns false before the first
// write; the caller then runs the per-pixel reference, which raises the original error or produces the same bytes.
static std::uint64_t batch_executed=0;   // transfers completed by a batch path (diagnostics / differential test)
static std::uint64_t batch_copy_executed=0;   // ... of which by the run-wise identity copy
static bool force_no_batch=[]{const char* v=std::getenv("ANYPS3_RSX_BLIT_BATCH");return v && std::string(v)=="0";}();
// A span much larger than the bytes it must contain would turn a cheap snapshot into a copy of unrelated memory.
static bool dense_span(std::uint64_t span,std::uint64_t needed){return span<=(UINT64_C(64)<<20) && span<=std::max<std::uint64_t>(needed*64,UINT64_C(256)<<10);}
struct Chunk {std::uint64_t at;const std::uint8_t* data;std::size_t size;};   // `size` bytes for destination byte offset `at`
// Writes the chunks (in any order; two chunks on one byte or an unwritable/unmapped covering span return false before the first write).
// Past the first write nothing may fall back: a failure there must stop the run, not repeat the transfer.
static bool write_chunks(PpuState& s,const Image& dst,std::vector<Chunk>& chunks,std::uint64_t dlo,std::uint64_t dhi,std::uint64_t needed){
    struct Run {std::uint64_t at;std::size_t stage,size;};
    std::vector<Run> runs;std::vector<std::uint8_t> stage;std::optional<GuestMemory::Range> target;
    try{
        if(!dense_span(dhi-dlo,needed))return false;
        std::stable_sort(chunks.begin(),chunks.end(),[](const Chunk& a,const Chunk& b){return a.at<b.at;});
        stage.reserve(needed);
        for(const auto& chunk:chunks){
            if(!runs.empty()){
                const auto end=runs.back().at+runs.back().size;
                if(chunk.at<end)return false;   // two output pixels on one byte: keep the reference's raster order
                if(chunk.at==end){runs.back().size+=chunk.size;stage.insert(stage.end(),chunk.data,chunk.data+chunk.size);continue;}
            }
            runs.push_back({chunk.at,stage.size(),chunk.size});
            stage.insert(stage.end(),chunk.data,chunk.data+chunk.size);
        }
        target.emplace(access(s,dst.context,std::int64_t(dlo),unsigned(dhi-dlo),true));
    }catch(const std::exception&){return false;}
    for(const auto& run:runs)target->write(unsigned(run.at-dlo),stage.data()+run.stage,unsigned(run.size));
    return true;
}
// Pure byte copy: SRCCOPY, 1:1 unfiltered, identical byte layout on both sides (A8R8G8B8 -> A8R8G8B8, R5G6B5 -> R5G6B5, raw Y32),
// every source tap inside the image. Works on runs of consecutive pixels (Image::run) instead of single pixels.
static bool scaled_copy(PpuState& s,const Image& src,const Image& dst,unsigned src_format,unsigned dst_format,bool raw_copy,unsigned operation,unsigned filter,
    std::int64_t dx,std::int64_t dy,std::int64_t u,std::int64_t v,unsigned ox,unsigned oy,unsigned x0,unsigned y0,unsigned x1,unsigned y1){
    if(operation!=3 || filter || dx!=1048576 || dy!=1048576 || (u&1048575) || (v&1048575) || src.bpp!=dst.bpp)return false;
    if(!raw_copy && !((src_format==3 && dst_format==10) || (src_format==7 && dst_format==4)))return false;
    std::vector<Chunk> chunks;std::vector<std::uint8_t> snapshot;std::uint64_t lo=UINT64_MAX,hi=0,dlo=UINT64_MAX,dhi=0,needed=0;
    struct Seg {std::uint64_t from,to;unsigned bytes;};std::vector<Seg> segments;
    try{
        const std::int64_t sx=(u>>20)-ox,sy=(v>>20)-oy;   // source x = x + sx, source y = y + sy
        if(std::int64_t(x0)+sx<0 || std::int64_t(x1-1)+sx>=std::int64_t(src.width) || std::int64_t(y0)+sy<0 || std::int64_t(y1-1)+sy>=std::int64_t(src.height))return false;
        Image::Memo from_memo,to_memo;
        for(unsigned y=y0;y<y1;++y){
            for(unsigned x=x0;x<x1;){
                const unsigned left=x1-x,source_x=unsigned(std::int64_t(x)+sx),source_y=unsigned(std::int64_t(y)+sy);
                const unsigned n=std::min(src.run(s.rsx,source_x,source_y,left),dst.run(s.rsx,x,y,left));
                const auto from=src.locate(s.rsx,source_x,source_y,&from_memo),to=dst.locate(s.rsx,x,y,&to_memo);
                if(n>1){   // the last pixel of the run must validate too and sit exactly (n-1) pixels further on both sides
                    if(src.locate(s.rsx,source_x+n-1,source_y,&from_memo)!=from+std::uint64_t(n-1)*src.bpp ||
                       dst.locate(s.rsx,x+n-1,y,&to_memo)!=to+std::uint64_t(n-1)*dst.bpp)return false;
                }
                const unsigned bytes=n*src.bpp;
                segments.push_back({from,to,bytes});needed+=bytes;
                lo=std::min(lo,from);hi=std::max<std::uint64_t>(hi,from+bytes);dlo=std::min(dlo,to);dhi=std::max<std::uint64_t>(dhi,to+bytes);
                x+=n;
            }
        }
        if(segments.empty() || hi>UINT32_MAX || !dense_span(hi-lo,needed))return false;
        snapshot.resize(hi-lo);
        access(s,src.context,std::int64_t(lo),unsigned(hi-lo),false).read(0,snapshot.data(),unsigned(hi-lo));
        chunks.reserve(segments.size());
        for(const auto& segment:segments){
            const auto* data=snapshot.data()+(segment.from-lo);
            if(!chunks.empty() && chunks.back().at+chunks.back().size==segment.to && chunks.back().data+chunks.back().size==data)chunks.back().size+=segment.bytes;
            else chunks.push_back({segment.to,data,segment.bytes});
        }
    }catch(const std::exception&){return false;}
    if(!write_chunks(s,dst,chunks,dlo,dhi,needed))return false;
    ++batch_executed;++batch_copy_executed;return true;
}
static bool scaled_batch(PpuState& s,const Image& src,const Image& dst,unsigned src_format,unsigned dst_format,bool raw_copy,unsigned operation,unsigned filter,
    std::int64_t dx,std::int64_t dy,std::int64_t u,std::int64_t v,unsigned ox,unsigned oy,unsigned x0,unsigned y0,unsigned x1,unsigned y1){
    if(scaled_copy(s,src,dst,src_format,dst_format,raw_copy,operation,filter,dx,dy,u,v,ox,oy,x0,y0,x1,y1))return true;
    std::vector<Chunk> chunks;std::uint64_t dlo=UINT64_MAX,dhi=0,needed=0;
    std::vector<std::uint8_t> bytes;   // output pixels in raster order (the chunks point into it)
    try{
        if(x0>=x1 || y0>=y1 || dst.bpp!=dest_bpp(dst_format))return false;
        auto clamp_to=[](std::int64_t value,std::int64_t last){return std::clamp<std::int64_t>(value,0,last);};
        const std::int64_t xa=floor_fixed(u+std::int64_t(x0-ox)*dx),xb=floor_fixed(u+std::int64_t(x1-1-ox)*dx);
        const std::int64_t ya=floor_fixed(v+std::int64_t(y0-oy)*dy),yb=floor_fixed(v+std::int64_t(y1-1-oy)*dy);
        const std::int64_t extra=filter?1:0;
        const std::int64_t cx0=clamp_to(std::min(xa,xb),src.width-1),cx1=clamp_to(std::max(xa,xb)+extra,src.width-1);
        const std::int64_t cy0=clamp_to(std::min(ya,yb),src.height-1),cy1=clamp_to(std::max(ya,yb)+extra,src.height-1);
        const std::uint64_t rw=std::uint64_t(cx1-cx0+1),rh=std::uint64_t(cy1-cy0+1),out_pixels=std::uint64_t(x1-x0)*(y1-y0);
        if(rw*rh>(UINT64_C(4)<<20) || out_pixels>(UINT64_C(16)<<20))return false;
        // 1. physical address of every source pixel of the touched rectangle, then one snapshot of the covering span.
        std::vector<std::uint32_t> table(rw*rh);
        std::uint64_t lo=UINT64_MAX,hi=0;
        {Image::Memo memo;
            for(std::uint64_t y=0;y<rh;++y)for(std::uint64_t x=0;x<rw;++x){
                const auto at=src.locate(s.rsx,unsigned(cx0+x),unsigned(cy0+y),&memo);
                if(at>UINT32_MAX-4)return false;
                table[y*rw+x]=std::uint32_t(at);lo=std::min(lo,at);hi=std::max<std::uint64_t>(hi,at+src.bpp);}}
        if(!dense_span(hi-lo,rw*rh*src.bpp))return false;
        std::vector<std::uint8_t> snapshot(hi-lo);
        access(s,src.context,std::int64_t(lo),unsigned(hi-lo),false).read(0,snapshot.data(),unsigned(hi-lo));
        auto fetch=[&](std::int64_t x,std::int64_t y){
            x=clamp_to(x,src.width-1);y=clamp_to(y,src.height-1);
            if(x<cx0 || x>cx1 || y<cy0 || y>cy1)throw std::runtime_error("RSX blit batch tap outside its table");
            return snapshot.data()+(table[std::uint64_t(y-cy0)*rw+std::uint64_t(x-cx0)]-lo);};
        auto sample=[&](std::int64_t x,std::int64_t y){const auto* p=fetch(x,y);
            if(raw_copy)return Color{p[0],p[1],p[2],p[3]};
            return read(p,src_format);};
        // 2. colours and destination addresses in raster order (the pixel reference's order).
        bytes.reserve(out_pixels*dst.bpp);std::vector<std::uint64_t> where;where.reserve(out_pixels);
        {Image::Memo memo;
            for(unsigned y=y0;y<y1;++y)for(unsigned x=x0;x<x1;++x){
                const auto fx=u+std::int64_t(x-ox)*dx,fy=v+std::int64_t(y-oy)*dy;
                const auto ix=floor_fixed(fx),iy=floor_fixed(fy);
                Color c=sample(ix,iy);
                if(filter){
                    const std::uint64_t a=fx-ix*1048576,b=fy-iy*1048576;
                    const auto c10=sample(ix+1,iy),c01=sample(ix,iy+1),c11=sample(ix+1,iy+1);
                    for(unsigned k=0;k<4;++k)c[k]=(std::uint64_t(c[k])*(1048576-a)*(1048576-b)+std::uint64_t(c10[k])*a*(1048576-b)+
                        std::uint64_t(c01[k])*(1048576-a)*b+std::uint64_t(c11[k])*a*b+(UINT64_C(1)<<39))>>40;
                }
                if(operation==0 && !c[0])continue;
                const auto at=dst.locate(s.rsx,x,y,&memo);
                const auto packed_color=packed(dst_format,c);
                where.push_back(at);bytes.insert(bytes.end(),packed_color.data(),packed_color.data()+dst.bpp);
                dlo=std::min(dlo,at);dhi=std::max<std::uint64_t>(dhi,at+dst.bpp);
            }}
        if(where.empty()){++batch_executed;return true;}   // every pixel discarded: nothing to write
        // 3. chunks of raster-consecutive pixels with consecutive physical bytes.
        needed=where.size()*dst.bpp;chunks.reserve(where.size()/4+1);
        for(std::uint64_t i=0;i<where.size();++i){
            if(!chunks.empty() && chunks.back().at+chunks.back().size==where[i])chunks.back().size+=dst.bpp;
            else chunks.push_back({where[i],bytes.data()+i*dst.bpp,dst.bpp});}
    }catch(const std::exception&){return false;}
    if(!write_chunks(s,dst,chunks,dlo,dhi,needed))return false;
    ++batch_executed;return true;
}
static void scaled(PpuState& s,unsigned point){
    auto& gpu=s.rsx;
    const auto clip=state(gpu,0xc30c),out=state(gpu,0xc314);
    // Empty transfers are documented and need no surface or source mapping.
    if(!(clip&0xffff) || !(clip>>16) || !(out&0xffff) || !(out>>16))return;
    const auto operation=state(gpu,0xc304),conversion=get(gpu,0xc2fc,1);
    validate_operation(gpu,operation,0xc188);
    const auto src_format=state(gpu,0xc300),in_size=state(gpu,0xc400),in_format=state(gpu,0xc404);
    Image src;src.width=in_size&0xffff;src.height=in_size>>16;src.bpp=source_bpp(src_format);
    src.pitch=in_format&0xffff;if(!src.pitch)src.pitch=src.width*src.bpp;
    if(!src.width || !src.height || src.width>4096 || src.height>4096 || src.pitch<src.width*src.bpp)
        throw std::runtime_error("invalid NV3089 input image size/pitch");
    const auto origin=(in_format>>16)&255,filter=in_format>>24;
    if((origin!=1 && origin!=2) || filter>1)throw std::runtime_error("unsupported NV3089 origin/interpolation");
    const std::int64_t dx=std::int32_t(state(gpu,0xc318)),dy=std::int32_t(state(gpu,0xc31c));
    if(!dx || !dy)throw std::runtime_error("invalid NV3089 scale factor");
    src.context=state(gpu,0xc184);src.offset=state(gpu,0xc408);
    const auto out_point=state(gpu,0xc310),clip_point=state(gpu,0xc308);
    const unsigned ox=out_point&0xffff,oy=out_point>>16,ow=out&0xffff,oh=out>>16;
    if(std::uint64_t(ox)+ow>4096 || std::uint64_t(oy)+oh>4096)throw std::runtime_error("NV3089 output image too large");
    const unsigned x0=std::max(ox,clip_point&0xffff),y0=std::max(oy,clip_point>>16);
    const unsigned x1=std::min(ox+ow,(clip_point&0xffff)+(clip&0xffff)),y1=std::min(oy+oh,(clip_point>>16)+(clip>>16));
    if(x0>=x1 || y0>=y1)return;
    unsigned dst_format=0;const auto dst=destination(gpu,state(gpu,0xc198),ox+ow,oy+oh,dst_format);
    validate_conversion(conversion,dst_format,src_format,filter!=0);
    // Y32 is untyped data. The source-format override in the surface path
    // preserves all bits; do not invent luminance or interpolate opaque words.
    const bool raw_copy=dst_format==11;
    if(raw_copy && (src.bpp!=4 || filter || operation!=3))
        throw std::runtime_error("NV3089 Y32 requires unfiltered 32-bit SRCCOPY");
    const std::int64_t u=std::int64_t(point&0xffff)*65536-(origin==1?524288:0);
    const std::int64_t v=std::int64_t(point>>16)*65536-(origin==1?524288:0);
#ifdef ANYPS3_RSX_VK
    if(gpu.graphics_session && operation==3 && !filter && dx==1048576 && dy==1048576 && u==0 && v==0 && !dst.swizzled &&
       ox==0 && oy==0 && x0==0 && y0==0 && x1==ow && y1==oh && ow==src.width && oh==src.height &&
       ((src_format==3 && dst_format==10) || (src_format==7 && dst_format==4))){
        GuestMemory::ObserverPause acquire(s.memory);
        const auto from=access(s,src.context,src.offset,(oh-1)*std::uint64_t(src.pitch)+ow*src.bpp,false);
        const auto to=access(s,dst.context,dst.offset,(oh-1)*std::uint64_t(dst.pitch)+ow*dst.bpp,true);
        if(static_cast<rsxgraphics::Session*>(gpu.graphics_session)->copy(from,to,ow*src.bpp,oh,src.pitch,dst.pitch,dst_format==4?3:8)){
            ::RsxPerfCounters::add(::RsxPerfCounters::blit_calls);::RsxPerfCounters::add(::RsxPerfCounters::blit_gpu);
            ::RsxPerfCounters::add(::RsxPerfCounters::blit_pixels,std::uint64_t(ow)*oh);return;}
    }
#endif
    {   // Diagnostic (ANYPS3_RSX_BLIT_TRACE=1): the first 300 scaled transfers, to size the per-pixel Range cost.
        static const bool trace=[]{const char* v=std::getenv("ANYPS3_RSX_BLIT_TRACE");return v && std::string(v)=="1";}();
        // ANYPS3_RSX_BLIT_TRACE=2: running totals every 120 transfers (steady-state mix: calls, pixels, row-wise share, tiled share).
        static const bool stats=[]{const char* v=std::getenv("ANYPS3_RSX_BLIT_TRACE");return v && std::string(v)=="2";}();
        static std::uint64_t calls=0,pixels=0,tiled_calls=0,tiled_pixels=0,filtered=0;
        if(stats){
            bool tiled=false;
            for(const auto& tile:gpu.tiles)if(tile.bound && (tile.location==location(src.context) || tile.location==location(dst.context)))tiled=true;
            ++calls;pixels+=std::uint64_t(x1-x0)*(y1-y0);filtered+=filter!=0;if(tiled){++tiled_calls;tiled_pixels+=std::uint64_t(x1-x0)*(y1-y0);}
            if(calls%120==0)std::fprintf(stderr,"BLIT_STATS calls=%llu pixels=%llu tile_location_calls=%llu tile_location_pixels=%llu filtered=%llu rows_path=%llu\n",
                (unsigned long long)calls,(unsigned long long)pixels,(unsigned long long)tiled_calls,(unsigned long long)tiled_pixels,(unsigned long long)filtered,(unsigned long long)rows_executed);
        }
        static unsigned traced=0;
        if(trace && traced++<300){
            bool src_tile=false,dst_tile=false;
            for(const auto& tile:gpu.tiles){if(!tile.bound)continue;if(tile.location==location(src.context))src_tile=true;if(tile.location==location(dst.context))dst_tile=true;}
            std::fprintf(stderr,"BLIT_TRACE scaled op=%u filter=%u src{ctx=%08x off=%u pitch=%u %ux%u fmt=%u tilesloc=%d} dst{ctx=%08x off=%u pitch=%u %ux%u fmt=%u swz=%d tilesloc=%d} rect=%u,%u..%u,%u dx=%lld dy=%lld u=%lld v=%lld\n",
                operation,filter,src.context,src.offset,src.pitch,src.width,src.height,src_format,int(src_tile),dst.context,dst.offset,dst.pitch,dst.width,dst.height,dst_format,int(dst.swizzled),int(dst_tile),
                x0,y0,x1,y1,(long long)dx,(long long)dy,(long long)u,(long long)v);
        }
    }
    // Path accounting (always on, a few relaxed stores per transfer): PERF RSX_COUNTERS blit_*; the _ns counters are host wall time.
    const auto path_start=std::chrono::steady_clock::now();
    auto account=[&](::RsxPerfCounters::Id calls,::RsxPerfCounters::Id ns){
        ::RsxPerfCounters::add(::RsxPerfCounters::blit_calls);::RsxPerfCounters::add(calls);
        ::RsxPerfCounters::add(::RsxPerfCounters::blit_pixels,std::uint64_t(x1-x0)*(y1-y0));
        ::RsxPerfCounters::add(ns,std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-path_start).count());};
    if(operation==3 && !force_pixel_path && scaled_rows(s,src,dst,src_format,dst_format,raw_copy,filter,dx,dy,u,v,ox,oy,x0,y0,x1,y1)){
        account(::RsxPerfCounters::blit_rows,::RsxPerfCounters::blit_rows_ns);return;}
    if(!force_pixel_path && !force_no_batch && scaled_batch(s,src,dst,src_format,dst_format,raw_copy,operation,filter,dx,dy,u,v,ox,oy,x0,y0,x1,y1)){
        account(::RsxPerfCounters::blit_batch,::RsxPerfCounters::blit_batch_ns);return;}
    // Keep the ordinary pixel descriptor compact. Only a physical pixel that
    // straddles mappings needs a retained scatter range; mappings cannot change
    // during this synchronous command.
    struct Pixel {std::uint8_t* direct;std::shared_ptr<GuestMemory::Range> split;Color color;};
    std::vector<Pixel> pixels;pixels.reserve(std::size_t(x1-x0)*(y1-y0));
    auto sample=[&](std::int64_t x,std::int64_t y){return read(src.pixel(s,std::clamp<std::int64_t>(x,0,src.width-1),std::clamp<std::int64_t>(y,0,src.height-1),false),src_format);};
    for(unsigned y=y0;y<y1;++y)for(unsigned x=x0;x<x1;++x){
        const auto fx=u+std::int64_t(x-ox)*dx,fy=v+std::int64_t(y-oy)*dy;
        const auto ix=floor_fixed(fx),iy=floor_fixed(fy);
        Color c;
        if(raw_copy){
            const auto p=src.pixel(s,std::clamp<std::int64_t>(ix,0,src.width-1),std::clamp<std::int64_t>(iy,0,src.height-1),false);
            std::array<std::uint8_t,4> bytes{};p.read(0,bytes.data(),4);
            c={bytes[0],bytes[1],bytes[2],bytes[3]};
        }else c=sample(ix,iy);
        if(filter){
            const std::uint64_t a=fx-ix*1048576,b=fy-iy*1048576;
            const auto c10=sample(ix+1,iy),c01=sample(ix,iy+1),c11=sample(ix+1,iy+1);
            for(unsigned k=0;k<4;++k)c[k]=(std::uint64_t(c[k])*(1048576-a)*(1048576-b)+std::uint64_t(c10[k])*a*(1048576-b)+
                std::uint64_t(c01[k])*(1048576-a)*b+std::uint64_t(c11[k])*a*b+(UINT64_C(1)<<39))>>40;
        }
        if(operation==0 && !c[0])continue;
        auto range=dst.pixel(s,x,y,true);auto* direct=s.memory.concurrent()?nullptr:range.pointer(0,range.size);
        auto scatter=direct?nullptr:std::make_shared<GuestMemory::Range>(std::move(range));
        pixels.push_back({direct,std::move(scatter),c});
    }
    // Snapshot also makes in-place conversion/scaling and IO aliases safe.
    for(const auto& p:pixels){
        if(p.direct){const auto bytes=packed(dst_format,p.color);const auto n=dest_bpp(dst_format);
            s.memory.mark_write(p.direct,n);std::copy_n(bytes.data(),n,p.direct);
        }else write(s,*p.split,dst_format,p.color);
    }
    account(::RsxPerfCounters::blit_pixel,::RsxPerfCounters::blit_pixel_ns);
}
static void cpu(PpuState& s,unsigned reg,unsigned value){
    auto& gpu=s.rsx;const auto size=state(gpu,0xa308),input=state(gpu,0xa30c);
    const unsigned index=(reg-0xa400)/4,ow=size&0xffff,oh=size>>16;
    if(index>=ow || !oh)return;
    // RPCS3's verified COLOR path is a register-indexed scanline. Do not guess
    // multi-row streaming or packed halfword semantics from the NV40 class.
    if(oh!=1 || (input>>16)!=1 || (input&0xffff)<ow)
        throw std::runtime_error("unsupported NV308A multi-row/packed input");
    const auto operation=state(gpu,0xa2fc),conversion=get(gpu,0xa2f8,1);
    validate_operation(gpu,operation,0xa18c);
    if(get(gpu,0xa184,0) || get(gpu,0xa188,0))throw std::runtime_error("unsupported NV308A color key/clip object");
    const auto src_format=cpu_source_format(state(gpu,0xa300));
    const auto point=state(gpu,0xa304);const unsigned x=(point&0xffff)+index,y=point>>16;
    unsigned fmt=0;const auto dst=destination(gpu,state(gpu,0xa19c),(point&0xffff)+ow,y+1,fmt);
    // NV308A's context accepts SURFACE2D only (nv1_ifc.xml:99-102).
    if(dst.swizzled)throw std::runtime_error("NV308A context does not support swizzled surfaces");
    validate_conversion(conversion,fmt,src_format);
    Color c{value>>24,(value>>16)&255,(value>>8)&255,value&255};
    if(fmt!=11 && src_format==4)c[0]=255;
    if(fmt==11 && operation!=3)throw std::runtime_error("NV308A Y32 requires SRCCOPY");
    if(operation==0 && !c[0])return;
    write(s,dst.pixel(s,x,y,true),fmt,c);
}
} // namespace rsxblit
'''
