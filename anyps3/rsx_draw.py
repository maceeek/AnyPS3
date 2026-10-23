"""Native RSX draw path: vertex fetch, vertex/fragment program interpretation
and rasterization into the guest surfaces in RSX memory.

Guest memory stays the source of truth, as for clears and scanout. Program
field layouts follow RPCS3 RSXVertexProgram.h / RSXFragmentProgram.h and
gcm_enums.h. Shading uses host binary32 arithmetic (the RSX is not an IEEE
exact unit); unsupported state, opcodes and formats stop explicitly.
"""

from . import rsx_fpdecode
from .rsx_perf import CPP as PERF_CPP
from .rsx_vk import CPP as VK_DRAW_CPP
from .rsx_graphics import CPP as GRAPHICS_CPP
from .rsx_fixed import CPP as FIXED_CPP
from .rsx_gaps import CPP as GAPS_CPP
from .rsx_swz import CPP as SWZ_CPP
from .rsx_tex import CPP as TEX_CPP

TYPES = r'''
struct RsxDrawState {
    std::uint32_t primitive=0;
    bool indexed=false;
    std::vector<std::pair<std::uint32_t,std::uint32_t>> ranges;
    std::vector<std::pair<std::uint32_t,unsigned>> elements; // inline index and source width
    std::uint64_t draws=0,triangles=0,pixels=0;
    // Register attributes: 0=float, 1=ub normalized, 2=short, 3=short normalized.
    struct Immediate { unsigned count=0,type=0; std::array<std::uint32_t,4> words{}; };
    std::array<Immediate,16> immediate{};
    // Immediate-mode vertices: register snapshots taken when ATTR0 completes.
    std::vector<std::array<Immediate,16>> immediate_vertices;
    bool inline_active=false;std::vector<std::uint8_t> inline_bytes;
    // Per-RSX scratch, never guest state. No retained memory pointers or
    // decoded indices: every draw reads the current mapping and bytes.
    std::vector<std::uint32_t> scratch_indices;
    std::vector<std::uint8_t> scratch_index_bytes;
    std::vector<std::array<std::uint32_t,3>> scratch_triangles;
};
'''

CPP = r'''
#include <cmath>
#include <chrono>
#include <atomic>
#include <optional>
#include <cstring>
#include <limits>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <functional>
#include <exception>
#include <climits>
#include <cfenv>
// The interpreter loops are hot; optimize them independently of the -Og
// guest translation. This does not change their semantics.
#pragma GCC push_options
#pragma GCC optimize("O2")
#if defined(__SSSE3__) && defined(__SSE4_1__)
#include <immintrin.h>
#endif
namespace rsxdraw {
using Vec4=std::array<float,4>;
// Big-endian index decode: out[i]=raw+base, or UINT32_MAX for the restart value
// when restart is enabled. largest/any cover the unrestarted raw values only,
// for the caller's overflow check. Width is 2 or 4 bytes.
[[maybe_unused]] static void decode_indices(const std::uint8_t* in,std::uint32_t* out,unsigned count,unsigned width,bool restart,
        std::uint32_t restart_index,std::uint32_t base,std::uint32_t& largest,bool& any){
    unsigned i=0;
#if defined(__SSSE3__) && defined(__SSE4_1__)
    __m128i maxv=_mm_setzero_si128(),seen=_mm_setzero_si128();
    const __m128i basev=_mm_set1_epi32(int(base)),restartv=_mm_set1_epi32(int(restart_index)),ones=_mm_set1_epi32(-1);
    auto lanes=[&](__m128i x,std::uint32_t* to){
        if(restart){const __m128i hit=_mm_cmpeq_epi32(x,restartv);
            _mm_storeu_si128(reinterpret_cast<__m128i*>(to),_mm_or_si128(_mm_add_epi32(x,basev),hit));
            maxv=_mm_max_epu32(maxv,_mm_andnot_si128(hit,x));seen=_mm_or_si128(seen,_mm_andnot_si128(hit,ones));}
        else{_mm_storeu_si128(reinterpret_cast<__m128i*>(to),_mm_add_epi32(x,basev));maxv=_mm_max_epu32(maxv,x);seen=ones;}
    };
    if(width==2){
        const __m128i low=_mm_setr_epi8(1,0,-1,-1,3,2,-1,-1,5,4,-1,-1,7,6,-1,-1),high=_mm_setr_epi8(9,8,-1,-1,11,10,-1,-1,13,12,-1,-1,15,14,-1,-1);
        for(;i+8<=count;i+=8){const __m128i v=_mm_loadu_si128(reinterpret_cast<const __m128i*>(in+2*i));
            lanes(_mm_shuffle_epi8(v,low),out+i);lanes(_mm_shuffle_epi8(v,high),out+i+4);}
    }else{
        const __m128i swap=_mm_setr_epi8(3,2,1,0,7,6,5,4,11,10,9,8,15,14,13,12);
        for(;i+4<=count;i+=4)lanes(_mm_shuffle_epi8(_mm_loadu_si128(reinterpret_cast<const __m128i*>(in+4*i)),swap),out+i);
    }
    alignas(16) std::uint32_t m[4];_mm_store_si128(reinterpret_cast<__m128i*>(m),maxv);
    largest=std::max(std::max(m[0],m[1]),std::max(m[2],m[3]));any=!_mm_testz_si128(seen,seen);
#else
    largest=0;any=false;
#endif
    for(;i<count;++i){
        const std::uint32_t raw=width==2?(std::uint32_t(in[2*i])<<8|in[2*i+1]):(std::uint32_t(in[4*i])<<24|std::uint32_t(in[4*i+1])<<16|std::uint32_t(in[4*i+2])<<8|in[4*i+3]);
        if(restart && raw==restart_index){out[i]=UINT32_MAX;continue;}
        largest=std::max(largest,raw);any=true;out[i]=raw+base;
    }
}
// Minimum and maximum over every index of n triangles (n may be zero).
[[maybe_unused]] static void index_extent(const std::array<std::uint32_t,3>* t,std::size_t n,unsigned& lo,unsigned& hi){
    static_assert(sizeof(std::array<std::uint32_t,3>)==12,"packed triangle indices");
    std::size_t v=0;const std::size_t values=3*n;
#if defined(__SSSE3__) && defined(__SSE4_1__)
    if(values>=8){const auto* bytes=reinterpret_cast<const char*>(t);__m128i minv=_mm_set1_epi32(-1),maxv=_mm_setzero_si128();
        for(;v+4<=values;v+=4){const __m128i x=_mm_loadu_si128(reinterpret_cast<const __m128i*>(bytes+4*v));minv=_mm_min_epu32(minv,x);maxv=_mm_max_epu32(maxv,x);}
        alignas(16) std::uint32_t a[4],b[4];_mm_store_si128(reinterpret_cast<__m128i*>(a),minv);_mm_store_si128(reinterpret_cast<__m128i*>(b),maxv);
        for(unsigned k=0;k<4;++k){lo=std::min(lo,a[k]);hi=std::max(hi,b[k]);}}
#endif
    for(;v<values;++v){const auto id=t[v/3][v%3];lo=std::min(lo,id);hi=std::max(hi,id);}
}
''' + PERF_CPP + r'''
// One pool per calling host thread; workers never enter GuestMemory. The caller
// participates as stripe 0. Jobs finish before draw-owned references expire.
class DrawPool {
    std::mutex mutex;
    std::condition_variable start,finished;
    std::vector<std::thread> workers;
    void (*job)(const void*,unsigned)=nullptr;const void* job_context=nullptr;
    std::vector<std::exception_ptr> errors;
    std::uint64_t generation=0;
    unsigned active=0,pending=0;
    bool stopping=false;
    std::fenv_t environment{};
    void invoke(unsigned index) noexcept {
        // A reused worker must use this draw's host rounding mode, rather
        // than the one inherited when its thread happened to be created.
        if(index)std::fesetenv(&environment);
        try{job(job_context,index);}catch(...){errors[index]=std::current_exception();}
    }
public:
    DrawPool()=default;
    DrawPool(const DrawPool&)=delete;
    DrawPool& operator=(const DrawPool&)=delete;
    ~DrawPool(){
        {std::lock_guard<std::mutex> lock(mutex);stopping=true;}
        start.notify_all();
        for(auto& thread:workers)thread.join();
    }
    template<class Function> bool run(unsigned count,const Function& function){
        {
            std::lock_guard<std::mutex> lock(mutex);
            // Grow only between jobs. A new worker must observe this generation
            // under the same lock before the next job can be published.
            try{
                while(workers.size()+1<count){
                    const unsigned index=workers.size()+1;
                    const auto seen=generation;
                    workers.emplace_back([this,index,seen]{worker_from(index,seen);});
                }
            }catch(const std::system_error&){return false;} // No job dispatched; caller can run sequentially.
            errors.assign(count,{});job_context=&function;
            job=[](const void* context,unsigned i){(*static_cast<const Function*>(context))(i);};
            std::fegetenv(&environment);
            active=count;pending=count-1;++generation;
        }
        start.notify_all();invoke(0);
        {
            std::unique_lock<std::mutex> lock(mutex);
            finished.wait(lock,[&]{return !pending;});job=nullptr;job_context=nullptr;
        }
        // Stable stripe order, independent of worker completion order.
        for(const auto& error:errors)if(error)std::rethrow_exception(error);
        return true;
    }
private:
    void worker_from(unsigned index,std::uint64_t seen){
        std::unique_lock<std::mutex> lock(mutex);
        for(;;){
            start.wait(lock,[&]{return stopping || seen!=generation;});
            if(stopping)return;
            seen=generation;
            if(index>=active)continue;
            lock.unlock();invoke(index);lock.lock();
            if(!--pending)finished.notify_one();
        }
    }
};
static unsigned draw_thread_count(){
    const char* value=std::getenv("ANYPS3_RSX_THREADS");
    // hardware_concurrency() reads sysfs on every call (~2 % of the RSX worker, profile 331ts).
    static const unsigned host_threads=std::max(1u,std::thread::hardware_concurrency());
    if(!value)return host_threads;
    unsigned count=0;
    for(const char* p=value;*p;++p){
        if(*p<'0' || *p>'9' || count>(UINT_MAX-unsigned(*p-'0'))/10)
            throw std::runtime_error("invalid ANYPS3_RSX_THREADS: expected positive integer");
        count=count*10+unsigned(*p-'0');
    }
    if(!count)throw std::runtime_error("invalid ANYPS3_RSX_THREADS: expected positive integer");
    return count;
}
static float bits_float(std::uint32_t v){float f;std::memcpy(&f,&v,4);return f;}
static float half_float(std::uint16_t h){
    // All binary16 values are exact binary32 values. Integer construction also
    // preserves the old canonical quiet-NaN and its sign, in every host mode.
    static const auto table=[] {
        std::array<float,65536> result{};
        for(unsigned n=0;n<65536;++n){
            const unsigned exp=(n>>10)&31;unsigned frac=n&1023,bits=(n&0x8000u)<<16;
            if(exp==31)bits|=0x7f800000u|(frac?0x400000u:0u);
            else if(exp)bits|=((exp+112)<<23)|(frac<<13);
            else if(frac){unsigned e=113;while(!(frac&1024)){frac<<=1;--e;}bits|=(e<<23)|((frac&1023)<<13);}
            result[n]=bits_float(bits);
        }
        return result;
    }();
    return table[h];
}
static std::uint16_t float_half(float f){
    // Round to nearest even binary16 (RSX H registers).
    std::uint32_t x;std::memcpy(&x,&f,4);
    const std::uint32_t sign=(x>>16)&0x8000;std::int32_t exp=((x>>23)&255)-127+15;std::uint32_t mant=x&0x7fffff;
    if(((x>>23)&255)==255)return sign|0x7c00|(mant?0x200:0);
    if(exp>=31)return sign|0x7c00;
    if(exp<=0){
        if(exp<-10)return sign;
        mant|=0x800000;const unsigned shift=14-exp;
        std::uint32_t h=mant>>shift;const std::uint32_t rest=mant&((1u<<shift)-1),half=1u<<(shift-1);
        if(rest>half || (rest==half && (h&1)))++h;
        return sign|h;
    }
    std::uint32_t h=(exp<<10)|(mant>>13);const std::uint32_t rest=mant&0x1fff;
    if(rest>0x1000 || (rest==0x1000 && (h&1)))++h;
    return sign|h;
}
static unsigned logic_op(unsigned op,unsigned src,unsigned dst);
''' + GAPS_CPP + SWZ_CPP + r'''
struct Reg { // gpu.registers access; unwritten registers hold reset value 0 (RPCS3 rsx_methods.cpp:137).
    const NativeRsx& gpu;
    std::uint32_t operator()(std::uint32_t r)const{
        auto it=gpu.registers.find(r);
        return it==gpu.registers.end()?0u:it->second;
    }
    std::uint32_t get(std::uint32_t r,std::uint32_t fallback)const{auto it=gpu.registers.find(r);return it==gpu.registers.end()?fallback:it->second;}
};

static const std::vector<GuestMemory::Region>& rsx_maps(const PpuState& s){return s.memory.regions;}
template<class State> static const auto& rsx_maps(const State& s){return s.maps;}
// Pinned physical identity only: this never publishes a raw guest pointer.
static std::uint8_t* range_identity(const GuestMemory::Range& range,unsigned offset,unsigned count){
    if(offset>range.size || count>range.size-offset)throw std::runtime_error("RSX identity bounds fault");
    for(const auto& part:range.parts)if(offset>=part.offset && offset-part.offset<part.size && count<=part.size-(offset-part.offset))return part.data+offset-part.offset;
    return nullptr;
}
// A payload transaction owns only its declared bytes/pages. The map owner
// pins before handoff; a transferred GpuPages can use the same commit API.
struct ClaimTransfer {
    const GuestMemory::Range& range;
    GuestMemory::RawYield yield;
    std::unique_ptr<GuestMemory::GpuPages> claim;

    static std::vector<GuestMemory::HostSpan> spans(const GuestMemory::Range& r,unsigned offset,unsigned count){
        if(offset>r.size || count>r.size-offset)throw std::runtime_error("RSX transfer bounds fault");
        std::vector<GuestMemory::HostSpan> result;
        for(const auto& part:r.parts){const auto first=std::max(offset,part.offset),last=std::min(offset+count,part.offset+part.size);
            if(first<last)result.push_back({part.data+first-part.offset,last-first,part.owner});}
        return result;
    }
    explicit ClaimTransfer(const GuestMemory::Range& r,std::vector<GuestMemory::HostSpan> declared):range(r),yield(*r.memory){
        if(!r.memory->concurrent())return;
        if(r.memory->renderer_enabled && !r.memory->memory_owner() && !r.memory->local_state().fault_claim){
            r.memory->renderer_pin([&]{claim=std::make_unique<GuestMemory::GpuPages>(*r.memory,std::move(declared));});claim->adopt();
        }else{r.memory->enable_page_broker();claim=std::make_unique<GuestMemory::GpuPages>(*r.memory,std::move(declared));}
        // Zero payload still validates every original mapping and its rights.
        // Each actual operation additionally proves its exact claimed bytes.
        claim->commit_spans(r,0,0,r.writable);
    }
    explicit ClaimTransfer(const GuestMemory::Range& r,unsigned offset,unsigned count):ClaimTransfer(r,spans(r,offset,count)){
        if(claim)claim->commit_spans(r,offset,count,r.writable);
    }
    void read(unsigned offset,std::uint8_t* out,unsigned count){
        if(claim)claim->snapshot_into(range,offset,count,out);
        else range.read(offset,out,count);
    }
    void write(unsigned offset,const std::uint8_t* bytes,unsigned count){
        if(claim)claim->commit(range,offset,bytes,count);else range.write(offset,bytes,count);
    }
    std::uint32_t fetch_or32(unsigned offset,std::uint32_t bits){
        if(claim)return claim->fetch_or32(range,offset,bits);
        std::uint8_t bytes[4];range.read(offset,bytes,4);const auto old=read_be(bytes,4);write_be(bytes,4,old|bits);range.write(offset,bytes,4);return old;
    }
};
// One exact physical claim for all direct vertex inputs in a draw. Range
// identities/rights are still checked for each payload by snapshot(). No
// bounding envelope claims holes between separate attributes or split maps.
struct DrawReadBatch {
    struct Input {const GuestMemory::Range* range=nullptr;unsigned destination=0;};
    std::array<Input,16> inputs{};unsigned count=0;
    void add(const GuestMemory::Range& range,unsigned destination){
        if(count==inputs.size())throw std::runtime_error("RSX draw input capacity");
        inputs[count++]={&range,destination};
    }
    void read(std::uint8_t* output){
        if(!count)return;
        const auto& first=*inputs[0].range;
        if(!first.memory->concurrent()){
            for(unsigned i=0;i<count;++i){const auto& input=inputs[i];input.range->read(0,output+input.destination,input.range->size);}return;
        }
        // Shared reader admission per input (the Range::read protocol used for indices, fragment programs and textures):
        // GPU-authoritative bytes are retired by the observer inside the lease, map identity is checked by
        // Range::check_epoch, every 64-bit word is read atomically. No exclusive page claim and no membarrier per draw
        // (the claim cost ~2 ms/frame in the level). A bulk guest write that overlaps this read UNORDERED is a race on
        // hardware too. The ordered cases are protected by (A) acquire/release: a write completed before the label the
        // FIFO acquired is seen completely, and (B) program order: this read finishes inside rsx_execute_draw, before any
        // later FIFO method can publish a label/semaphore the writer waits for (the read is never deferred past the draw).
        // tests/test_rsx2_draw.py ordered_bulk_writes_ pins both; a future batching/deferral of this read must keep B.
        for(unsigned i=0;i<count;++i){const auto& input=inputs[i];const auto& range=*input.range;
            if(range.memory!=first.memory)throw std::runtime_error("RSX mixed draw input memory");
            range.read(0,output+input.destination,range.size);}
    }
};
// Reverse translation is used only with a pinned original backing. Retain
// first-match guest mapping rules and reject a remapped physical identity.
static GuestMemory::Range pinned_host_range(GuestMemory& memory,const void* pointer,unsigned count,bool write){
    const auto at=reinterpret_cast<std::uintptr_t>(pointer);
    for(const auto& region:memory.regions){const auto base=reinterpret_cast<std::uintptr_t>(region.bytes->data());
        if(at<base || at-base>region.bytes->size() || count>region.bytes->size()-(at-base))continue;
        auto range=memory.range(region.base+at-base,count,write);
        if(range.parts.size()==1 && range.parts[0].owner==region.bytes && range.parts[0].data==pointer)return range;
    }
    throw std::runtime_error("RSX pinned physical span has no current mapping");
}

static std::vector<GuestMemory::Range> pinned_host_ranges(GuestMemory& memory,const void* pointer,unsigned count,bool write){
    std::vector<GuestMemory::Range> ranges;auto at=reinterpret_cast<std::uintptr_t>(pointer);unsigned left=count;
    while(left){unsigned chunk=0;
        for(const auto& region:memory.regions){const auto base=reinterpret_cast<std::uintptr_t>(region.bytes->data());
            if(at>=base && at-base<region.bytes->size()){chunk=std::min<std::uint64_t>(left,region.bytes->size()-(at-base));break;}}
        if(!chunk)throw std::runtime_error("RSX host transfer contains unmapped bytes");
        ranges.push_back(pinned_host_range(memory,reinterpret_cast<const void*>(at),chunk,write));at+=chunk;left-=chunk;
    }
    return ranges;
}
static void pinned_host_read(GuestMemory& memory,const void* pointer,std::uint8_t* out,unsigned count){
    for(const auto& range:pinned_host_ranges(memory,pointer,count,false)){range.read(0,out,range.size);out+=range.size;}
}
[[maybe_unused]] static void pinned_host_write(GuestMemory& memory,const void* pointer,const std::uint8_t* bytes,unsigned count){
    // Full validation precedes any publication, even across physical backings.
    const auto ranges=pinned_host_ranges(memory,pointer,count,true);
    for(const auto& range:ranges){ClaimTransfer transfer(range,0,range.size);transfer.write(0,bytes,range.size);bytes+=range.size;}
}
static std::uint64_t location_address(const NativeRsx& gpu,unsigned location,std::uint32_t offset,std::uint32_t bytes){
    if(location==0){
        if(offset>gpu.local_size || bytes>gpu.local_size-offset)throw std::runtime_error("RSX draw reads outside VRAM");
        return UINT64_C(0xc0000000)+offset;
    }
    if(location==1){
        if(std::uint64_t(offset)+bytes>0x10000000)throw std::runtime_error("RSX draw main-memory read outside IO aperture");
        const auto first=rsx_address(gpu,offset);
        for(std::uint64_t page=(std::uint64_t(offset)|0xfffff)+1;page<std::uint64_t(offset)+bytes;page+=0x100000)
            if(rsx_address(gpu,page)!=first+page-offset)throw std::runtime_error("RSX draw main-memory read spans discontiguous IO pages");
        if(bytes && rsx_address(gpu,offset+bytes-1)!=first+bytes-1)throw std::runtime_error("RSX draw main-memory read spans discontiguous IO pages");
        return first;
    }
    throw std::runtime_error("unsupported RSX draw memory location");
}

static Vec4 sample_vertex_texture(PpuState& s,unsigned unit,const Vec4& coord);

// ---------------- Vertex program ----------------
struct VertexOut { std::array<Vec4,16> o; };
static Vec4 vp_source(const std::array<std::uint32_t,4>& d,unsigned raw,unsigned n,const std::array<Vec4,32>& temp,
                      const std::array<Vec4,16>& input,const NativeRsx& gpu,const std::array<std::array<int,4>,2>& address){
    const unsigned type=raw&3,tmp=(raw>>2)&63;
    Vec4 v{};
    if(type==1){if(tmp>=32)throw std::runtime_error("RSX VP temp register out of range");v=temp[tmp];}
    else if(type==2){
        const int index=int((d[1]>>8)&15)+((d[0]&(1u<<27))?address[(d[0]>>24)&1][d[0]&3]:0);
        if(index<0 || index>=16)throw std::runtime_error("RSX VP indexed input out of range");
        v=input[index];
    }
    else if(type==3){
        const unsigned base=(d[1]>>12)&1023;
        const unsigned c=d[3]&2?(base+unsigned(address[(d[0]>>24)&1][d[0]&3]))&511:base;
        // Direct constant indices >=468 read zero on HW; indexed constants
        // wrap at 512 and may reach an undocumented bank (RPCS3 GetSRC).
        if((d[3]&2) && c>=468)throw std::runtime_error("RSX VP indexed constant reaches undocumented bank");
        if(c<468)for(unsigned i=0;i<4;++i){
            if(!gpu.transform_constants_written[c*4+i])throw std::runtime_error("RSX VP reads unwritten constant "+std::to_string(c));
            v[i]=bits_float(gpu.transform_constants[c*4+i]);
        }
    }else throw std::runtime_error("invalid RSX VP source register type");
    Vec4 s;for(unsigned i=0;i<4;++i)s[i]=v[(raw>>(14-2*i))&3];
    if((d[0]>>(21+n))&1)for(auto& x:s)x=std::fabs(x);
    if((raw>>16)&1)for(auto& x:s)x=-x;
    return s;
}
static VertexOut run_vertex_program(const NativeRsx& gpu,std::uint32_t start,const std::array<Vec4,16>& input,PpuState* state=nullptr){
    if(host_stats().enabled)++host_stats().cpu_vertex;
    VertexOut out;for(auto& r:out.o)r={0,0,0,1};
    std::array<Vec4,32> temp{};
    std::array<std::array<int,4>,2> address{};std::array<Vec4,2> cc{};
    std::vector<unsigned> call_stack;
    unsigned pc=start;
    for(unsigned steps=0;;++steps){
        if(pc>=544 || steps>=65536)throw std::runtime_error("RSX VP PC out of range or execution budget exhausted");
        std::array<std::uint32_t,4> d;
        for(unsigned i=0;i<4;++i){
            if(!gpu.transform_program_written[pc*4+i])throw std::runtime_error("RSX VP executes unwritten instruction");
            d[i]=gpu.transform_program[pc*4+i];
        }
        const unsigned condition=(d[0]>>10)&7,cc_index=(d[0]>>25)&1;
        std::array<bool,4> pass{};
        for(unsigned i=0;i<4;++i){
            const float x=cc[cc_index][(d[0]>>(8-2*i))&3];
            pass[i]=condition==7 || ((condition&1)&&x<0) || ((condition&2)&&x==0) || ((condition&4)&&x>0);
        }
        const bool branch_pass=pass[0]||pass[1]||pass[2]||pass[3];
        const unsigned s0=((d[2]>>23)&511)|((d[1]&255)<<9),s1=(d[2]>>6)&0x1ffff,s2=((d[3]>>21)&2047)|((d[2]&63)<<11);
        const unsigned vec=(d[1]>>22)&31,sca=(d[1]>>27)&31;
        const unsigned dst=(d[3]>>2)&31,vtmp=(d[0]>>15)&63,stmp=(d[3]>>7)&63;
        const bool vec_result=(d[0]>>30)&1,sat=(d[0]>>26)&1;
        auto src=[&](unsigned i){return vp_source(d,i==0?s0:i==1?s1:s2,i,temp,input,gpu,address);};
        auto write=[&](const Vec4& value,bool scalar){
            const unsigned maskbits=scalar?(d[3]>>17)&15:(d[3]>>13)&15; // bit3=x .. bit0=w
            Vec4 v=value;if(sat)for(auto& x:v)x=std::min(1.f,std::max(0.f,x));
            const bool to_output=scalar?!vec_result:vec_result;
            const unsigned tmp=scalar?stmp:vtmp;
            for(unsigned i=0;i<4;++i)if((maskbits&(8>>i)) && (!(d[0]&(1u<<13)) || pass[i])){
                if(!scalar && vec==13){
                    if(!std::isfinite(v[i]) || v[i]<-2147483648.f || v[i]>=2147483648.f)throw std::runtime_error("RSX VP address conversion out of range");
                    if(tmp>1 || (to_output && dst!=31))throw std::runtime_error("invalid RSX VP ARL destination");
                    address[tmp][i]=int(std::floor(v[i]));continue;
                }
                if(tmp==63 && (!to_output || (dst==31 && ((d[0]&(1u<<14)) || (d[0]&(1u<<29))))))cc[cc_index][i]=v[i];
                if(to_output && dst!=31 && !(scalar && tmp!=63)){if(dst>15)throw std::runtime_error("RSX VP output register out of range");out.o[dst][i]=v[i];}
                if(tmp!=63){if(tmp>=32)throw std::runtime_error("RSX VP temp write out of range");temp[tmp][i]=v[i];}
            }
        };
        Vec4 vr{};bool vec_write=true;
        switch(vec){
        case 0:vec_write=false;break;
        case 1:vr=src(0);break;
        case 2:{auto a=src(0),b=src(1);for(unsigned i=0;i<4;++i)vr[i]=a[i]*b[i];break;}
        case 3:{auto a=src(0),b=src(2);for(unsigned i=0;i<4;++i)vr[i]=a[i]+b[i];break;}
        case 4:{auto a=src(0),b=src(1),c=src(2);for(unsigned i=0;i<4;++i)vr[i]=std::fma(a[i],b[i],c[i]);break;}
        case 5:{auto a=src(0),b=src(1);float t=a[0]*b[0]+a[1]*b[1]+a[2]*b[2];vr={t,t,t,t};break;}
        case 6:{auto a=src(0),b=src(1);float t=a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+b[3];vr={t,t,t,t};break;}
        case 7:{auto a=src(0),b=src(1);float t=a[0]*b[0]+a[1]*b[1]+a[2]*b[2]+a[3]*b[3];vr={t,t,t,t};break;}
        case 8:{auto a=src(0),b=src(1);vr={1.f,a[1]*b[1],a[2],b[3]};break;}
        case 9:{auto a=src(0),b=src(1);for(unsigned i=0;i<4;++i)vr[i]=std::min(a[i],b[i]);break;}
        case 10:{auto a=src(0),b=src(1);for(unsigned i=0;i<4;++i)vr[i]=std::max(a[i],b[i]);break;}
        case 11:case 12:case 16:case 17:case 18:case 19:case 20:case 21:{
            auto a=src(0),b=src(1);
            for(unsigned i=0;i<4;++i){bool t=false;
                switch(vec){case 11:t=a[i]<b[i];break;case 12:t=a[i]>=b[i];break;case 16:t=a[i]==b[i];break;case 17:t=false;break;
                case 18:t=a[i]>b[i];break;case 19:t=a[i]<=b[i];break;case 20:t=a[i]!=b[i];break;case 21:t=true;break;}
                vr[i]=t?1.f:0.f;}
            break;}
        case 13:vr=src(0);break;
        case 25:if(!state)throw std::runtime_error("RSX VP texture fetch requires guest memory");else vr=sample_vertex_texture(*state,(d[2]>>8)&3,src(0));break;
        case 14:{auto a=src(0);for(unsigned i=0;i<4;++i)vr[i]=a[i]-std::floor(a[i]);break;}
        case 15:{auto a=src(0);for(unsigned i=0;i<4;++i)vr[i]=std::floor(a[i]);break;}
        case 22:{auto a=src(0);for(unsigned i=0;i<4;++i)vr[i]=a[i]>0?1.f:a[i]<0?-1.f:0.f;break;}
        default:throw std::runtime_error("unsupported RSX VP vector opcode "+std::to_string(vec));
        }
        if(vec_write)write(vr,false);
        Vec4 sr{};bool sca_write=true;
        switch(sca){
        case 0:sca_write=false;break;
        case 1:sr=src(2);break;
        case 2:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=1.f/a[i];break;}
        case 3:{auto a=src(2);for(unsigned i=0;i<4;++i){float r=1.f/a[i];sr[i]=std::copysign(std::min(std::max(std::fabs(r),5.42101e-20f),1.884467e19f),r);}break;}
        case 4:{auto a=src(2);float t=1.f/std::sqrt(std::fabs(a[0]));sr={t,t,t,t};break;}
        case 5:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::exp(a[i]);break;}
        case 6:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::log(a[i]);break;}
        case 7:sr=lit_value(src(2));break;
        case 8:case 9:case 10:case 12:case 17:case 18:sca_write=false;break;
        case 11:throw std::runtime_error("RSX VP CLI has no verified semantics");
        case 13:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::log2(std::fabs(a[i]));break;}
        case 14:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::exp2(a[i]);break;}
        case 15:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::sin(a[i]);break;}
        case 16:{auto a=src(2);for(unsigned i=0;i<4;++i)sr[i]=std::cos(a[i]);break;}
        default:throw std::runtime_error("unsupported RSX VP scalar opcode "+std::to_string(sca));
        }
        if(sca_write)write(sr,true);
        const unsigned immediate=((d[0]>>23)&1)<<9|((d[2]&63)<<3)|(d[3]>>29);
        const bool bool_branch=bool((Reg{gpu}.get(0x1ff8,0)>>((d[3]>>23)&31))&1)==bool((d[3]>>28)&1);
        if((sca==8 || sca==9 || sca==10 || sca==11 || sca==12 || sca==17 || sca==18) &&
           ((sca==17 || sca==18)?bool_branch:branch_pass)){
            if(sca==12){if(call_stack.empty())return out;pc=call_stack.back();call_stack.pop_back();continue;}
            if(sca==10 || sca==11 || sca==18){
                if(call_stack.size()>=16)throw std::runtime_error("RSX VP call stack overflow");
                call_stack.push_back(pc+1);
            }
            const int target=sca==8?address[(d[0]>>24)&1][d[0]&3]:int(immediate);
            if(target<0 || target>=544)throw std::runtime_error("RSX VP branch target out of range");
            pc=unsigned(target);continue;
        }
        if(d[3]&1)return out;
        ++pc;
    }
}

// ---------------- Vertex fetch ----------------
static Vec4 register_attribute(const RsxDrawState::Immediate& value){
    Vec4 v{0,0,0,1};
    if(value.type==1){for(unsigned i=0;i<4;++i)v[i]=float((value.words[0]>>(24-8*i))&255)/255.f;}
    else if(value.type==2 || value.type==3){
        for(unsigned i=0;i<value.count;++i){const auto raw=std::int16_t(value.words[i/2]>>(16*(i%2)));
            v[i]=value.type==3?std::max(-1.f,float(raw)/32767.f):float(raw);}
    }else for(unsigned i=0;i<value.count;++i)v[i]=bits_float(value.words[i]);
    return v;
}
template<class State> static Vec4 fetch_attribute(State& s,const Reg& reg,unsigned attr,std::uint32_t index){
    if(host_stats().enabled)++host_stats().cpu_attribute;
    auto& gpu=s.rsx;auto& m=s.memory;
    const auto format=reg(0x1740+4*attr);
    const unsigned type=format&15,size=(format>>4)&15,stride=(format>>8)&255,frequency=format>>16;
    Vec4 v{0,0,0,1};
    if(!size){
        // Disabled array: the register attribute value (SET_VERTEX_DATA*_M).
        return register_attribute(gpu.draw.immediate[attr]);
    }
    // Zero/one frequency preserves the ordinary stream used by libgcm reset.
    // Higher divisors are applied to the original guest vertex index.
    if(frequency>1)index=(reg.get(0x1fc0,0)&(1u<<attr))?index%frequency:index/frequency;
    if(size>4)throw std::runtime_error("invalid RSX vertex attribute size");
    const auto offset=reg(0x1680+4*attr);
    const unsigned width=type==2?4:(type==1 || type==3 || type==5)?2:type==6?4:1;
    const unsigned bytes=type==6?4:width*size;
    const std::uint64_t at=std::uint64_t(offset&0x7fffffff)+reg.get(0x1738,0)+std::uint64_t(stride)*index;
    if(at>0xffffffffu)throw std::runtime_error("RSX vertex address overflow");
    const auto address=location_address(gpu,offset>>31,static_cast<std::uint32_t>(at),bytes);
    if(type==6){
        const auto w=static_cast<std::uint32_t>(m.load(address,4));
        auto sx=[](std::uint32_t x,unsigned n){const std::int32_t t=std::int32_t(x<<(32-n))>>(32-n);return std::max(-1.f,float(t)/float((1<<(n-1))-1));};
        v={sx(w&2047,11),sx((w>>11)&2047,11),sx(w>>22,10),1.f};return v;
    }
    for(unsigned i=0;i<size;++i){
        const auto raw=static_cast<std::uint32_t>(m.load(address+i*width,width));
        switch(type){
        case 1:v[i]=std::max(-1.f,float(std::int16_t(raw))/32767.f);break;
        case 2:v[i]=bits_float(raw);break;
        case 3:v[i]=half_float(static_cast<std::uint16_t>(raw));break;
        case 4:v[i]=float(raw)/255.f;break;
        case 5:v[i]=float(std::int16_t(raw));break;
        case 7:v[i]=float(raw);break;
        default:throw std::runtime_error("unsupported RSX vertex attribute type "+std::to_string(type));
        }
    }
    return v;
}

// ---------------- Textures ----------------
''' + TEX_CPP + r'''

// ---------------- Fragment program ----------------
''' + rsx_fpdecode.TYPES + r'''
struct FpInst { std::uint32_t d,s0,s1,s2; Vec4 constant; bool has_constant=false; unsigned word_offset=UINT32_MAX; std::array<FpSourcePlan,3> sources{};bool decoded=false,program_prepared=false;unsigned fast_opcode=0;std::shared_ptr<const FpFlowPlan> flow{}; };
''' + rsx_fpdecode.PREPARE + r'''
// The program is a pure function of its microcode bytes. Cache the exact
// byte image per SET_SHADER_PROGRAM value; a hit re-reads exactly those bytes
// (same 1 MiB IO-page split, same permission/observer path) in one bulk
// read and compares them. Any difference or ADMISSION fault (an unmapped or
// unreadable byte, nothing was notified) runs the original 16-byte block
// parser, so errors and results are unchanged; any other failure of an
// admitted read (an observer or coherence error) propagates and is never
// replayed through the parser.
// Returns false only for a bulk read that failed its admission.
static bool read_fragment_bytes(PpuState& s,unsigned location,std::uint32_t at,std::uint8_t* out,unsigned n,bool bulk=false){
    unsigned done=0;
    while(done<n){
        const unsigned count=std::min<unsigned>(n-done,0x100000-((at+done)&0xfffff));
        // A translation fault is deterministic and notifies nothing: the bulk check reports it as "not the same" and the parser raises it again.
        std::uint64_t address=0;
        try{address=location_address(s.rsx,location-1,at+done,count);}catch(const std::exception&){if(bulk)return false;throw;}
        // A cache check uses the admitted, permission-checked transfer of the
        // same bytes without building a Range; the parser keeps Range::read.
        if(bulk){
            try{s.memory.transfer(address,out+done,count,false);}
            catch(const std::exception&){if(s.memory.readable_span(address,count))throw;return false;}
        }
        else{auto mapping=s.memory.range(address,count,false);mapping.read(0,out+done,count);}
        done+=count;
    }
    return true;
}
static std::shared_ptr<const std::vector<FpInst>> load_fragment_program_shared(PpuState& s,const Reg& reg){
    const auto value=reg(0x8e4);const unsigned location=(value&3);
    if(location!=1 && location!=2)throw std::runtime_error("invalid RSX fragment program location");
    struct Entry {std::vector<std::uint8_t> bytes;std::shared_ptr<const std::vector<FpInst>> program;std::uint64_t use;};
    static thread_local std::unordered_map<std::uint32_t,Entry> cache;static thread_local std::uint64_t clock=0;
    // Programs are a pure function of their microcode bytes (word offsets are relative to the start): a new SET_SHADER_PROGRAM
    // value whose bytes equal an earlier program shares its decoded/prepared result.
    struct Content {std::shared_ptr<const std::vector<FpInst>> program;std::uint64_t use;};
    static thread_local std::unordered_map<std::string,Content> by_content;
    static thread_local std::vector<std::uint8_t> scratch;
    // O(n) once per n/8 insertions instead of a full min_element scan per insertion: drop the least recently used eighth.
    auto trim=[](auto& map){
        if(map.size()<1024)return;
        std::vector<std::uint64_t> uses;uses.reserve(map.size());for(const auto& item:map)uses.push_back(item.second.use);
        const auto cut=uses.begin()+uses.size()/8;std::nth_element(uses.begin(),cut,uses.end());const auto limit=*cut;
        for(auto it=map.begin();it!=map.end();)it=it->second.use<limit?map.erase(it):std::next(it);
    };
    const std::uint32_t start=value&~3u;
    if(auto found=cache.find(value);found!=cache.end()){auto& entry=found->second;
        scratch.resize(entry.bytes.size());
        const bool same=read_fragment_bytes(s,location,start,scratch.data(),unsigned(scratch.size()),true) && !std::memcmp(scratch.data(),entry.bytes.data(),scratch.size());
        if(same){entry.use=++clock;::RsxPerfCounters::add(::RsxPerfCounters::fp_hits);return entry.program;}
    }
    ::RsxPerfCounters::add(::RsxPerfCounters::fp_misses);
    std::vector<FpInst> program;std::vector<std::uint8_t> image;
    std::uint32_t offset=start;
    // The extent of an unknown program is only discovered by decoding: the original parser reads one 16-byte block at a time
    // (an instruction, plus one constant block when an operand selects it) until END, and every observer (surface readback,
    // label synchronization) sees exactly those spans. A read-ahead chunk would also touch bytes behind END, so every block is
    // read on its own, as ONE admitted 16-byte transfer inside one 1 MiB IO page (no Range): the transfer validates the complete
    // block before any observer sees a byte, so a block that is half readable notifies nothing, exactly like Range. A block
    // straddling an IO page edge and a block whose admission faults take the Range path, which raises the original error at the
    // original block; a failure of an admitted block (an observer error, also for a block split over two mappings) propagates and is
    // never replayed.
    auto block=[&](std::uint32_t at){
        std::array<std::uint8_t,16> bytes{};bool loaded=false;
        if(((at&0xfffff)+16)<=0x100000){
            const auto address=location_address(s.rsx,location-1,at,16);   // the Range path below would raise the same error
            try{s.memory.transfer(address,bytes.data(),16,false);loaded=true;}
            catch(const std::exception&){if(s.memory.readable_span(address,16))throw;}
        }
        if(!loaded)read_fragment_bytes(s,location,at,bytes.data(),16);
        image.insert(image.end(),bytes.begin(),bytes.end());
        std::array<unsigned,4> words{};
        for(unsigned i=0;i<4;++i){const auto w=read_be(bytes.data()+4*i,4);words[i]=(w<<16)|(w>>16);}return words;
    };
    for(unsigned n=0;n<4096;++n){
        const auto words=block(offset);FpInst inst{words[0],words[1],words[2],words[3],{},false};
        inst.word_offset=(offset-start)/4;
        offset+=16;
        const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
        if(opcode<64 && (((inst.s0&3)==2) || ((inst.s1&3)==2) || ((inst.s2&3)==2))){
            const auto constants=block(offset);for(unsigned i=0;i<4;++i)inst.constant[i]=bits_float(constants[i]);
            inst.has_constant=true;offset+=16;
        }
        program.push_back(inst);
        if(inst.d&1){
            const std::string key(reinterpret_cast<const char*>(image.data()),image.size());
            std::shared_ptr<const std::vector<FpInst>> result;
            if(auto same=by_content.find(key);same!=by_content.end()){result=same->second.program;same->second.use=++clock;}
            else{result=std::make_shared<const std::vector<FpInst>>(prepared_fragment_program(std::move(program)));
                trim(by_content);by_content[key]=Content{result,++clock};}
            if(!cache.count(value))trim(cache);
            cache[value]=Entry{std::move(image),result,++clock};
            return result;
        }
    }
    throw std::runtime_error("RSX fragment program has no end");
}
[[maybe_unused]] static std::vector<FpInst> load_fragment_program(PpuState& s,const Reg& reg){return *load_fragment_program_shared(s,reg);}
struct FpContext {
    std::array<std::array<std::uint32_t,4>,48> r{}; // raw R registers; H registers alias halves
    std::array<Vec4,15> inputs{};
    std::array<Vec4,2> cc{};
    bool killed=false;
    const PpuState* state=nullptr;std::array<Texture,16>* textures=nullptr;
    Vec4 read_r(unsigned i)const{Vec4 v;for(unsigned c=0;c<4;++c)v[c]=bits_float(r.at(i)[c]);return v;}
    Vec4 read_h(unsigned i)const{
        const auto& reg=r.at(i/2);const unsigned base=(i&1)*2;Vec4 v;
        for(unsigned c=0;c<4;++c){const auto word=reg[base+c/2];v[c]=half_float(static_cast<std::uint16_t>(c&1?word>>16:word));}
        return v;
    }
    void write(unsigned i,bool half,unsigned mask,const Vec4& v){
        if(!half){for(unsigned c=0;c<4;++c)if(mask&(1u<<c)){std::uint32_t b;std::memcpy(&b,&v[c],4);r.at(i)[c]=b;}return;}
        auto& reg=r.at(i/2);const unsigned base=(i&1)*2;
        for(unsigned c=0;c<4;++c)if(mask&(1u<<c)){
            auto& word=reg[base+c/2];const std::uint32_t h=float_half(v[c]);
            word=c&1?(word&0xffff)|(h<<16):(word&0xffff0000)|h;
        }
    }
};
static Vec4 fp_source_raw(const FpInst& inst,unsigned n,const FpContext& ctx){
    const auto raw=n==0?inst.s0:n==1?inst.s1:inst.s2;
    const unsigned type=raw&3,index=(raw>>2)&63;const bool half=(raw>>8)&1;
    Vec4 v{};
    switch(type){
    case 0:v=half?ctx.read_h(index):ctx.read_r(index);break;
    case 1:{if(inst.s2&(1u<<30))throw std::runtime_error("RSX FP indexed input is unsupported");const unsigned attr=(inst.d>>13)&15;if(attr>14)throw std::runtime_error("invalid RSX FP input attribute");v=ctx.inputs[attr];break;}
    case 2:v=inst.constant;break;
    default:throw std::runtime_error("invalid RSX FP source register type");
    }
    Vec4 sw;for(unsigned i=0;i<4;++i)sw[i]=v[(raw>>(9+2*i))&3];
    const unsigned precision=(inst.s1>>(19+3*n))&7;
    if(precision>5)throw std::runtime_error("unsupported RSX FP source precision modifier");
    auto clamp_precision=[&](float x){
        if(precision==0 || precision==5 || (precision==1 && half && type==0))return x;
        const float low=precision==4?0.f:precision==1?-65504.f:precision==2?-2.f:-1.f;
        const float high=precision==1?65504.f:precision==2?2.f:1.f;
        return std::min(high,std::max(low,x));
    };
    if(precision==4)for(auto& x:sw)x=clamp_precision(x);
    const bool absolute=n==0?((raw>>29)&1):((raw>>18)&1);
    if(absolute)for(auto& x:sw)x=std::fabs(x);
    if(precision!=4)for(auto& x:sw)x=clamp_precision(x);
    if((raw>>17)&1)for(auto& x:sw)x=-x;
    return sw;
}
''' + rsx_fpdecode.SOURCE + rsx_fpdecode.FAST + r'''
// Executes one 2x2 quad in lockstep (lane 0..3 = (x,y),(x+1,y),(x,y+1),(x+1,y+1)).
// Killed lanes keep running as helpers so coarse derivatives stay defined.
static void run_fragment_quad_linear_range(const FpInst* instructions,std::size_t count,std::array<FpContext,4>& quad,unsigned active=15){
    for(std::size_t pc=0;pc<count;++pc){
        const auto& inst=instructions[pc];
        if(perf_work)perf_work->fp_instructions+=4;
        const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
        if(opcode==0 || opcode==61 || opcode==62){if(inst.d&1)return;continue;}
        const unsigned exec=(inst.s0>>18)&7; // bit0 lt, bit1 eq, bit2 gt
        if(!exec){if(inst.d&1)return;continue;}
        if(active==15 && fast_fragment_instruction(inst,quad)){if(inst.d&1)return;continue;}
        std::array<Vec4,4> result{};std::array<bool,4> texture_kill{}; // alpha kill (rsx_tex.py, RSXTEX3)
        if(opcode==21 || opcode==22){
            std::array<Vec4,4> x;for(unsigned l=0;l<4;++l)x[l]=fp_source(inst,0,quad[l]);
            for(unsigned l=0;l<4;++l)for(unsigned i=0;i<4;++i)
                result[l][i]=opcode==21?x[1][i]-x[0][i]:x[2][i]-x[0][i];
        }else if(opcode==23 || opcode==24 || opcode==25 || opcode==47 || opcode==49){
            if(!quad[0].textures)throw std::runtime_error("RSX FP texture fetch without bound textures");
            const unsigned unit=(inst.d>>17)&15;auto& t=(*quad[0].textures)[unit];
            if(!t.valid)throw std::runtime_error("RSX FP samples undecoded texture unit");
            std::array<Vec4,4> x;
            for(unsigned l=0;l<4;++l){x[l]=fp_source(inst,0,quad[l]);if(opcode==24){x[l][0]/=x[l][3];x[l][1]/=x[l][3];x[l][2]/=x[l][3];}}
            // Equal non-mip min/mag filters cannot observe LOD. Retain all
            // source reads (TXD errors), and cubemap validation in the old order.
            const bool needs_lod=t.cube || t.min!=t.mag || t.min>2;
            float lambda=-std::numeric_limits<float>::infinity();
            if(needs_lod && (t.cube || (opcode!=25 && opcode!=47))){
                Vec4 ddx{},ddy{};for(unsigned c=0;c<4;++c){ddx[c]=x[1][c]-x[0][c];ddy[c]=x[2][c]-x[0][c];}
                const float rho=std::max(texture_gradient_length(t,x[0],ddx),texture_gradient_length(t,x[0],ddy));
                lambda=rho>0?std::log2(rho):-std::numeric_limits<float>::infinity();
            }
            for(unsigned l=0;l<4;++l){
                float lod=lambda;
                if(opcode==25){
                    const auto dx=fp_source(inst,1,quad[l]),dy=fp_source(inst,2,quad[l]);
                    if(needs_lod){
                        const float rx=texture_gradient_length(t,x[l],dx),ry=texture_gradient_length(t,x[l],dy);
                        const float r=std::max(rx,ry);lod=r>0?std::log2(r):-std::numeric_limits<float>::infinity();
                    }
                }else if(opcode==47)lod=x[l][3];
                else if(opcode==49)lod+=x[l][3];
                result[l]=sample_texture(t,x[l][0],x[l][1],lod,x[l][2],x[l][2],&texture_kill[l]);
                if((inst.d>>21)&1)for(auto& c:result[l])c=2*c-1; // _bx2
            }
        }else for(unsigned l=0;l<4;++l){
            auto& ctx=quad[l];
            auto a=[&]{return fp_source(inst,0,ctx);};
            auto b=[&]{return fp_source(inst,1,ctx);};
            auto c=[&]{return fp_source(inst,2,ctx);};
            Vec4& v=result[l];
            switch(opcode){
            case 1:v=a();break;
            case 2:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=x[i]*y[i];break;}
            case 3:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=x[i]+y[i];break;}
            case 4:{auto x=a(),y=b(),z=c();for(unsigned i=0;i<4;++i)v[i]=std::fma(x[i],y[i],z[i]);break;}
            case 5:{auto x=a(),y=b();float t=x[0]*y[0]+x[1]*y[1]+x[2]*y[2];v={t,t,t,t};break;}
            case 6:{auto x=a(),y=b();float t=x[0]*y[0]+x[1]*y[1]+x[2]*y[2]+x[3]*y[3];v={t,t,t,t};break;}
            case 7:{auto x=a(),y=b();v={1,x[1]*y[1],x[2],y[3]};break;}
            case 8:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=std::min(x[i],y[i]);break;}
            case 9:{auto x=a(),y=b();for(unsigned i=0;i<4;++i)v[i]=std::max(x[i],y[i]);break;}
            case 10:case 11:case 12:case 13:case 14:case 15:{
                auto x=a(),y=b();
                for(unsigned i=0;i<4;++i){bool t=false;
                    switch(opcode){case 10:t=x[i]<y[i];break;case 11:t=x[i]>=y[i];break;case 12:t=x[i]<=y[i];break;
                    case 13:t=x[i]>y[i];break;case 14:t=x[i]!=y[i];break;case 15:t=x[i]==y[i];break;}
                    v[i]=t?1.f:0.f;}
                break;}
            case 16:{auto x=a();for(unsigned i=0;i<4;++i)v[i]=x[i]-std::floor(x[i]);break;}
            case 17:{auto x=a();for(unsigned i=0;i<4;++i)v[i]=std::floor(x[i]);break;}
            case 18:break;
            case 19:case 36:case 39:case 41:case 44:{
                auto x=a();unsigned packed=0;
                if(opcode==44)for(auto& c:x)c=std::clamp(c<.0031308f?12.92f*c:1.055f*std::pow(c,1.f/2.4f)-.055f,0.f,1.f);
                if(opcode==36)packed=float_half(x[0])|(unsigned(float_half(x[1]))<<16);
                else{
                    const unsigned count=opcode==41?2:4,bits=opcode==41?16:8;
                    for(unsigned i=0;i<count;++i){
                        const int value=opcode==19?int(std::lround(std::clamp(x[i],-1.f,1.f)*127.f)):
                            int(std::lround(std::clamp(x[i],0.f,1.f)*(bits==16?65535.f:255.f)));
                        packed|=(unsigned(value)&(bits==16?65535:255))<<(bits*i);
                    }
                }
                const float f=bits_float(packed);v={f,f,f,f};break;
            }
            case 20:case 37:case 40:case 42:case 45:{
                const unsigned packed=float_bits(a()[0]);
                for(unsigned i=0;i<4;++i){
                    if(opcode==37)v[i]=half_float(packed>>(16*(i&1)));
                    else if(opcode==42)v[i]=((packed>>(16*(i&1)))&65535)/65535.f;
                    else if(opcode==20)v[i]=std::max(-1.f,float(std::int8_t(packed>>(8*i)))/127.f);
                    else{v[i]=((packed>>(8*i))&255)/255.f;if(opcode==45)v[i]=v[i]<=.04045f?v[i]/12.92f:std::pow((v[i]+.055f)/1.055f,2.4f);}
                }break;
            }
            case 26:{float t=1.f/a()[0];v={t,t,t,t};break;}
            case 27:{float t=1.f/std::sqrt(std::fabs(a()[0]));v={t,t,t,t};break;}
            case 28:{float t=std::exp2(a()[0]);v={t,t,t,t};break;}
            case 29:{float t=std::log2(a()[0]);v={t,t,t,t};break;}
            case 30:v=lit_value(a());break;
            case 32:v={1,1,1,1};break;
            case 33:break;
            case 31:{auto x=a(),y=b(),z=c();for(unsigned i=0;i<4;++i)v[i]=z[i]+x[i]*(y[i]-z[i]);break;}
            case 34:{float t=std::cos(a()[0]);v={t,t,t,t};break;}
            case 35:{float t=std::sin(a()[0]);v={t,t,t,t};break;}
            case 38:{float t=std::pow(a()[0],b()[0]);v={t,t,t,t};break;}
            case 43:{auto x=a(),y=b(),z=c();v={x[0]+y[0]*z[0]+y[1]*z[1],x[1]+y[0]*z[2]+y[1]*z[3],x[0]+y[0]*z[0]+y[1]*z[1],x[1]+y[0]*z[2]+y[1]*z[3]};break;}
            case 46:{auto x=a(),y=b(),z=c();const float t=x[0]*y[0]+x[1]*y[1]+z[0];v={t,t,t,t};break;}
            case 54:{auto x=a(),y=b();float dot=0;for(unsigned i=0;i<4;++i)dot+=x[i]*y[i];for(unsigned i=0;i<4;++i)v[i]=x[i]-2*dot*y[i];break;}
            case 57:{auto x=a();const float n=std::sqrt(x[0]*x[0]+x[1]*x[1]+x[2]*x[2]);v={x[0]/n,x[1]/n,x[2]/n,x[2]/n};break;}
            case 59:{auto x=a();const float den=std::sqrt(b()[0]);for(unsigned i=0;i<4;++i)v[i]=std::fabs(x[i])>0?x[i]/den:x[i];break;}
            case 60:{auto x=a();v={1,x[1],x[1]>0?std::exp2(x[3]):0.f,1};break;}
            case 56:{auto x=a(),y=b();float t=x[0]*y[0]+x[1]*y[1];v={t,t,t,t};break;}
            case 58:{auto x=a();float y=b()[0];for(unsigned i=0;i<4;++i)v[i]=x[i]/y;break;}
            default:throw std::runtime_error("unsupported RSX FP opcode "+std::to_string(opcode));
            }
        }
        for(unsigned l=0;l<4;++l){
            if(!(active&(1u<<l)))continue;
            auto& ctx=quad[l];Vec4 v=result[l];
            std::array<bool,4> pass{true,true,true,true};
            if(exec!=7){
                const auto& cc=ctx.cc[(inst.s0>>31)&1];
                for(unsigned i=0;i<4;++i){
                    const float x=cc[(inst.s0>>(21+2*i))&3];
                    pass[i]=(x<0 && (exec&1)) || (x==0 && (exec&2)) || (x>0 && (exec&4));
                }
            }
            if(texture_kill[l] && (pass[0]||pass[1]||pass[2]||pass[3]))ctx.killed=true;
            if(opcode==18){if(pass[0]||pass[1]||pass[2]||pass[3])ctx.killed=true;continue;}
            static const float scales[8]={1,2,4,8,1,0.5f,0.25f,0.125f};
            const unsigned scale=(inst.s1>>28)&7;
            if(scale==4)throw std::runtime_error("invalid RSX FP result scale");
            if(scale)for(auto& x:v)x*=scales[scale];
            const bool half_dest=(inst.d>>7)&1,no_dest=(inst.d>>30)&1;
            if(inst.d>>31)for(auto& x:v)x=std::min(1.f,std::max(0.f,x));
            else if(!no_dest){
                // Precision clamp, with RPCS3's exempt opcodes (NRM MAX MIN COS SIN REFL FRC LIT LIF LG2).
                const bool exempt=opcode==57||opcode==9||opcode==8||opcode==34||opcode==35||opcode==54||opcode==16||opcode==30||opcode==60||opcode==29||
                    (opcode==1 && half_dest && ((inst.s0>>8)&1) && (inst.s0&3)==0);
                const unsigned prec=(inst.d>>22)&3;
                if(!exempt && !(prec==1 && !half_dest)){
                    const float limit=prec==2?2.f:prec==3?1.f:prec==1?65504.f:0.f;
                    if(limit)for(auto& x:v)x=std::min(limit,std::max(-limit,x));
                }
            }
            unsigned mask=(inst.d>>9)&15;
            for(unsigned i=0;i<4;++i)if(!pass[i])mask&=~(1u<<i);
            const unsigned dest=(inst.d>>1)&63;
            if(!no_dest)ctx.write(dest,half_dest,mask,v);
            if((inst.d>>8)&1){
                // CC receives the value as stored in the destination (rounded for H).
                const Vec4 stored=no_dest?v:half_dest?ctx.read_h(dest):ctx.read_r(dest);
                auto& cc=ctx.cc[(inst.s0>>30)&1];
                for(unsigned i=0;i<4;++i)if(mask&(1u<<i))cc[i]=stored[i];
            }
        }
        if(inst.d&1)return;
    }
}
static void run_fragment_quad_linear(const std::vector<FpInst>& program,std::array<FpContext,4>& quad,unsigned active=15){
    run_fragment_quad_linear_range(program.data(),program.size(),quad,active);
}
// Structured flow control uses the same quad ALU, with lane write masks.
// Addresses count 32-bit words, including inline constants (FPToCFG.cpp:203-242).
// RPCS3's current CFG emits rep_count iterations for LOOP/REP; older interpreter
// uses a different end/start interpretation. This follows the current CFG path.
static void run_fragment_quad(const std::vector<FpInst>& program,std::array<FpContext,4>& quad){
    PerfTimer timer(perf_work?&perf_work->fp_ns:nullptr);
    const bool prepared=!program.empty() && program[0].program_prepared;
    const bool flow=prepared?bool(program[0].flow):std::any_of(program.begin(),program.end(),[](const FpInst& i){return (i.s1>>31)!=0;});
    if(!flow){run_fragment_quad_linear(program,quad);return;}
    FpFlowPlan scratch;
    if(!prepared){
        unsigned words=0;
        for(unsigned i=0;i<program.size();++i){
            const auto& inst=program[i];const unsigned at=inst.word_offset==UINT32_MAX?words:inst.word_offset;
            if(!scratch.offsets.emplace(at,i).second)scratch.duplicate=true;
            words=at+4+(inst.has_constant?4:0);
        }
        scratch.offsets.emplace(words,unsigned(program.size()));
    }
    const auto& plan=prepared?*program[0].flow:scratch;
    if(plan.duplicate)throw std::runtime_error("duplicate RSX FP instruction address");
    const auto& offsets=plan.offsets;
    auto target=[&](unsigned word){auto it=offsets.find(word);if(it==offsets.end())throw std::runtime_error("RSX FP control target is not an instruction boundary");return it->second;};
    auto predicate=[&](const FpInst& inst,unsigned active){
        unsigned result=0;const unsigned test=(inst.s0>>18)&7;
        for(unsigned lane=0;lane<4;++lane)if(active&(1u<<lane))for(unsigned c=0;c<4;++c){
            const float x=quad[lane].cc[inst.s0>>31][(inst.s0>>(21+2*c))&3];
            if(test==7 || ((test&1)&&x<0) || ((test&2)&&x==0) || ((test&4)&&x>0))result|=1u<<lane;
        }
        return result;
    };
    unsigned returned=0,budget=1048576;
    auto execute=[&](auto&& self,unsigned begin,unsigned end,unsigned lanes,unsigned& broken,unsigned depth)->void{
        if(depth>32)throw std::runtime_error("RSX FP flow nesting limit exhausted");
        for(unsigned pc=begin;pc<end;++pc){
            const unsigned active=lanes&~(returned|broken);
            if(!active)return;
            if(!budget--)throw std::runtime_error("RSX FP execution budget exhausted");
            const auto& inst=program[pc];const unsigned op=((inst.d>>24)&63)|((inst.s1>>31)<<6);
            if(op>=64){
                const auto taken=predicate(inst,active);
                switch(op){
                case 64:if(!depth)throw std::runtime_error("RSX FP BRK outside loop");broken|=taken;break;
                case 65:throw std::runtime_error("RSX FP CAL has no verified RSX encoding/semantics");
                case 66:{
                    const unsigned other=target(inst.s1&0x7fffffff),after=target(inst.s2);
                    if(other<pc+1 || after<other || after>end)throw std::runtime_error("invalid RSX FP IF range");
                    self(self,pc+1,other,taken,broken,depth);
                    self(self,other,after,active&~taken,broken,depth);pc=after-1;break;
                }
                case 67:case 68:{
                    const unsigned after=target(inst.s2),count=(inst.s1>>2)&255;
                    if(after<pc+1 || after>end)throw std::runtime_error("invalid RSX FP loop range");
                    unsigned live=taken,stopped=0;
                    for(unsigned iteration=0;iteration<count && (live&~returned);++iteration){
                        self(self,pc+1,after,live,stopped,depth+1);live&=~stopped;
                    }
                    pc=after-1;break;
                }
                case 69:returned|=taken;break;
                default:throw std::runtime_error("unsupported RSX FP flow opcode "+std::to_string(op));
                }
            }else{
                if(active!=15 && (op==21 || op==22 || op==23 || op==24 || op==49))
                    throw std::runtime_error("RSX derivatives/implicit texture LOD in divergent flow are unsupported");
                if(op!=61 && op!=62)run_fragment_quad_linear_range(&inst,1,quad,active);
                // FENCT/FENCB only serialize synchronous texture/ALU units.
            }
            if(inst.d&1){returned|=active;return;}
        }
    };
    unsigned broken=0;execute(execute,0,unsigned(program.size()),15,broken,0);
    if(broken)throw std::runtime_error("RSX FP BRK outside loop");
}
// Single-pixel form: four identical lanes, so derivatives are zero.
[[maybe_unused]] static void run_fragment_program(const std::vector<FpInst>& program,FpContext& ctx){
    std::array<FpContext,4> quad{ctx,ctx,ctx,ctx};
    run_fragment_quad(program,quad);
    ctx=quad[0];
}

// ---------------- Surfaces ----------------
struct Target {
    unsigned bpp=4,swizzle_width=0,swizzle_height=0;
    mutable std::array<std::uint8_t,16> wide{};
    std::uint32_t offset=0,pitch=0;const NativeRsx::Tile* tile=nullptr;std::uint8_t* base=nullptr;std::uint32_t base_offset=0;
    bool observed=false;
    std::shared_ptr<GuestMemory::Range> mapping;
    mutable bool split_pixel=false;
    // The permutation preserves the low five address bits. Cache one 32-byte
    // run per quad row, lazily: malformed tiles must still fault at the first
    // sample that the original rasterizer would have accessed.
    struct Run { std::uint32_t linear=UINT32_MAX,physical=0; };
    mutable std::array<Run,2> runs{};
    mutable RsxTileAddress tile_address;
    std::uint8_t* physical(unsigned offset,unsigned count)const{
        if(!mapping)return base+offset;
        return mapping->pointer(offset,count);
    }
    unsigned address(unsigned x,unsigned y,unsigned byte=0)const{
        if(swizzle_width)return static_cast<unsigned>(swizzle_byte(swizzle_width,swizzle_height,x,y,bpp,byte));
        const auto linear=offset+std::uint64_t(y)*pitch+x*bpp;
        if(!tile)return static_cast<std::uint32_t>(linear)-base_offset+byte;
        const auto relative=static_cast<std::uint32_t>(linear-tile->offset),group=relative&~31u;
        if(relative>=tile->size)throw std::runtime_error("invalid RSX tile address configuration");
        const auto previous=tile_address.key;const bool known=tile_address.valid;
        tile_address.refresh(*tile);
        if(!known || previous!=tile_address.key)runs={};
        auto& run=runs[y&1];
        if(run.linear==group)return run.physical+(relative&31)-base_offset+byte;
        const auto physical=tile_address.address(*tile,relative);
        const auto first=physical-(relative&31);
        if(std::uint64_t(group)+32<=tile->size && first>=tile->offset &&
           std::uint64_t(first)+32<=std::uint64_t(tile->offset)+tile->size)run={group,first};
        return physical-base_offset+byte;
    }
    std::uint8_t* at(unsigned x,unsigned y)const{
        split_pixel=false;
        if(swizzle_width && bpp>4){
            for(unsigned i=0;i<bpp;i+=4){const auto at=address(x,y,i);
                if(mapping)mapping->read(at,wide.data()+i,4);else std::copy_n(base+at,4,wide.data()+i);}
            return wide.data();
        }
        const auto at=address(x,y);auto* p=physical(at,bpp);
        if(p)return p;
        mapping->read(at,wide.data(),bpp);split_pixel=true;return wide.data();
    }
    void commit(unsigned x,unsigned y)const{
        if(swizzle_width && bpp>4){
            for(unsigned i=0;i<bpp;i+=4){const auto at=address(x,y,i);
                if(mapping)mapping->write(at,wide.data()+i,4,false);else std::copy_n(wide.data()+i,4,base+at);}
        }else if(split_pixel)mapping->write(address(x,y),wide.data(),bpp,observed);
    }
    void mark(GuestMemory& memory,unsigned x,unsigned y,std::uint8_t* p)const{
        if(!observed)return;
        if(swizzle_width && bpp>4){
            for(unsigned i=0;i<bpp;i+=4){const auto at=address(x,y,i);
                if(mapping)mapping->mark(at,4);else memory.mark_write(base+at,4);}
        }else if(!split_pixel)memory.mark_write(p,bpp);
    }

};
struct TargetCache {
    using Key=std::array<unsigned,9>;
    struct Hash {std::size_t operator()(const Key& key)const{std::size_t h=0;for(auto v:key)h=(h*16777619u)^v;return h;}};
    std::unordered_map<Key,Target,Hash> entries;
    // Rebuilt at each serial FIFO admission, where no guest reservation can
    // be created concurrently. Only keys are indexed; versions remain live.
    std::vector<std::uintptr_t> reservation_lines,reservation_inventory;
    std::size_t reservation_count=SIZE_MAX;
    void refresh_reservations(const GuestMemory& memory){
        // Compare exact keys, not just count: tests and map-owner replacement
        // can replace an inventory with the same number of reservations.
        if(reservation_count==memory.write_versions.size() && std::equal(memory.write_versions.begin(),memory.write_versions.end(),reservation_inventory.begin(),
            [](const auto& entry,auto line){return entry.first==line;}))return;
        reservation_inventory.clear();reservation_inventory.reserve(memory.write_versions.size());
        for(const auto& entry:memory.write_versions)reservation_inventory.push_back(entry.first);
        reservation_lines=reservation_inventory;std::sort(reservation_lines.begin(),reservation_lines.end());reservation_count=memory.write_versions.size();
    }
};
// Installed only for a bounded FIFO turn. Mapping/IO/tile changes belong to
// the coordinator outside this turn; direct test/HLE calls remain uncached.
static thread_local TargetCache* fifo_target_cache=nullptr;
template<class State> static Target bind_target(State& s,std::uint32_t context,std::uint32_t offset,std::uint32_t pitch,unsigned width,unsigned height,unsigned bpp=4,unsigned swizzle_width=0,unsigned swizzle_height=0){
    const TargetCache::Key key{context,offset,pitch,width,height,bpp,swizzle_width,swizzle_height,0};
    auto track=[&](Target& t){
        if(s.memory.concurrent()){t.observed=true;return;}
        // ObserverPause suppresses host callbacks. With no intersecting
        // reservation mark() has no side effects, but stale maps still stop.
        t.mapping->check_epoch();t.observed=false;
        if(fifo_target_cache && fifo_target_cache->reservation_count!=s.memory.write_versions.size())fifo_target_cache->refresh_reservations(s.memory);
        for(const auto& part:t.mapping->parts){
            const auto first=reinterpret_cast<std::uintptr_t>(part.data)/128;
            const auto last=(reinterpret_cast<std::uintptr_t>(part.data)+part.size-1)/128;
            if(fifo_target_cache){const auto& lines=fifo_target_cache->reservation_lines;auto it=std::lower_bound(lines.begin(),lines.end(),first);
                if(it!=lines.end() && *it<=last)t.observed=true;
            }else for(const auto& entry:s.memory.write_versions)if(entry.first>=first && entry.first<=last){t.observed=true;break;}
            if(t.observed)break;
        }
        if(t.observed)t.mapping->mark(0,t.mapping->size);
    };
    if(fifo_target_cache){auto hit=fifo_target_cache->entries.find(key);if(hit!=fifo_target_cache->entries.end()){
        GuestMemory::ObserverPause acquire_target(s.memory);auto t=hit->second;track(t);return t;}}
    auto& gpu=s.rsx;
    if(swizzle_width){width=swizzle_width;height=swizzle_height;pitch=width*bpp;}
    if(context!=0xfeed0000 && context!=0xfeed0001)throw std::runtime_error("unsupported RSX draw surface DMA context");
    const unsigned location=context==0xfeed0001?1:0;
    const std::uint64_t limit=location?0x10000000u:gpu.local_size;
    const std::uint64_t span=std::uint64_t(height-1)*pitch+std::uint64_t(width)*bpp;
    if(pitch<width*bpp || pitch%bpp || offset%bpp || offset>limit || span>limit-offset)
        throw std::runtime_error("RSX draw surface outside VRAM/IO aperture or invalid pitch");
    Target t;t.swizzle_width=swizzle_width;t.swizzle_height=swizzle_height;t.bpp=bpp;t.offset=offset;t.pitch=pitch;t.base_offset=offset;
    std::uint64_t bytes=span;
    for(const auto& tile:gpu.tiles)if(tile.bound && tile.location==location && offset<std::uint64_t(tile.offset)+tile.size && tile.offset<offset+span){
        if(swizzle_width || t.tile || offset<tile.offset || offset+span>std::uint64_t(tile.offset)+tile.size || tile.pitch!=pitch)
            throw std::runtime_error("unsupported RSX draw surface tile layout");
        t.tile=&tile;t.base_offset=tile.offset;bytes=tile.size;
    }
    const auto address=location_address(gpu,location,t.base_offset,static_cast<std::uint32_t>(bytes));
    GuestMemory::ObserverPause acquire_target(s.memory);
    t.mapping=std::make_shared<GuestMemory::Range>(s.memory.range(address,static_cast<std::uint32_t>(bytes),true));
    t.base=t.mapping->parts.front().data;
    // Preserve bind-time tracking above. Per-pixel marks cannot affect a token
    // outside this host allocation span; no new observers appear during draw.
    track(t);
    if(fifo_target_cache){if(fifo_target_cache->entries.size()>=64)fifo_target_cache->entries.clear();fifo_target_cache->entries.emplace(key,t);}
    return t;
}

struct ShadedVertex { Vec4 clip; std::array<Vec4,16> o; };
struct LineGeometry { float x,y,dx,dy;unsigned width;std::array<Vec4,16> provoking; };
// Clip in homogeneous coordinates, before perspective divide. Intersections
// interpolate every varying with the same homogeneous edge parameter.
// User distances: RPCS3 VertexInterpreter.glsl:617-622, o5.yzw/o6.yzw.
static std::vector<ShadedVertex> clip_polygon(const std::array<const ShadedVertex*,3>& vertices,
        unsigned user,unsigned output_mask,float sz,float vz,float near_z,float far_z){
    if(host_stats().enabled)++host_stats().cpu_clip;
    std::vector<ShadedVertex> poly{*vertices[0],*vertices[1],*vertices[2]};
    // A primitive with a non-finite position draws nothing (derived: RPCS3 ffba2bf0 VKVertexProgram.cpp:278,485 passes
    // the VP position to the host clipper; NVIDIA/llvmpipe discard such primitives; the game relies on it).
    for(const auto& v:poly)for(float c:v.clip)if(!std::isfinite(c))return {};
    const bool eye_crossing=std::any_of(poly.begin(),poly.end(),[](const ShadedVertex& v){return !(v.clip[3]>0);});
    for(unsigned plane=0;plane<12 && !poly.empty();++plane){
        if(plane<4 && !eye_crossing)continue;
        if(plane>=6){
            const unsigned i=plane-6,mode=(user>>(4*i))&15;
            if(mode>2)throw std::runtime_error("invalid RSX user clip plane mode");
            if(!mode || !(output_mask&(1u<<(6+i))))continue;
        }
        auto distance=[&](const ShadedVertex& v){
            const auto& c=v.clip;
            switch(plane){
            case 0:return c[3]+c[0];case 1:return c[3]-c[0];case 2:return c[3]+c[1];case 3:return c[3]-c[1];
            case 4:return c[2]*sz+(vz-near_z)*c[3];case 5:return (far_z-vz)*c[3]-c[2]*sz;
            default:{const unsigned i=plane-6;const float d=v.o[i<3?5:6][1+i%3];return ((user>>(4*i))&15)==1?-d:d;}
            }
        };
        bool all_inside=true;for(const auto& v:poly)all_inside&=distance(v)>=0;
        if(all_inside)continue; // preserve original vertices/FP order for the hot path
        std::vector<ShadedVertex> result;
        const ShadedVertex* prev=&poly.back();float dp=distance(*prev);
        for(const auto& cur:poly){
            const float dc=distance(cur);
            if((dp>=0)!=(dc>=0)){
                const float t=dp/(dp-dc);ShadedVertex v;
                for(unsigned c=0;c<4;++c)v.clip[c]=prev->clip[c]+t*(cur.clip[c]-prev->clip[c]);
                for(unsigned r=0;r<16;++r)for(unsigned c=0;c<4;++c)v.o[r][c]=prev->o[r][c]+t*(cur.o[r][c]-prev->o[r][c]);
                v.o[0]=v.clip;result.push_back(v);
            }
            if(dc>=0)result.push_back(cur);
            prev=&cur;dp=dc;
        }
        poly=std::move(result);
    }
    // A zero homogeneous vector can remain at the projective apex. It has
    // zero projected area and cannot be passed to a perspective divide.
    poly.erase(std::remove_if(poly.begin(),poly.end(),[](const ShadedVertex& v){return !(v.clip[3]>0);}),poly.end());
    return poly;
}
static bool compare(unsigned func,float a,float b){
    switch(func){
    case 0x200:return false;case 0x201:return a<b;case 0x202:return a==b;case 0x203:return a<=b;
    case 0x204:return a>b;case 0x205:return a!=b;case 0x206:return a>=b;case 0x207:return true;
    default:throw std::runtime_error("invalid RSX comparison function "+std::to_string(func));
    }
}
static float blend_factor(unsigned f,const Vec4& src,const Vec4& dst,const Vec4& constant,unsigned c){
    switch(f){
    case 0:return 0;case 1:return 1;
    case 0x300:return src[c];case 0x301:return 1-src[c];case 0x302:return src[3];case 0x303:return 1-src[3];
    case 0x304:return dst[3];case 0x305:return 1-dst[3];case 0x306:return dst[c];case 0x307:return 1-dst[c];
    case 0x308:return c==3?1.f:std::min(src[3],1-dst[3]);
    case 0x8001:return constant[c];case 0x8002:return 1-constant[c];case 0x8003:return constant[3];case 0x8004:return 1-constant[3];
    default:throw std::runtime_error("unsupported RSX blend factor "+std::to_string(f));
    }
}
''' + FIXED_CPP + r'''
static std::uint64_t target_span(const Target& t,unsigned width,unsigned height){
    return !t.base?0:t.swizzle_width?std::uint64_t(t.swizzle_width)*t.swizzle_height*t.bpp:t.tile?t.tile->size:std::uint64_t(height-1)*t.pitch+width*t.bpp;
}
static bool overlaps(const void* a,std::uint64_t an,const void* b,std::uint64_t bn){
    const auto x=reinterpret_cast<std::uintptr_t>(a),y=reinterpret_cast<std::uintptr_t>(b);
    return an && bn && x<y+bn && y<x+an;
}
static bool target_overlaps(const Target& a,const Target& b,unsigned width,unsigned height){
    if(a.mapping && b.mapping)return a.mapping->overlaps(*b.mapping);
    return overlaps(a.base,target_span(a,width,height),b.base,target_span(b,width,height));
}
static bool target_overlaps(const Target& a,const void* p,std::uint64_t size,unsigned width,unsigned height){
    return a.mapping?a.mapping->overlaps(p,size):overlaps(a.base,target_span(a,width,height),p,size);
}
// Certify bounds AND one-to-one storage for the entire surface. Testing 32B
// runs suffices because tiled addressing preserves the low five bits. Cache
// only successes, by every address-affecting value, never by a guest pointer.
static bool parallel_target(const Target& t,unsigned width,unsigned height){
    if(!t.base)return true;
    if(t.observed)return false;
    if(t.mapping){
        // Each logical byte must have a distinct host destination. Range parts
        // are immutable during a draw; untracked writes don't enter GuestMemory.
        for(unsigned i=0;i<t.mapping->parts.size();++i)for(unsigned j=0;j<i;++j){
            const auto& a=t.mapping->parts[i];const auto& b=t.mapping->parts[j];
            if(overlaps(a.data,a.size,b.data,b.size))return false;
        }
    }
    if(!t.tile)return true; // bind_target already checked linear row separation.
    const auto& tile=*t.tile;
    using Key=std::array<std::uint32_t,9>;
    const Key key{t.offset,t.pitch,width,height,tile.offset,tile.pitch,tile.size,tile.bank,t.bpp};
    static thread_local std::vector<Key> certified;
    if(std::find(certified.begin(),certified.end(),key)!=certified.end())return true;
    std::vector<std::uint32_t> physical;
    physical.reserve(std::size_t(height)*(width/8+2));
    try{
        for(unsigned y=0;y<height;++y){
            const auto first=std::uint64_t(t.offset)-tile.offset+std::uint64_t(y)*t.pitch;
            const auto end=first+width*t.bpp;
            for(auto at=first&~UINT64_C(31);at<end;at+=32){
                if(at+32>tile.size)return false;
                const auto p=rsx_tiled_offset(tile,static_cast<std::uint32_t>(at));
                if(p%32 || std::uint64_t(p)+32>std::uint64_t(tile.offset)+tile.size)return false;
                physical.push_back(p);
            }
        }
    }catch(const std::runtime_error&){return false;}
    std::sort(physical.begin(),physical.end());
    if(std::adjacent_find(physical.begin(),physical.end())!=physical.end())return false;
    if(certified.size()==64)certified.erase(certified.begin());
    certified.push_back(key);return true;
}
// Check even unused operands conservatively. No supported worker instruction
// can then take an interpreter exception path (registers, scale, opcode, wrap).
// Texture storage was accessed once by decode_texture, before binding targets.
static bool parallel_fragment(const std::vector<FpInst>& fp,const std::array<Texture,16>& textures){
    for(const auto& inst:fp){
        const unsigned op=((inst.d>>24)&63)|((inst.s1>>31)<<6);
        switch(op){
        case 0:case 1:case 2:case 3:case 4:case 5:case 6:case 8:case 9:case 10:case 11:case 12:case 13:
        case 14:case 15:case 16:case 17:case 18:case 21:case 22:case 23:case 24:case 26:case 27:case 28:
        case 7:case 19:case 20:case 25:case 29:case 30:case 31:case 32:case 33:case 34:case 35:case 36:case 37:case 38:case 39:case 40:case 41:case 42:case 43:case 44:case 45:case 46:case 47:case 49:case 54:case 56:case 57:case 58:case 59:case 60:break;
        default:return false;
        }
        if(((inst.s1>>28)&7)==4)return false;
        if((inst.s2&(1u<<30)) && ((inst.s0&3)==1 || (inst.s1&3)==1 || (inst.s2&3)==1))return false;
        for(unsigned i=0;i<3;++i)if(((inst.s1>>(19+3*i))&7)>5)return false;
        for(const auto raw:{inst.s0,inst.s1,inst.s2}){
            if((raw&3)==3 || ((raw&3)==1 && ((inst.d>>13)&15)>14))return false;
            if((raw&3)==0 && !(raw&256) && ((raw>>2)&63)>=48)return false;
        }
        if(!(inst.d&(1u<<30)) && !(inst.d&128) && ((inst.d>>1)&63)>=48)return false;
        if(op==23 || op==24 || op==25 || op==47 || op==49){
            const auto& t=textures[(inst.d>>17)&15];
            auto wrap=[](unsigned w){return (w>=1 && w<=6) || w==8;};
            if(!t.valid || !wrap(t.wrap_s) || !wrap(t.wrap_t))return false;
        }
    }
    return true;
}
static bool parallel_fixed(bool depth_test,unsigned depth_func,bool alpha_test,unsigned alpha_func,
                           bool blend,std::uint32_t sf,std::uint32_t df,std::uint32_t eq){
    if((depth_test && (depth_func<0x200 || depth_func>0x207)) ||
       (alpha_test && (alpha_func<0x200 || alpha_func>0x207)))return false;
    if(!blend)return true;
    auto factor=[](unsigned f){return f<=1 || (f>=0x300 && f<=0x308) || (f>=0x8001 && f<=0x8004);};
    auto equation=[](unsigned e){return e==0x8006 || e==0x800a || e==0x800b || e==0x8007 || e==0x8008;};
    return factor(sf&0xffff) && factor(sf>>16) && factor(df&0xffff) && factor(df>>16) &&
        equation(eq&0xffff) && equation(eq>>16);
}
} // namespace rsxdraw

''' + VK_DRAW_CPP + GRAPHICS_CPP + r'''
// Returns true when reg is a SET_VERTEX_DATA*_M register write it handled.
static bool rsx_vertex_data_register(NativeRsx& gpu,std::uint32_t reg,std::uint32_t value){
    auto& draw=gpu.draw;
    if(reg==0x1808){
        if(!value && !draw.inline_bytes.empty())throw std::runtime_error("RSX INLINE_ARRAY has an incomplete vertex");
        if(!value)draw.inline_active=false;
        if(value){draw.inline_active=false;draw.inline_bytes.clear();}
        return false;
    }
    if(reg==0x1818){
        if(!draw.primitive || !draw.ranges.empty() || !draw.elements.empty() || (!draw.inline_active && !draw.immediate_vertices.empty()))
            throw std::runtime_error("RSX INLINE_ARRAY outside begin/end or mixed vertex sources");
        draw.inline_active=true;
        unsigned stride=0;std::array<unsigned,16> formats{},sizes{};
        for(unsigned a=0;a<16;++a){
            formats[a]=rsxdraw::Reg{gpu}(0x1740+4*a);const unsigned n=(formats[a]>>4)&15,t=formats[a]&15;
            if(!n)continue;
            if(n>4 || !t || t==6 || t>7)throw std::runtime_error("unsupported RSX INLINE_ARRAY attribute format");
            sizes[a]=t==2?n*4:(t==1||t==3||t==5)?(n==3?4:n)*2:n;
            if((t==4 || t==7) && n!=4)throw std::runtime_error("unsupported RSX INLINE_ARRAY byte component count");
            stride+=sizes[a];
        }
        if(!stride)throw std::runtime_error("RSX INLINE_ARRAY has no enabled array formats");
        for(unsigned i=0;i<4;++i)draw.inline_bytes.push_back(value>>(24-8*i));
        while(draw.inline_bytes.size()>=stride){
            auto vertex=draw.immediate;unsigned at=0;
            for(unsigned a=0;a<16;++a){
                const unsigned n=(formats[a]>>4)&15,t=formats[a]&15;
                if(!n)continue;
                auto& v=vertex[a];v={};v.count=n;
                if(t==4){v.type=1;v.words[0]=rsxdraw::read_be(draw.inline_bytes.data()+at,4);}
                else for(unsigned i=0;i<n;++i){
                    const unsigned bytes=t==2?4:t==7?1:2;
                    const auto raw=rsxdraw::read_be(draw.inline_bytes.data()+at+i*bytes,bytes);
                    const float f=t==2?rsxdraw::bits_float(raw):t==3?rsxdraw::half_float(raw):t==1?
                        std::max(-1.f,float(std::int16_t(raw))/32767.f):t==5?float(std::int16_t(raw)):float(raw);
                    v.words[i]=rsxdraw::float_bits(f);
                }
                at+=sizes[a];
            }
            draw.immediate_vertices.push_back(vertex);
            draw.inline_bytes.erase(draw.inline_bytes.begin(),draw.inline_bytes.begin()+stride);
        }
        return true;
    }
    unsigned attr=0,component=0,count=0,type=0;
    if(reg>=0xa80 && reg<0xb00){attr=(reg-0xa80)/8;component=((reg-0xa80)/4)%2;count=4;type=3;}
    else if(reg>=0x1900 && reg<0x1940){attr=(reg-0x1900)/4;count=2;type=2;}
    else if(reg>=0x1980 && reg<0x1a00){attr=(reg-0x1980)/8;component=((reg-0x1980)/4)%2;count=4;type=2;}
    else if(reg>=0x1940 && reg<0x1980){attr=(reg-0x1940)/4;count=4;type=1;}
    else if(reg>=0x1e40 && reg<0x1e80){attr=(reg-0x1e40)/4;count=1;}
    else if(reg>=0x1880 && reg<0x1900){attr=(reg-0x1880)/8;component=(reg/4)%2;count=2;}
    else if(reg>=0x1500 && reg<0x1600){attr=(reg-0x1500)/16;component=(reg/4)%4;count=3;if(component==3)return true;}
    else if(reg>=0x1c00 && reg<0x1d00){attr=(reg-0x1c00)/16;component=(reg/4)%4;count=4;}
    else return false;
    if(draw.inline_active)throw std::runtime_error("mixed RSX INLINE_ARRAY and immediate vertices");
    auto& entry=gpu.draw.immediate[attr];
    entry.count=count;entry.type=type;entry.words[component]=value;
    // Inside begin/end, completing ATTR0 emits a vertex from current registers.
    if(gpu.draw.primitive && attr==0 && component+1==((type==2 || type==3)?count/2:(count==3?3u:count))){
        if(!gpu.draw.ranges.empty() || !gpu.draw.elements.empty())throw std::runtime_error("mixed RSX immediate and array draws");
        gpu.draw.immediate_vertices.push_back(gpu.draw.immediate);
    }
    return true;
}
static void rsx_execute_draw(PpuState& s){
    using namespace rsxdraw;
    GuestMemory::SplitReadScope split_reads(s.memory);
    auto& gpu=s.rsx;auto& draw=gpu.draw;auto& m=s.memory;const Reg reg{gpu};
    // NV4097_SET_RENDER_ENABLE mode 2 with a zero report value (rsx_set_render_enable): the draw has no effect.
    if(gpu.render_disabled){draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;}
    try{
#ifdef ANYPS3_RSX_VK
    // An active Session was admitted through the GPU FIFO path. Standalone
    // draws still read the environment, including tests that change backend.
    const char* backend=gpu.graphics_session?"gpu":std::getenv("ANYPS3_RSX_BACKEND");
    backend=backend?backend:"gpu";
    if(std::strcmp(backend,"cpu") && std::strcmp(backend,"gpu") && std::strcmp(backend,"compute"))throw std::runtime_error("invalid ANYPS3_RSX_BACKEND");
    const bool graphics_backend=!std::strcmp(backend,"gpu");
    if(!graphics_backend && rsx_graphics_resolve_conditional(s)){draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;}
    std::optional<GuestMemory::RawYield> gpu_payload;
    if(graphics_backend)gpu_payload.emplace(m);
    // The explicitly selected CPU diagnostic keeps its legacy pointer path.
    std::unique_ptr<GuestMemory::RawPermit> diagnostic_raw;
#else
    constexpr bool graphics_backend=false;
#endif
    // The thread count only splits CPU rasterization; the GPU path never reads it.
    const unsigned requested_threads=graphics_backend?1u:draw_thread_count();
    PerfDraw perf;if(host_stats().enabled)++host_stats().draws;::RsxPerfCounters::add(::RsxPerfCounters::draws);
    HostPhase host_phase(HostStage::indices);
    // ---- indices
    auto& indices=draw.scratch_indices;indices.clear();
    bool lazy_geometry=false;
#ifdef ANYPS3_RSX_VK
    std::optional<rsxgraphics::DeviceGeometry> device_geometry;
    if(graphics_backend){device_geometry.emplace(draw);lazy_geometry=device_geometry->valid;}
#endif
    auto build_indices=[&]{
    const auto base_index=reg.get(0x173c,0);
    if(!draw.elements.empty()){
        const bool restart=reg.get(0x1dac,0)&1;
        for(const auto& entry:draw.elements){
            const auto raw=entry.first,mask=entry.second==2?0xffffu:UINT32_MAX;
            if(restart && raw==(reg.get(0x1db0,UINT32_MAX)&mask))indices.push_back(UINT32_MAX);
            else{if(std::uint64_t(raw)+base_index>=UINT32_MAX)throw std::runtime_error("RSX vertex index overflow");indices.push_back(raw+base_index);}
        }
    }else if(draw.indexed){
        const auto dma=reg(0x1820),offset=reg(0x181c);
        const unsigned type=(dma>>4)&15,width=type==0?4:type==1?2:0;
        if(!width || (dma&15)>1)throw std::runtime_error("unsupported RSX index array format/location");
        const bool restart=reg.get(0x1dac,0)&1;
        const unsigned restart_index=reg.get(0x1db0,0xffffffff)&(width==2?0xffff:0xffffffff);
        for(const auto& [first,count]:draw.ranges){
            const auto begin=std::uint64_t(offset)+std::uint64_t(first)*width;
            if(begin>UINT32_MAX || std::uint64_t(count)*width>UINT32_MAX)throw std::runtime_error("RSX index array address overflow");
            const auto address=location_address(gpu,dma&15,begin,count*width);
            if(!count)continue;
            auto& bytes=draw.scratch_index_bytes;bytes.resize(count*width);
            m.transfer(address,bytes.data(),count*width,false);   // one admitted read; the Range would only be read once
            // Same values/errors as the per-element loop: decode into place,
            // checking overflow on the unrestarted maximum only.
            const auto at=indices.size();indices.resize(at+count);
            std::uint32_t largest=0;bool any=false;
            decode_indices(bytes.data(),indices.data()+at,count,width,restart,restart_index,base_index,largest,any);
            if(any && std::uint64_t(largest)+base_index>=UINT32_MAX)throw std::runtime_error("RSX vertex index overflow");
        }
    }else if(!draw.immediate_vertices.empty()){
        for(std::uint32_t i=0;i<draw.immediate_vertices.size();++i)indices.push_back(i);
    }else for(const auto& [first,count]:draw.ranges)for(unsigned i=0;i<count;++i)indices.push_back(first+i);
    };
    if(!lazy_geometry)build_indices();
    host_phase.next(HostStage::assembly);
    // ---- vertices
    const auto input_mask=reg(0x1ff0),start=reg.get(0x1ea0,0);
    std::map<std::uint32_t,ShadedVertex> cache;
    auto vertex=[&](std::uint32_t index)->const ShadedVertex&{
        auto it=cache.find(index);if(it!=cache.end())return it->second;
        std::array<Vec4,16> in{};
        for(unsigned a=0;a<16;++a)in[a]=!((input_mask>>a)&1)?Vec4{0,0,0,1}:
            draw.immediate_vertices.empty()?fetch_attribute(s,reg,a,index):register_attribute(draw.immediate_vertices[index][a]);
        const auto out=run_vertex_program(gpu,start,in,&s);
        ShadedVertex v;v.clip=out.o[0];v.o=out.o;
        return cache.emplace(index,v).first->second;
    };
    // ---- primitive assembly
    auto& triangles=draw.scratch_triangles;triangles.clear();
    const bool point_draw=draw.primitive==1,line_draw=draw.primitive>=2 && draw.primitive<=4;
    std::vector<std::array<unsigned,2>> lines;
    auto assemble=[&](const std::vector<std::uint32_t>& indices){
    const auto n=indices.size();
    switch(draw.primitive){
    case 2:for(std::size_t i=0;i+1<n;i+=2)lines.push_back({indices[i],indices[i+1]});break;
    case 3:case 4:
        for(std::size_t i=0;i+1<n;++i)lines.push_back({indices[i],indices[i+1]});
        if(draw.primitive==3 && n>1)lines.push_back({indices.back(),indices.front()});
        break;
    case 1:break; // expanded into squares after viewport/program state is known
    case 5:{const auto at=triangles.size();triangles.resize(at+n/3);if(n/3)std::memcpy(triangles.data()+at,indices.data(),n/3*3*sizeof(std::uint32_t));}break;
    case 6:for(std::size_t i=0;i+2<n;++i)triangles.push_back(i&1?std::array<std::uint32_t,3>{indices[i+1],indices[i],indices[i+2]}:std::array<std::uint32_t,3>{indices[i],indices[i+1],indices[i+2]});break;
    case 7:case 10:for(std::size_t i=1;i+1<n;++i)triangles.push_back({indices[0],indices[i],indices[i+1]});break;
    case 8:for(std::size_t i=0;i+3<n;i+=4){triangles.push_back({indices[i],indices[i+1],indices[i+2]});triangles.push_back({indices[i],indices[i+2],indices[i+3]});}break;
    case 9:for(std::size_t i=0;i+3<n;i+=2){triangles.push_back({indices[i],indices[i+1],indices[i+3]});triangles.push_back({indices[i],indices[i+3],indices[i+2]});}break;
    default:throw std::runtime_error("unsupported RSX primitive type "+std::to_string(draw.primitive));
    }
    };
    auto build_triangles=[&]{
    if(draw.indexed && (reg.get(0x1dac,0)&1)){
        std::vector<std::uint32_t> strip;
        for(const auto index:indices){if(index==UINT32_MAX){assemble(strip);strip.clear();}else strip.push_back(index);}
        assemble(strip);
        indices.erase(std::remove(indices.begin(),indices.end(),UINT32_MAX),indices.end());
    }else assemble(indices);
    };
    if(!lazy_geometry)build_triangles();
    host_phase.next(HostStage::state);
    // ---- fixed state
    const auto surface_format=reg(0x208),color_target=reg(0x220);
    const unsigned color_format=surface_format&31,depth_format=(surface_format>>5)&7;
    const bool swizzled=((surface_format>>8)&15)==2;
    if(((surface_format>>8)&15)!=1 && !swizzled)throw std::runtime_error("unsupported RSX draw surface type");
    const unsigned aa=(surface_format>>12)&15;
    if(aa!=0 && aa!=3 && aa!=4 && aa!=5)throw std::runtime_error("unsupported RSX draw antialiasing");
    const unsigned samples_x=aa?2:1,samples_y=aa>=4?2:1,samples=samples_x*samples_y;
    // RPCS3 uses host MSAA sample indices, then expands them to x+y*2 in
    // ColorResolvePass.glsl. Match standard Vulkan positions for diagonal/rotated;
    // centered 4 uses a 2x2 grid. This is an explicit software sampling model,
    // not a characterization of RSX's undocumented programmable locations.
    const std::array<std::array<float,2>,4> positions=aa==3?
        std::array<std::array<float,2>,4>{{{.75f,.75f},{.25f,.25f},{0,0},{0,0}}}:aa==4?
        std::array<std::array<float,2>,4>{{{.25f,.25f},{.75f,.25f},{.25f,.75f},{.75f,.75f}}}:aa==5?
        std::array<std::array<float,2>,4>{{{.375f,.125f},{.875f,.375f},{.125f,.625f},{.625f,.875f}}}:
        std::array<std::array<float,2>,4>{{{.5f,.5f},{0,0},{0,0},{0,0}}};
    unsigned color_count=0;
    std::array<unsigned,4> target_ids{};
    switch(color_target){
    case 0:break;case 1:color_count=1;break;case 2:color_count=1;target_ids[0]=1;break;
    case 0x13:color_count=2;target_ids={0,1,2,3};break;
    case 0x17:color_count=3;target_ids={0,1,2,3};break;
    case 0x1f:color_count=4;target_ids={0,1,2,3};break;
    default:throw std::runtime_error("unsupported RSX draw color target set "+std::to_string(color_target));
    }
    const unsigned color_bpp=color_target?color_bytes(color_format):4;
    if(reg.get(0x1478,0)&0xff000000)throw std::runtime_error("unsupported RSX user clip plane control bits");
    for(unsigned i=0;i<6;++i)if(((reg.get(0x1478,0)>>(4*i))&15)>2)throw std::runtime_error("invalid RSX user clip plane mode");
    // 16-bit target dithering (0x300): the RSX ordered-dither pattern is
    // undocumented; RPCS3's Vulkan backend never applies it (rsx_methods.h
    // dither_enabled() unused in VK/). Derived approximation: quantize without
    // dither (at most 1 LSB per channel), as for host GPU rounding.
    const unsigned width=reg(0x200)>>16,height=reg(0x204)>>16,clip_x=reg(0x200)&0xffff,clip_y=reg(0x204)&0xffff;
    // A zero surface clip is an invalid framebuffer: RPCS3 RSXThread.cpp
    // get_framebuffer_layout (1477-1487) leaves rtt_config_valid clear and the
    // draw does nothing. Larger than 4096 remains an explicit stop.
    if(!width || !height){draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;}
    if(width>4096 || height>4096)throw std::runtime_error("invalid RSX draw surface size");
    validate_triangle_state(reg);
    const Stencil front_stencil(reg,false,depth_format!=1),back_stencil(reg,true,depth_format!=1);
    const bool stencil=front_stencil.enabled && depth_format==2,logic=reg.get(0x374,0)&1,bounds=reg.get(0x380,0)&1;
    const auto logic_func=reg.get(0x378,0x1503);
    if(logic)(void)logic_op(logic_func,0,0);
    const float bounds_min=bits_float(reg.get(0x384,0)),bounds_max=bits_float(reg.get(0x388,0x3f800000));
    if(bounds && (!std::isfinite(bounds_min) || !std::isfinite(bounds_max) || bounds_min>bounds_max))
        throw std::runtime_error("invalid RSX depth bounds");
    const bool offset_fill=!point_draw && !line_draw && (reg.get(0xa68,0)&1);
    const float offset_factor=bits_float(reg.get(0xa78,0)),offset_units=bits_float(reg.get(0xa7c,0));
    if(offset_fill && (!std::isfinite(offset_factor) || !std::isfinite(offset_units)))throw std::runtime_error("invalid RSX polygon offset");
    if(!point_draw && !line_draw && (reg.get(0x1828,0x1b02)!=0x1b02 || reg.get(0x182c,0x1b02)!=0x1b02))throw std::runtime_error("RSX non-fill polygon mode is unsupported");
    const bool two_sided=reg.get(0x142c,0)&1;
    const bool framebuffer_srgb=reg.get(0x1fec,0)!=0;
    if(logic && float_color(color_format))throw std::runtime_error("RSX logic operations on float targets are unsupported");
    if(framebuffer_srgb && float_color(color_format))throw std::runtime_error("RSX sRGB conversion on float targets is unsupported");
    const auto shader_control=reg(0x1d60);
    const bool depth_export=shader_control&0xe;
    if(reg.get(0x1d88,0)&0xff0000 || (reg.get(0x1d88,0)>>12)&15)throw std::runtime_error("unsupported RSX shader window origin/pixel center");
    const bool depth_test=reg.get(0xa74,0)&1,depth_write=reg.get(0xa70,0)&1;
    const bool use_depth=depth_test || depth_write || stencil || bounds;
    const unsigned depth_bpp=depth_format==1?2:4,depth_max=depth_format==1?65535:16777215;
    if(use_depth && depth_format!=2 && depth_format!=1)throw std::runtime_error("unsupported RSX depth format for draw");
    const bool alpha_test=reg.get(0x304,0)&1;
    const unsigned blend_mask=logic?0:((reg.get(0x310,0)&1)|(reg.get(0x36c,0)&14));
    const bool blend=blend_mask&((1u<<color_count)-1);
    if(reg.get(0x36c,0)&~15u)throw std::runtime_error("invalid RSX MRT blend mask");
    const auto color_mask=reg.get(0x324,0x01010101);
    const unsigned shade_flat=reg.get(0x368,0x1d01)==0x1d00;
    const bool cull=!point_draw && !line_draw && (reg.get(0x183c,0)&1);const auto cull_face=reg.get(0x1830,0x405),front_face=reg.get(0x1834,0x901);
    const float vx=bits_float(reg(0xa20)),vy=bits_float(reg(0xa24)),vz=bits_float(reg(0xa28));
    const float sx=bits_float(reg(0xa30)),sy=bits_float(reg(0xa34)),sz=bits_float(reg(0xa38));
    const float clip_min=bits_float(reg.get(0x394,0)),clip_max=bits_float(reg.get(0x398,0x3f800000));
    const unsigned window_x=reg.get(0x2b8,0)&0xffff,window_y=reg.get(0x2b8,0)>>16;
    const bool window_exclude=reg.get(0x2bc,0)&1;
    std::array<std::array<unsigned,2>,8> windows{};unsigned window_count=0;
    for(unsigned i=0;i<8;++i)if(gpu.registers.count(0x2c0+i*8) && gpu.registers.count(0x2c4+i*8))
        windows[window_count++]={reg.get(0x2c0+i*8,0),reg.get(0x2c4+i*8,0)};
    const auto blend_colour2=reg.get(0x37c,0);
    const bool stipple=reg.get(0x147c,0)!=0;
    std::array<unsigned,32> stipple_rows{};
    if(stipple)for(unsigned i=0;i<32;++i)stipple_rows[i]=reg.get(0x1480+4*i,0);
    auto window_visible=[&](unsigned x,unsigned y){
        bool any=false,inside=false;
        for(unsigned i=0;i<window_count;++i){
            const auto h=windows[i][0],v=windows[i][1];
            any=true;inside|=x>=(h&0xffff) && x<=(h>>16) && y>=(v&0xffff) && y<=(v>>16);
        }
        return !any || (window_exclude?!inside:inside);
    };
    unsigned x0=std::max(clip_x,reg.get(0x8c0,0x10000000)&0xffff),y0=std::max(clip_y,reg.get(0x8c4,0x10000000)&0xffff);
    unsigned x1=std::min({clip_x+width,(reg.get(0x8c0,0x10000000)&0xffff)+(reg.get(0x8c0,0x10000000)>>16),width});
    unsigned y1=std::min({clip_y+height,(reg.get(0x8c4,0x10000000)&0xffff)+(reg.get(0x8c4,0x10000000)>>16),height});
    const auto viewport_h=reg.get(0xa00,(width<<16)),viewport_v=reg.get(0xa04,(height<<16));
    x0=std::max(x0,viewport_h&0xffff);y0=std::max(y0,viewport_v&0xffff);
    x1=std::min(x1,(viewport_h&0xffff)+(viewport_h>>16));y1=std::min(y1,(viewport_v&0xffff)+(viewport_v>>16));
    if(x0>=x1 || y0>=y1){draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();++draw.draws;return;}
    std::shared_ptr<const std::vector<FpInst>> fp_owner;if(color_target || alpha_test || depth_export)fp_owner=load_fragment_program_shared(s,reg);
    static const std::vector<FpInst> no_fragment_program;const auto& fp=fp_owner?*fp_owner:no_fragment_program;
    host_phase.next(HostStage::texture_metadata);
    std::array<Texture,16> textures{};
    unsigned used_inputs=0;
#ifdef ANYPS3_RSX_VK
    bool texture_context_validated=false;
#endif
    for(const auto& inst:fp){
        // Conservatively include every input source, even one ignored by its
        // opcode. Interpolate each consumed input with the original FP order.
        if((inst.s0&3)==1 || (inst.s1&3)==1 || (inst.s2&3)==1)used_inputs|=1u<<((inst.d>>13)&15);
        const unsigned opcode=((inst.d>>24)&63)|((inst.s1>>31)<<6);
        if(opcode==23 || opcode==24 || opcode==25 || opcode==47 || opcode==49){const unsigned unit=(inst.d>>17)&15;
            if(!(reg.get(0x1a00+unit*32+0xc,0)>>31)){textures[unit].null_texture=true;continue;}
            if(!textures[unit].valid){
#ifdef ANYPS3_RSX_VK
            if(graphics_backend && gpu.graphics_session){
                auto& session=*static_cast<rsxgraphics::Session*>(gpu.graphics_session);
                if(!texture_context_validated){session.validate_texture_context();texture_context_validated=true;}
                textures[unit]=session.texture_metadata(unit,true);
            }
            else
#endif
            textures[unit]=decode_texture(s,reg,unit); }}
        else if(opcode==51 || opcode==52)throw std::runtime_error("unsupported RSX FP texture opcode "+std::to_string(opcode));
    }
    for(unsigned t=0;t<10;++t)if((used_inputs&(1u<<(4+t))) && (reg.get(0xb40+4*t,0)&~0x11u))
        throw std::runtime_error("unsupported RSX texcoord control");
    const auto output_mask=reg.get(0x1ff4,0x3fffff);
    for(unsigned i=1;i<14;++i)if(used_inputs&(1u<<i)){
        unsigned required=i==1?0x5:i==2?0xa:i==3?0x10:i<12?1u<<(i+10):1u<<i;
        const bool sprite_available=point_draw && (reg.get(0x1ee8,0)&1) && i>=4 && i<=13 && (reg.get(0x1ee8,0)&(1u<<(8+i-4)));
        const bool available=sprite_available || ((i<=2 && !two_sided)?bool(output_mask&required):(output_mask&required)==required);
        if(!available)throw std::runtime_error("RSX fragment reads a masked vertex output");
    }
    const bool fog_configured=gpu.registers.count(0x8cc);
    if((used_inputs&8) && fog_configured)(void)fog_value(reg.get(0x8cc,0x800),0,bits_float(reg.get(0x8d0,0)),bits_float(reg.get(0x8d4,0)));
    if(point_draw){
        // Aliased point rules: OpenGL 2.1 section 3.3.1. RPCS3 uses gl_PointSize
        // from o6.x when output-mask bit 5 is enabled, otherwise SET_POINT_SIZE.
        if(aa)throw std::runtime_error("RSX multisample point rasterization is unsupported");
        if(reg.get(0x1ee8,0)&~0x3ff01u)throw std::runtime_error("unsupported RSX point sprite control bits");
        if(!std::isfinite(sx) || !std::isfinite(sy) || sx==0 || sy==0)throw std::runtime_error("invalid RSX point viewport");
        unsigned next=UINT32_MAX-1;
        for(unsigned id:indices){
            const auto center=vertex(id);
            if(clip_polygon({&center,&center,&center},reg.get(0x1478,0),output_mask,sz,vz,clip_min,clip_max).empty())continue;
            if(!(center.clip[3]>0) || std::fabs(center.clip[0])>center.clip[3] || std::fabs(center.clip[1])>center.clip[3])continue;
            const float size=output_mask&(1u<<5)?center.o[6][0]:bits_float(reg.get(0x1ee0,0x3f800000));
            if(!std::isfinite(size) || size<=0 || size>64)throw std::runtime_error("unsupported RSX aliased point size (supported 0<size<=64)");
            const unsigned n=std::max(1u,unsigned(std::lround(size)));
            const float x=center.clip[0]/center.clip[3]*sx+vx,y=center.clip[1]/center.clip[3]*sy+vy;
            const float cx=n&1?std::floor(x)+.5f:std::floor(x+.5f),cy=n&1?std::floor(y)+.5f:std::floor(y+.5f);
            const float left=cx-n*.5f,top=cy-n*.5f;
            std::array<unsigned,4> square;
            for(unsigned i=0;i<4;++i){
                while(cache.count(next) || std::find(indices.begin(),indices.end(),next)!=indices.end()){
                    if(!next)throw std::runtime_error("RSX point expansion exhausted indices");
                    --next;
                }
                ShadedVertex v=center;
                v.clip[0]=(left+((i==1 || i==2)?n:0)-vx)/sx*center.clip[3];
                v.clip[1]=(top+(i>=2?n:0)-vy)/sy*center.clip[3];
                const auto sprite=reg.get(0x1ee8,0);
                if(sprite&1)for(unsigned t=0;t<10;++t)if(sprite&(1u<<(8+t)))
                    v.o[t<8?7+t:t==8?15:6]={float(i==1 || i==2),float(i>=2),1,0};
                square[i]=next--;cache.emplace(square[i],v);
            }
            triangles.push_back({square[0],square[1],square[2]});triangles.push_back({square[0],square[2],square[3]});
        }
    }
    std::map<unsigned,LineGeometry> line_geometry;
    if(line_draw){
        if(aa || reg.get(0x3bc,0) || reg.get(0x1db4,0))
            throw std::runtime_error("unsupported RSX multisample/smooth/stipple line rasterization");
        const unsigned raw=reg.get(0x3b8,8);const unsigned line_width=std::max(1u,unsigned(std::lround(raw/8.f)));
        if(!raw || line_width>64)throw std::runtime_error("unsupported RSX aliased line width");
        if(!std::isfinite(sx) || !std::isfinite(sy) || sx==0 || sy==0)throw std::runtime_error("invalid RSX line viewport");
        unsigned next=UINT32_MAX-1;
        for(const auto& ids:lines){
            const auto a=vertex(ids[0]),b=vertex(ids[1]);
            const auto clipped=clip_polygon({&a,&b,&b},reg.get(0x1478,0),output_mask,sz,vz,clip_min,clip_max);
            if(clipped.empty())continue;
            auto first=clipped.front(),last=first;
            double lo=std::numeric_limits<double>::infinity(),hi=-lo;
            // Degenerate polygon clipping can rotate its first vertex. Recover
            // original line direction, needed for the half-open final endpoint.
            for(const auto& v:clipped){
                double t=0;for(unsigned c=0;c<4;++c)t+=(double(v.clip[c])-a.clip[c])*(double(b.clip[c])-a.clip[c]);
                if(t<lo){lo=t;first=v;}if(t>hi){hi=t;last=v;}
            }
            const float ax=first.clip[0]/first.clip[3]*sx+vx,ay=first.clip[1]/first.clip[3]*sy+vy;
            const float bx=last.clip[0]/last.clip[3]*sx+vx,by=last.clip[1]/last.clip[3]*sy+vy;
            if(!std::isfinite(ax) || !std::isfinite(ay) || !std::isfinite(bx) || !std::isfinite(by) ||
               std::max({std::fabs(ax),std::fabs(ay),std::fabs(bx),std::fabs(by)})>1048576.f)
                throw std::runtime_error("RSX line window coordinates out of range");
            const float dx=bx-ax,dy=by-ay;if(dx==0 && dy==0)continue;
            const float left=std::min(ax,bx)-line_width,top=std::min(ay,by)-line_width;
            const float right=std::max(ax,bx)+line_width,bottom=std::max(ay,by)+line_width;
            std::array<unsigned,4> square;
            for(unsigned i=0;i<4;++i){
                while(cache.count(next) || std::find(indices.begin(),indices.end(),next)!=indices.end()){
                    if(!next)throw std::runtime_error("RSX line expansion exhausted indices");
                    --next;
                }
                ShadedVertex v=(i==1 || i==2)?last:first;
                v.clip[0]=(((i==1 || i==2)?right:left)-vx)/sx*v.clip[3];
                v.clip[1]=((i>=2?bottom:top)-vy)/sy*v.clip[3];
                square[i]=next--;cache.emplace(square[i],v);
            }
            line_geometry.emplace(square[0],LineGeometry{ax,ay,dx,dy,line_width,(reg.get(0x1454,0)?a:b).o});
            triangles.push_back({square[0],square[1],square[2]});triangles.push_back({square[0],square[2],square[3]});
        }
    }
    host_phase.next(HostStage::targets);
    unsigned swizzle_width=0,swizzle_height=0;
    if(swizzled){
        const unsigned lw=(surface_format>>16)&255,lh=surface_format>>24;
        if(lw>12 || lh>12)throw std::runtime_error("invalid RSX swizzled draw dimensions");
        swizzle_width=(1u<<lw)*samples_x;swizzle_height=(1u<<lh)*samples_y;
        if(width*samples_x>swizzle_width || height*samples_y>swizzle_height)
            throw std::runtime_error("RSX draw clip exceeds swizzled surface dimensions");
        if(window_x || window_y)throw std::runtime_error("unsupported RSX swizzled draw window offset");
    }
    std::array<Target,4> colors{};Target depth;
    auto window_offset=[&](unsigned offset,unsigned pitch,unsigned bpp){
        const auto at=std::uint64_t(offset)+std::uint64_t(window_y)*samples_y*pitch+std::uint64_t(window_x)*samples_x*bpp;
        if(at>UINT32_MAX)throw std::runtime_error("RSX window surface offset overflow");
        return static_cast<unsigned>(at);
    };
    const unsigned contexts[]={0x194,0x18c,0x1b4,0x1b8},offsets[]={0x210,0x218,0x288,0x28c},pitches[]={0x20c,0x21c,0x280,0x284};
    for(unsigned i=0;i<color_count;++i){const unsigned id=target_ids[i];
        colors[i]=bind_target(s,reg(contexts[id]),window_offset(reg(offsets[id]),swizzled?swizzle_width*color_bpp:reg(pitches[id]),color_bpp),
            reg(pitches[id]),width*samples_x,height*samples_y,color_bpp,swizzle_width,swizzle_height);
    }
    const auto& color=colors[0];
    if(use_depth)depth=bind_target(s,reg(0x198),window_offset(reg(0x214),swizzled?swizzle_width*depth_bpp:reg(0x22c),depth_bpp),
        reg(0x22c),width*samples_x,height*samples_y,depth_bpp,swizzle_width,swizzle_height);
    const unsigned output_reg=0;
    const unsigned output_regs[]={0,2,3,4};
    std::array<unsigned,4> color_masks{color_mask,0,0,0};
    for(unsigned i=1;i<color_count;++i){const unsigned n=(reg.get(0x370,0)>>(4*i))&15;
        color_masks[i]=((n&1)?0x01000000:0)|((n&2)?0x00010000:0)|((n&4)?0x100:0)|((n&8)?1:0);
    }
    const unsigned depth_func=depth_test?reg(0xa6c):0x207,alpha_func=alpha_test?reg(0x308):0x207;
    const float alpha_ref=float(reg.get(0x30c,0)&255)/255.f;
    const auto blend_sf=blend?reg(0x314):0,blend_df=blend?reg(0x318):0,blend_eq=blend?reg(0x320):0,blend_colour=reg.get(0x31c,0);
    const bool half_output=!(shader_control&0x40);
    // A single MOV from an inline constant cannot depend on interpolated inputs
    // or neighbouring lanes. Still use the interpreter for predicates, masks,
    // precision, half-register aliasing and errors, once at the first covered
    // quad. Geometry and all per-pixel fixed-function operations remain below.
    const bool constant_mov=color_count<=1 && !depth_export && fp.size()==1 && (((fp[0].d>>24)&63)|((fp[0].s1>>31)<<6))==1 && (fp[0].s0&3)==2;
    host_phase.next(HostStage::certification);
    // Speculation performs reads only. If certification fails, discard its
    // vertex cache and restore access's hint before the original traversal.
    // In particular, do not expose an early vertex/tile/shader failure instead
    // of the sequential renderer's exact committed prefix and triangle count.
    const unsigned bands=(y1+1)/2-y0/2;
    const unsigned min_work=fp.size()>=8?4096:16384;
    unsigned thread_count=std::min(requested_threads,bands);
    bool parallel=!graphics_backend;
    auto reject=[&](bool condition,DrawReason reason){if(parallel && condition){parallel=false;perf.reason=reason;}};
    if(!graphics_backend){
    reject(point_draw || line_draw,DrawReason::primitive);
    reject(thread_count<=1,DrawReason::one_thread);
    reject(std::uint64_t(x1-x0)*(y1-y0)<min_work,DrawReason::small);
    reject(!parallel_fixed(depth_test,depth_func,alpha_test,alpha_func,blend,blend_sf,blend_df,blend_eq),DrawReason::fixed);
    reject(!parallel_fragment(fp,textures),DrawReason::fragment);
    reject(target_overlaps(color,depth,width*samples_x,height*samples_y) ||
        !parallel_target(color,width*samples_x,height*samples_y) || !parallel_target(depth,width*samples_x,height*samples_y),DrawReason::target);
    for(unsigned i=1;i<color_count;++i){
        const auto& t=colors[i];
        parallel=parallel && parallel_target(t,width*samples_x,height*samples_y) && !target_overlaps(t,depth,width*samples_x,height*samples_y);
        for(unsigned j=0;j<i;++j)if(target_overlaps(t,colors[j],width*samples_x,height*samples_y))parallel=false;
    }
    if(!parallel && perf.reason==DrawReason::empty)perf.reason=DrawReason::target;
    }
    auto aliases_target=[&](const void* p,std::uint64_t size){
        if(target_overlaps(depth,p,size,width*samples_x,height*samples_y))return true;
        for(unsigned i=0;i<color_count;++i)if(target_overlaps(colors[i],p,size,width*samples_x,height*samples_y))return true;
        return false;
    };
    // RPCS3 ffba2bf0 texture_cache_helpers.h:750-779 snapshots a bound
    // render target for strict framebuffer sampling (copy_image_dynamic).
    // decode_texture's untiled backing is that pre-draw snapshot, owned by
    // Texture through all workers. Never retile/refresh it during this draw.
    // VP texture reads can alias a writable surface. They must retain the
    // sequential first-use order relative to earlier primitive writes.
    if(parallel)for(unsigned pc=0;pc<544;++pc)
        if(std::as_const(gpu).transform_program_written[pc*4+1] && ((std::as_const(gpu).transform_program[pc*4+1]>>22)&31)==25){parallel=false;perf.reason=DrawReason::vertex_texture;break;}
    if(parallel)for(const auto& texture:textures)
        if(texture.valid && !texture.untiled && aliases_target(texture.storage,texture.storage_size)){parallel=false;perf.reason=DrawReason::texture_alias;break;}
    const auto saved_last_region=m.last_region;
    if(parallel){
        perf.reason=DrawReason::vertex_alias;
        try{
            // Attribute arrays can also alias VRAM through a main-memory host
            // mapping. Check host spans, rather than guest address inequalities.
            if(!indices.empty() && draw.immediate_vertices.empty()){
                const auto bounds=std::minmax_element(indices.begin(),indices.end());
                for(unsigned attr=0;attr<16 && parallel;++attr)if(input_mask&(1u<<attr)){
                    const auto format=reg(0x1740+4*attr);
                    const unsigned type=format&15,size=(format>>4)&15,stride=(format>>8)&255;
                    if(!size)continue;
                    if(format>>16 || size>4){parallel=false;break;}
                    const unsigned width=type==2?4:(type==1 || type==3 || type==5)?2:type==6?4:1;
                    const unsigned bytes=type==6?4:width*size;
                    const auto offset=reg(0x1680+4*attr);
                    const auto at=std::uint64_t(offset&0x7fffffff)+reg.get(0x1738,0)+std::uint64_t(stride)*(*bounds.first);
                    const auto span=std::uint64_t(stride)*(*bounds.second-*bounds.first)+bytes;
                    if(at+span>UINT64_C(0x100000000) || span>UINT_MAX){parallel=false;break;}
                    const auto address=location_address(gpu,offset>>31,static_cast<std::uint32_t>(at),static_cast<unsigned>(span));
                    const auto input=m.range(address,static_cast<unsigned>(span),false);
                    for(const auto& part:input.parts)if(aliases_target(part.data,part.size))parallel=false;
                }
            }
            m.last_region=saved_last_region;
            if(parallel)perf.reason=DrawReason::geometry;
            std::uint64_t work=0;
            if(parallel)for(const auto& tri:triangles){
                // Same first-use order as the original traversal. Workers use
                // const map lookups only; no GuestMemory or vertex interpreter.
                std::array<const ShadedVertex*,3> v{&vertex(tri[0]),&vertex(tri[1]),&vertex(tri[2])};
                std::array<float,3> X,Y;unsigned index=0;
                for(const auto* p:v){
                    if(!(p->clip[3]>0)){parallel=false;break;}
                    const float iw=1.f/p->clip[3];
                    const float x=p->clip[0]*iw*sx+vx,y=p->clip[1]*iw*sy+vy,z=p->clip[2]*iw*sz+vz;
                    if(!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z) ||
                       std::fabs(x)>1048576.f || std::fabs(y)>1048576.f){parallel=false;break;}
                    X[index]=x;Y[index]=y;++index;
                }
                if(!parallel)break;
                const float area=(X[1]-X[0])*(Y[2]-Y[0])-(X[2]-X[0])*(Y[1]-Y[0]);
                if(area==0)continue;
                const bool front=front_face==0x901?area<0:!(area<0);
                if(cull && (cull_face==0x408 || (cull_face==0x404 && front) || (cull_face==0x405 && !front)))continue;
                const int left=std::max<int>(x0,int(std::floor(std::min({X[0],X[1],X[2]}))));
                const int right=std::min<int>(x1,int(std::ceil(std::max({X[0],X[1],X[2]})))+1);
                const int top=std::max<int>(y0,int(std::floor(std::min({Y[0],Y[1],Y[2]}))));
                const int bottom=std::min<int>(y1,int(std::ceil(std::max({Y[0],Y[1],Y[2]})))+1);
                if(right>left && bottom>top)work=std::min<std::uint64_t>(min_work,work+std::uint64_t(right-left)*(bottom-top));
            }
            if(parallel && work<min_work){parallel=false;perf.reason=DrawReason::small_geometry;}
        }catch(const std::exception&){parallel=false;}
        if(!parallel){cache.clear();m.last_region=saved_last_region;}
    }
#ifdef ANYPS3_RSX_VK

    std::uint64_t* fallback_time=nullptr;
    if(graphics_backend){
        if(!gpu.renderer)gpu.renderer=std::make_unique<NativeVulkan>(s.rsx.host);
        std::unique_ptr<rsxgraphics::Session> owned;
        auto* session=static_cast<rsxgraphics::Session*>(gpu.graphics_session);
        if(!session){owned=std::make_unique<rsxgraphics::Session>(*gpu.renderer,s);session=owned.get();}
        // Materialize in the same attempt when a supported VP/alias path
        // needs CPU geometry. A rejected draw is never prepared a second time.
        auto materialize=[&]{if(lazy_geometry){build_indices();build_triangles();lazy_geometry=false;}};
        using Materializer=decltype(materialize);
        const rsxgraphics::GeometryFallback fallback{&materialize,[](void* p){(*static_cast<Materializer*>(p))();}};
        const bool done=rsxgraphics::prepared(*session,triangles,vertex,fp,textures,colors,depth,width,height,x0,y0,x1,y1,point_draw,line_draw?&line_geometry:nullptr,lazy_geometry?&*device_geometry:nullptr,fallback);
        if(!done)materialize();
        if(done){
            if(owned)session->flush(true,nullptr,0,"standalone_draw");
            ++draw.draws;draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;
        }
        session->flush(true,nullptr,0,"cpu_fallback");
        const auto* diagnostic=std::getenv("ANYPS3_RSX_GPU_FALLBACK");
        if(!diagnostic || std::string(diagnostic)!="1")throw std::runtime_error("RSX GPU draw unsupported: "+rsxgraphics::fallback_stats().last_rejection+"; diagnostic CPU fallback requires ANYPS3_RSX_GPU_FALLBACK=1");
        diagnostic_raw=std::make_unique<GuestMemory::RawPermit>(m);
        session->clear_surfaces();
        session->allow_resident_textures=false;
        for(unsigned unit=0;unit<16;++unit)if(textures[unit].resident_image)textures[unit]=decode_texture(s,reg,unit);
        session->allow_resident_textures=true;
        auto& stats=rsxgraphics::fallback_stats();if(stats.enabled){++stats.cpu_fallback_draws;fallback_time=&stats.cpu_fallback_ns;}
    }
    PerfTimer fallback_timer(fallback_time);
#endif
    for(const auto& t:textures)if(t.null_texture)throw std::runtime_error("RSX FP samples a disabled texture unit on the CPU renderer");
    // Only the Vulkan graphics fragment path models 0x1d7c (sample mask, alpha-to-coverage/one).
    if(reg.get(0x1d7c,0xffff0000)!=0xffff0000)throw std::runtime_error("unsupported RSX multisample coverage/alpha control on the CPU renderer");
    for(auto& t:textures)materialize_texture(s,t);
    // Diagnostic only (ANYPS3_RSX_FEEDBACK_TRACE=1): describe draws whose contiguous texture bytes alias a bound
    // target: texture/target layout, primitive coverage overlap and the last NV4097_INVALIDATE_L2.
    static const bool feedback_trace=[]{const auto* v=std::getenv("ANYPS3_RSX_FEEDBACK_TRACE");return v && std::string(v)=="1";}();
    static unsigned feedback_lines=0;
    if(feedback_trace && feedback_lines<600){
        bool any=false;for(const auto& t:textures)if(t.valid && !t.untiled && aliases_target(t.storage,t.storage_size))any=true;
        if(any){++feedback_lines;std::ostringstream out;
            out<<"RSX FEEDBACK TRACE draw="<<draw.draws<<" pc=0x"<<std::hex<<s.pc<<std::dec<<" primitive="<<draw.primitive<<" triangles="<<triangles.size()
               <<" surface=0x"<<std::hex<<reg(0x208)<<" clip_h=0x"<<reg(0x200)<<" clip_v=0x"<<reg(0x204)<<std::dec<<" scissor=["<<x0<<","<<y0<<","<<x1<<","<<y1<<"]"
               <<" blend="<<(reg(0x310)&1)<<" depth_test="<<(reg(0xa74)&1)<<" fp="<<fp.size()<<" samples="<<samples_x*samples_y
               <<" l2_invalidations="<<gpu.texture_invalidations<<" draws_since_l2="<<(draw.draws-gpu.texture_invalidation_draw)<<" l2_value=0x"<<std::hex<<gpu.texture_invalidation_value<<std::dec;
            for(unsigned i=0;i<color_count;++i)if(colors[i].base)out<<" color"<<i<<"={off:0x"<<std::hex<<colors[i].offset<<",pitch:0x"<<colors[i].pitch<<std::dec<<",bpp:"<<colors[i].bpp<<",tile:"<<bool(colors[i].tile)<<",swz:"<<colors[i].swizzle_width<<",span:0x"<<std::hex<<target_span(colors[i],width*samples_x,height*samples_y)<<std::dec<<"}";
            if(depth.base)out<<" depth={off:0x"<<std::hex<<depth.offset<<",pitch:0x"<<depth.pitch<<std::dec<<",tile:"<<bool(depth.tile)<<"}";
            for(unsigned u=0;u<16;++u){const auto& t=textures[u];if(!t.valid)continue;
                const auto base=0x1a00+0x20*u;
                out<<" tex"<<u<<"={off:0x"<<std::hex<<reg(base)<<",fmt:0x"<<reg(base+4)<<",ctl0:0x"<<reg(base+0xc)<<",filter:0x"<<reg(base+0x14)<<",rect:0x"<<reg(base+0x18)<<std::dec<<",w:"<<t.levels[0].width<<",h:"<<t.levels[0].height<<",pitch:"<<t.levels[0].pitch<<",levels:"<<t.levels.size()
                   <<",untiled:"<<bool(t.untiled)<<",alias:"<<(!t.untiled && aliases_target(t.storage,t.storage_size));
                if(color_count && colors[0].base && !colors[0].mapping)out<<",rel0:"<<(std::int64_t(reinterpret_cast<std::uintptr_t>(t.storage))-std::int64_t(reinterpret_cast<std::uintptr_t>(colors[0].base)))<<",size:"<<t.storage_size;
                out<<"}";}
            // Pixel-center coverage (strict edges, so a shared edge never counts twice).
            if(!point_draw && !line_draw && x1>x0 && y1>y0){
                std::vector<std::uint8_t> hits(std::size_t(x1-x0)*(y1-y0));std::uint64_t covered=0,multi=0;
                for(const auto& tri:triangles){std::array<float,3> X{},Y{};bool ok=true;
                    for(unsigned k=0;k<3;++k){const auto& v=vertex(tri[k]);if(!(v.clip[3]>0)){ok=false;break;}const float iw=1.f/v.clip[3];X[k]=v.clip[0]*iw*sx+vx;Y[k]=v.clip[1]*iw*sy+vy;}
                    if(!ok){out<<" eye_crossing=1";continue;}
                    const float area=(X[1]-X[0])*(Y[2]-Y[0])-(X[2]-X[0])*(Y[1]-Y[0]);if(area==0)continue;
                    const int l=std::max<int>(x0,int(std::floor(std::min({X[0],X[1],X[2]})))),rr=std::min<int>(x1,int(std::ceil(std::max({X[0],X[1],X[2]})))+1);
                    const int t=std::max<int>(y0,int(std::floor(std::min({Y[0],Y[1],Y[2]})))),b=std::min<int>(y1,int(std::ceil(std::max({Y[0],Y[1],Y[2]})))+1);
                    if(feedback_lines<=40 && &tri-triangles.data()<8)out<<" tri=["<<X[0]<<","<<Y[0]<<" "<<X[1]<<","<<Y[1]<<" "<<X[2]<<","<<Y[2]<<"]";
                    for(int y=t;y<b;++y)for(int x=l;x<rr;++x){const float px=x+.5f,py=y+.5f;bool in=true;
                        for(unsigned k=0;k<3 && in;++k){const unsigned n=(k+1)%3;const float e=(X[n]-X[k])*(py-Y[k])-(Y[n]-Y[k])*(px-X[k]);in=area>0?e>0:e<0;}
                        if(in){auto& h=hits[std::size_t(y-y0)*(x1-x0)+(x-x0)];if(h==1)++multi;if(!h)++covered;if(h<255)++h;}}
                }
                out<<" covered="<<covered<<" overlap_pixels="<<multi;
            }
            std::fprintf(stderr,"%s\n",out.str().c_str());
        }
    }
    // Fragment textures whose contiguous bytes alias a bound target sample a
    // pre-draw snapshot, like gathered textures (untiled backing above): the
    // RSX texture cache is not coherent with ROP writes within a draw (games
    // invalidate it with NV4097_INVALIDATE_L2 0x1fd8 between passes), and
    // RPCS3 ffba2bf0 samples a bound target through a pre-draw copy
    // (texture_cache_helpers.h:750-779). The Vulkan path copies the same bytes.
    for(auto& t:textures)if(t.valid && !t.untiled && aliases_target(t.storage,t.storage_size)){
        const auto* first=t.levels[0].data-t.levels[0].offset;
        const auto* end=t.storage+t.storage_size;
        if(first<t.storage || first>=end)throw std::runtime_error("RSX feedback texture outside its storage envelope");
        const auto size=std::uint64_t(end-first);if(size>UINT_MAX)throw std::runtime_error("RSX feedback texture snapshot too large");
        auto backing=std::make_shared<GuestBacking>(size);
        if(s.memory.concurrent() || HostByteAccess::active())pinned_host_read(s.memory,first,backing->data(),unsigned(size));
        else std::memcpy(backing->data(),first,size);
        for(auto& level:t.levels)level.data=backing->data()+(level.data-first);
        t.untiled=std::move(backing);
    }
    // Cache only stable texture bytes (aliasing textures are snapshots now).
    if(std::uint64_t(x1-x0)*(y1-y0)>=4096)for(auto& texture:textures)
        if(texture.valid && (texture.untiled || !aliases_target(texture.storage,texture.storage_size)))prepare_texture_cache(texture);
#ifdef ANYPS3_RSX_VK

    if(parallel && !std::strcmp(backend,"compute")){
        if(!gpu.renderer)gpu.renderer=std::make_unique<NativeVulkan>(s.rsx.host);
        std::uint64_t count=0;
        if(rsxvk::prepared(*gpu.renderer,s,triangles,vertex,fp,textures,colors[0],depth,width,height,x0,y0,x1,y1,count,&colors)){
            if(reg.get(0x17cc,0))gpu.report_values[0]+=count;
            draw.pixels+=count;++draw.draws;
            draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;
        }
    }
#endif
    // Every stripe owns its address cache, lazy constants, FP/temp registers
    // and counters. Full 2x2 helper quads stay intact at stripe boundaries.
    auto rasterize=[&](unsigned begin,unsigned end,bool striped,unsigned left,unsigned right){
    PerfTimer raster_timer(perf_work?&perf_work->raster_ns:nullptr);
    auto color_target_views=colors;Target depth_target_view=depth;
    std::optional<Vec4> constant_output;
    std::optional<std::array<std::uint8_t,4>> constant_bytes;
    // ---- per triangle
    std::uint64_t shaded=0,triangle_count=0;
    for(const auto& tri:triangles){
        const auto& ready=cache;
        std::array<const ShadedVertex*,3> v{striped?&ready.at(tri[0]):&vertex(tri[0]),
            striped?&ready.at(tri[1]):&vertex(tri[1]),striped?&ready.at(tri[2]):&vertex(tri[2])};
        const auto original=v;
        const auto line_it=line_geometry.find(tri[0]);
        const LineGeometry* line=line_it==line_geometry.end()?nullptr:&line_it->second;
        auto polygon=clip_polygon(v,point_draw?0:reg.get(0x1478,0),output_mask,sz,vz,clip_min,clip_max);
        for(std::size_t fan=1;fan+1<polygon.size();++fan){
        v={&polygon[0],&polygon[fan],&polygon[fan+1]};
        std::array<float,3> X,Y,Z,W;
        for(unsigned i=0;i<3;++i){
            const float iw=1.f/v[i]->clip[3];
            X[i]=v[i]->clip[0]*iw*sx+vx;Y[i]=v[i]->clip[1]*iw*sy+vy;Z[i]=v[i]->clip[2]*iw*sz+vz;W[i]=iw;
        }
        const float area=(X[1]-X[0])*(Y[2]-Y[0])-(X[2]-X[0])*(Y[1]-Y[0]);
        if(area==0)continue;
        // Window y grows downward; a counter-clockwise triangle in GL window
        // space (y up) has negative area here.
        const bool ccw=area<0;
        const bool front=point_draw || line_draw || (front_face==0x901?ccw:!ccw);
        if(cull){
            if(cull_face==0x408 || (cull_face==0x404 && front) || (cull_face==0x405 && !front))continue;
        }
        const auto& stencil_state=front?front_stencil:back_stencil;
        float polygon_offset=0;
        if(offset_fill){
            const float dzdx=((Z[1]-Z[0])*(Y[2]-Y[0])-(Z[2]-Z[0])*(Y[1]-Y[0]))/area;
            const float dzdy=((X[1]-X[0])*(Z[2]-Z[0])-(X[2]-X[0])*(Z[1]-Z[0]))/area;
            polygon_offset=std::max(std::fabs(dzdx),std::fabs(dzdy))*offset_factor+offset_units/float(depth_max);
        }
        ++triangle_count;if(!striped)++draw.triangles;
        const int minx=std::max<int>(x0,int(std::floor(std::min({X[0],X[1],X[2]})))),maxx=std::min<int>(int(x1)-1,int(std::ceil(std::max({X[0],X[1],X[2]}))));
        const int miny=std::max<int>(y0,int(std::floor(std::min({Y[0],Y[1],Y[2]})))),maxy=std::min<int>(int(y1)-1,int(std::ceil(std::max({Y[0],Y[1],Y[2]}))));
        const float sign=area>0?1.f:-1.f;
        auto edge=[&](unsigned a,unsigned b,float px,float py){return ((X[b]-X[a])*(py-Y[a])-(Y[b]-Y[a])*(px-X[a]))*sign;};
        auto top_left=[&](unsigned a,unsigned b){
            const float dx=(X[b]-X[a])*sign,dy=(Y[b]-Y[a])*sign;
            return (dy==0 && dx>0) || dy<0; // top edge or left edge in y-down space
        };
        const bool tl0=top_left(1,2),tl1=top_left(2,0),tl2=top_left(0,1);
        for(int qy=std::max(miny&~1,int(begin));qy<=std::min(maxy,int(end)-1);qy+=2)for(int qx=std::max(minx&~1,int(left));qx<=std::min(maxx,int(right)-1);qx+=2){
            std::array<bool,4> covered{};std::array<unsigned,4> coverage{};std::array<float,4> lb0{},lb1{},lb2{};
            bool any=false;
            for(unsigned l=0;l<4;++l){
                const int px=qx+(l&1),py=qy+(l>>1);
                const float cx=px+0.5f,cy=py+0.5f;
                const float e0=edge(1,2,cx,cy),e1=edge(2,0,cx,cy),e2=edge(0,1,cx,cy);
                const float sum=e0+e1+e2;lb0[l]=e0/sum;lb1[l]=e1/sum;lb2[l]=e2/sum;
                if(line){
                    const float t=std::clamp(((cx-line->x)*line->dx+(cy-line->y)*line->dy)/(line->dx*line->dx+line->dy*line->dy),0.f,1.f);
                    lb0[l]=1-t;lb1[l]=t;lb2[l]=0;
                }
                if(samples==1){
                    if(e0>=0 && e1>=0 && e2>=0 && !((e0==0&&!tl0)||(e1==0&&!tl1)||(e2==0&&!tl2)))coverage[l]=1;
                }else for(unsigned sample=0;sample<samples;++sample){
                    const float a=edge(1,2,px+positions[sample][0],py+positions[sample][1]);
                    const float b=edge(2,0,px+positions[sample][0],py+positions[sample][1]);
                    const float c=edge(0,1,px+positions[sample][0],py+positions[sample][1]);
                    if(a>=0 && b>=0 && c>=0 && !((a==0&&!tl0)||(b==0&&!tl1)||(c==0&&!tl2)))coverage[l]|=1u<<sample;
                }
                bool inside=px>=minx && px<=maxx && py>=miny && py<=maxy && coverage[l];
                if(inside && line){
                    // OpenGL diamond-exit rule, including exclusion of the final
                    // endpoint. Perturb (-epsilon,-epsilon^2) resolves ties.
                    bool hit=false;const bool xmajor=std::fabs(line->dx)>=std::fabs(line->dy);
                    for(unsigned row=0;row<line->width && !hit;++row){
                        const double shift=double(row)-double(line->width-1)*.5;
                        const double x=line->x-cx-(xmajor?0:shift)-1.e-7,y=line->y-cy-(xmajor?shift:0)-1.e-14;
                        double enter=0,leave=1;
                        for(int plane=0;plane<4;++plane){
                            const int xs=plane&1?1:-1,ys=plane&2?1:-1;
                            const double q=.5-xs*x-ys*y,d=xs*double(line->dx)+ys*double(line->dy);
                            if(d>0)leave=std::min(leave,q/d);else if(d<0)enter=std::max(enter,q/d);else if(q<0)leave=-1;
                        }
                        hit=leave>enter && leave<1 && leave>0;
                    }
                    inside=hit;
                }
                if(inside && !point_draw && !line_draw && stipple)inside=(stipple_rows[unsigned(py)&31]>>(unsigned(px)&31))&1;
                covered[l]=inside && window_visible(px,py);any|=covered[l];
            }
            if(!any)continue;
            if(perf_work)++perf_work->quads;
            std::optional<std::array<FpContext,4>> quad;std::array<float,4> lz{};
            if(!constant_mov)quad.emplace();
            for(unsigned l=0;l<4;++l){
                const float b0=lb0[l],b1=lb1[l],b2=lb2[l];
                lz[l]=b0*Z[0]+b1*Z[1]+b2*Z[2];
                if(constant_mov)continue;
                const float iw=b0*W[0]+b1*W[1]+b2*W[2];
                const float p0=b0*W[0]/iw,p1=b1*W[1]/iw,p2=b2*W[2]/iw;
                auto varying=[&](unsigned o){
                    if(two_sided && !front && (o==1 || o==2))o+=2;
                    if(shade_flat && (o>=1 && o<=4))return line?line->provoking[o]:original[reg.get(0x1454,0)?0:2]->o[o];
                    Vec4 r;for(unsigned c=0;c<4;++c)r[c]=p0*v[0]->o[o][c]+p1*v[1]->o[o][c]+p2*v[2]->o[o][c];return r;
                };
                auto& ctx=(*quad)[l];ctx.state=&s;ctx.textures=&textures;
                if(used_inputs&1)ctx.inputs[0]={qx+(l&1)+0.5f,qy+(l>>1)+0.5f,lz[l],iw};
                if(used_inputs&2)ctx.inputs[1]=varying(1);
                if(used_inputs&4)ctx.inputs[2]=varying(2);
                if(used_inputs&8){auto f=varying(5);ctx.inputs[3]=fog_configured?fog_value(reg(0x8cc),f[0],bits_float(reg.get(0x8d0,0)),bits_float(reg.get(0x8d4,0))):Vec4{f[0],0,0,0};}
                for(unsigned t=0;t<8;++t)if(used_inputs&(1u<<(4+t)))ctx.inputs[4+t]=varying(7+t);
                if(used_inputs&(1u<<12))ctx.inputs[12]=varying(15);
                if(used_inputs&(1u<<13))ctx.inputs[13]=varying(6);
                for(unsigned t=0;t<10;++t)if((used_inputs&(1u<<(4+t))) && (reg.get(0xb40+4*t,0)&1)){
                    ctx.inputs[4+t][2]=0;ctx.inputs[4+t][3]=1.f/iw;
                }
                if(used_inputs&(1u<<14))ctx.inputs[14]={front?1.f:-1.f,0,0,0};
            }
            if(constant_mov){
                if(!constant_output){
                    FpContext ctx;run_fragment_program(fp,ctx);
                    constant_output=half_output?ctx.read_h(output_reg):ctx.read_r(output_reg);
                }
            }else if(!fp.empty())run_fragment_quad(fp,*quad);
            for(unsigned l=0;l<4;++l){
            if(!covered[l])continue;
            const int px=qx+(l&1),py=qy+(l>>1);
            for(unsigned sample=0;sample<samples;++sample){
            if(!(coverage[l]&(1u<<sample)))continue;
            const float cx=px+positions[sample][0],cy=py+positions[sample][1];
            // Keep the pre-existing binary32 operation order for 1x surfaces.
            float z=(samples==1?lz[l]:
                (edge(1,2,cx,cy)*Z[0]+edge(2,0,cx,cy)*Z[1]+edge(0,1,cx,cy)*Z[2])/std::fabs(area))+polygon_offset;
            const unsigned storage_x=unsigned(px)*samples_x+(sample%samples_x),storage_y=unsigned(py)*samples_y+sample/samples_x;
            if(z<clip_min || z>clip_max)continue;
            if(depth_export)z=constant_mov?0.f:(*quad)[l].read_r(1)[2];
            std::uint32_t stored_depth=0;std::uint8_t* depth_ptr=nullptr;
            if(use_depth){
                depth_ptr=depth_target_view.at(storage_x,storage_y);
                stored_depth=read_be(depth_ptr,depth_bpp==2?2:3);
            }
            const std::uint32_t z24=use_depth?static_cast<std::uint32_t>(std::lround(std::min(1.f,std::max(0.f,z))*float(depth_max))):0;
            Vec4 out{0,0,0,0};
            if(constant_mov)out=*constant_output;
            else if(!fp.empty()){
                if((*quad)[l].killed)continue;
                out=half_output?(*quad)[l].read_h(output_reg):(*quad)[l].read_r(output_reg);
            }
            if(alpha_test){
                if(!compare(alpha_func,std::min(1.f,std::max(0.f,out[3])),alpha_ref))continue;
            }
            // Killed fragments and alpha failures have no stencil/depth effects.
            // Depth bounds compares the existing depth sample, before stencil.
            if(bounds && (float(stored_depth)/float(depth_max)<bounds_min || float(stored_depth)/float(depth_max)>bounds_max))continue;
            if(stencil && !stencil_state.test(depth_ptr[3])){
                stencil_state.apply(m,depth_ptr+3,stencil_state.fail,depth.observed);depth_target_view.commit(storage_x,storage_y);continue;
            }
            if(depth_test && !compare(depth_func,float(z24),float(stored_depth))){
                if(stencil){stencil_state.apply(m,depth_ptr+3,stencil_state.zfail,depth.observed);depth_target_view.commit(storage_x,storage_y);}
                continue;
            }
            if(stencil)stencil_state.apply(m,depth_ptr+3,stencil_state.zpass,depth.observed);
            if(use_depth && depth_write){
                if(depth.observed)m.mark_write(depth_ptr,depth_bpp==2?2:3);
                write_be(depth_ptr,depth_bpp==2?2:3,z24);
            }
            if(use_depth && (stencil || depth_write))depth_target_view.commit(storage_x,storage_y);
            for(unsigned target=0;target<color_count;++target){
                auto& color_target_view=color_target_views[target];
                const auto color_mask=color_masks[target];const bool blend=blend_mask&(1u<<target);
                if(target){const unsigned r=output_regs[target];out=half_output?(*quad)[l].read_h(r*2):(*quad)[l].read_r(r);}
                auto* p=color_target_view.at(storage_x,storage_y);
                if(constant_mov && !blend && !logic && color_format==8){
                    if(!constant_bytes){
                        std::array<std::uint8_t,4> bytes;
                        for(unsigned c=0;c<4;++c){
                            float value=std::clamp(out[c],0.f,1.f);
                            if(framebuffer_srgb && c<3)value=linear_to_srgb(value);
                            bytes[c]=static_cast<std::uint8_t>(std::lround(std::clamp(value,0.f,1.f)*255.f));
                        }
                        constant_bytes=bytes;
                    }
                    color_target_view.mark(m,storage_x,storage_y,p);
                    if(color_mask&0x01000000)p[0]=(*constant_bytes)[3];
                    if(color_mask&0x00010000)p[1]=(*constant_bytes)[0];
                    if(color_mask&0x00000100)p[2]=(*constant_bytes)[1];
                    if(color_mask&0x00000001)p[3]=(*constant_bytes)[2];
                    color_target_view.commit(storage_x,storage_y);
                    continue;
                }
                Vec4 src=out;if(!float_color(color_format))for(auto& c:src)c=std::min(1.f,std::max(0.f,c));
                Vec4 dst=read_color(p,color_format);
                if(framebuffer_srgb && blend)for(unsigned c=0;c<3;++c)dst[c]=srgb_to_linear(dst[c]);
                Vec4 result=src;
                if(blend){
                    const auto sf=blend_sf,df=blend_df,eq=blend_eq,bc=blend_colour;
                    const auto bc2=blend_colour2;
                    const Vec4 constant=color_format==11?Vec4{half_float(bc&65535),half_float(bc>>16),half_float(bc2&65535),half_float(bc2>>16)}:
                        Vec4{((bc>>16)&255)/255.f,((bc>>8)&255)/255.f,(bc&255)/255.f,(bc>>24)/255.f};
                    for(unsigned c=0;c<4;++c){
                        const unsigned sfc=c==3?sf>>16:sf&0xffff,dfc=c==3?df>>16:df&0xffff,e=c==3?eq>>16:eq&0xffff;
                        const float a=src[c]*blend_factor(sfc,src,dst,constant,c),b=dst[c]*blend_factor(dfc,src,dst,constant,c);
                        float r;
                        switch(e){case 0x8006:r=a+b;break;case 0x800a:r=a-b;break;case 0x800b:r=b-a;break;
                                  case 0x8007:r=std::min(src[c],dst[c]);break;case 0x8008:r=std::max(src[c],dst[c]);break;
                                  default:throw std::runtime_error("unsupported RSX blend equation "+std::to_string(e));}
                        result[c]=float_color(color_format)?r:std::min(1.f,std::max(0.f,r));
                    }
                }
                if(framebuffer_srgb)for(unsigned c=0;c<3;++c)result[c]=std::clamp(linear_to_srgb(result[c]),0.f,1.f);
                color_target_view.mark(m,storage_x,storage_y,p);
                write_color(p,color_format,result,color_mask,logic,logic_func);
                color_target_view.commit(storage_x,storage_y);
            }
            ++shaded;
            }
            }
        }
    }
    }
    return std::make_pair(shaded,triangle_count);
    };
    std::uint64_t shaded=0;
    if(parallel){
        // A tile owns complete helper quads, all samples, and visits primitives
        // in original order. Dynamic work stealing handles small localized
        // geometry without assigning nearly-empty full-height stripes.
        constexpr unsigned tile_width=64,tile_height=16;
        const unsigned tile_left=x0&~1u,tile_top=y0&~1u;
        const unsigned columns=(x1-tile_left+tile_width-1)/tile_width,rows=(y1-tile_top+tile_height-1)/tile_height;
        const unsigned tasks=columns*rows;thread_count=std::min(thread_count,tasks);
        static thread_local DrawPool pool;
        std::vector<std::pair<std::uint64_t,std::uint64_t>> counts(thread_count);
        std::vector<PerfWork> metrics(perf.stats.enabled?thread_count:0);
        std::atomic<unsigned> next{thread_count};
        const bool dispatched=pool.run(thread_count,[&](unsigned i){
            PerfBind bind(perf.stats.enabled?&metrics[i]:nullptr);
            for(unsigned task=i;task<tasks;task=next.fetch_add(1,std::memory_order_relaxed)){
                const unsigned first=tile_top+(task/columns)*tile_height,last=std::min(y1,first+tile_height);
                const unsigned left=tile_left+(task%columns)*tile_width,right=std::min(x1,left+tile_width);
                const auto count=rasterize(first,last,true,left,right);
                counts[i].first+=count.first;
                if(task==0)counts[0].second=count.second;
            }
        });
        if(dispatched){
            for(const auto& count:counts)shaded+=count.first;
            for(const auto& metric:metrics)perf.work.add(metric);
            draw.triangles+=counts[0].second;perf.reason=DrawReason::parallel;
        }else{
            cache.clear();m.last_region=saved_last_region;perf.reason=DrawReason::pool;
            shaded=rasterize(0,height+1,false,0,width+1).first;
        }
    }else shaded=rasterize(0,height+1,false,0,width+1).first;
    perf.pixels=shaded;
    if(reg.get(0x17cc,0))gpu.report_values[0]+=shaded;
    draw.pixels+=shaded;++draw.draws;draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();
    if(gpu.trace || draw.draws<=16 || draw.draws%256==0)
        std::fprintf(stderr,"native RSX draw=%llu primitive=%u vertices=%zu triangles=%zu pixels=%llu\n",
            static_cast<unsigned long long>(draw.draws),draw.primitive,indices.size(),triangles.size(),static_cast<unsigned long long>(shaded));
    }catch(const std::runtime_error& error){
#ifdef ANYPS3_RSX_VK
        // A false deferred predicate also suppresses guest input validation.
        // Resolve only this exceptional path; a true predicate rethrows the
        // original unsupported/memory error and never falls through.
        const std::string message=error.what();
        const bool guest_validation=message.rfind("unsupported RSX FP opcode ",0)==0 || message.rfind("RSX GPU draw unsupported: ",0)==0 ||
            message.rfind("unmapped or split guest memory access",0)==0 || message=="guest memory permission fault" ||
            message=="RSX fragment reads a masked vertex output";
        // Host/Vulkan failures can leave a partially recorded command and
        // must never be submitted merely to resolve an unrelated predicate.
        if(guest_validation && rsx_graphics_conditional_fault(s)){draw.ranges.clear();draw.immediate_vertices.clear();draw.elements.clear();return;}
#endif
        throw;
    }
}
#pragma GCC pop_options
'''
