"""Content index for existing AOT semantics at arbitrary LS coordinates.

Only original image/overlay bytes are indexed. A match selects coordinates,
not instruction semantics; all execution and word guards remain in spu.lift.
The index lives in program.cpp and is never duplicated into SPU units.
"""

CPP = r'''
struct SpuRelocSource {unsigned image,pc,size;const char* bytes;};
class SpuRelocIndex {
    // Every 4-byte-aligned window of the indexed sources is a candidate (image, pc) whose `size` bytes (16, or 12/8/4 at a source
    // end) must equal LS at the queried pc; among matches the first in (size desc, image, pc) order wins. Full 16-byte windows are
    // sorted by their bytes (then image, pc), so the best full match is one binary search instead of a linear scan of every candidate
    // sharing the first word (common prologue words have thousands). Shorter tail windows keep the word-keyed scan; they can only
    // win when no full window matches, exactly as in the single (size desc, image, pc) order.
    struct Full {std::uint64_t key0,key1;std::uint32_t image,pc;};
    struct Candidate {std::uint32_t word,image,pc,size;const char* bytes;};
    std::vector<Full> full;std::vector<Candidate> tails;
    static std::uint64_t key(const void* p){std::uint64_t v;std::memcpy(&v,p,8);return v;}
public:
    SpuRelocIndex(std::initializer_list<SpuRelocSource> sources):SpuRelocIndex(sources.begin(),sources.end()){}
    SpuRelocIndex(const SpuRelocSource* begin,const SpuRelocSource* end){
        struct Range {const SpuRelocSource *b,*e;const SpuRelocSource* begin()const{return b;}const SpuRelocSource* end()const{return e;}};
        for(const auto& source:Range{begin,end})for(unsigned offset=0;offset+4<=source.size;offset+=4){
            const auto* bytes=source.bytes+offset;const unsigned size=std::min(16u,source.size-offset)&~3u;
            if(size==16)full.push_back({key(bytes),key(bytes+8),source.image,source.pc+offset});
            else tails.push_back({spu_load_word(reinterpret_cast<const std::uint8_t*>(bytes)),source.image,source.pc+offset,size,bytes});
        }
        std::sort(full.begin(),full.end(),[](const auto& a,const auto& b){
            if(a.key0!=b.key0)return a.key0<b.key0;
            if(a.key1!=b.key1)return a.key1<b.key1;
            if(a.image!=b.image)return a.image<b.image;
            return a.pc<b.pc;
        });
        std::sort(tails.begin(),tails.end(),[](const auto& a,const auto& b){
            if(a.word!=b.word)return a.word<b.word;
            if(a.size!=b.size)return a.size>b.size;
            if(a.image!=b.image)return a.image<b.image;
            return a.pc<b.pc;
        });
    }
    bool find(const std::uint8_t* ls,unsigned pc,std::uint32_t& original,std::uint32_t& image)const{
        if(pc<=0x40000-16){
            const auto k0=key(ls+pc),k1=key(ls+pc+8);
            const auto it=std::lower_bound(full.begin(),full.end(),std::make_pair(k0,k1),
                [](const Full& row,const std::pair<std::uint64_t,std::uint64_t>& k){return row.key0<k.first || (row.key0==k.first && row.key1<k.second);});
            if(it!=full.end() && it->key0==k0 && it->key1==k1){original=it->pc;image=it->image;return true;}
        }
        const auto word=spu_load_word(ls+pc);
        auto it=std::lower_bound(tails.begin(),tails.end(),word,
            [](const auto& row,std::uint32_t key){return row.word<key;});
        for(;it!=tails.end() && it->word==word;++it){
            if(it->size<=0x40000-pc && !std::memcmp(ls+pc,it->bytes,it->size)){
                original=it->pc;image=it->image;return true;
            }
        }
        return false;
    }
};
// Published counters are safe to sample from the PPU with SPU lanes active.
[[maybe_unused]] static void spu_dynamic_perf_report(const PpuState& s){
    std::uint64_t relocated=0,interpreted=0;
    for(const auto& group:s.spu.groups)for(const auto& t:group.threads){
        relocated+=t.relocated_entries.value.load(std::memory_order_relaxed);
        interpreted+=t.interpreted_instructions.value.load(std::memory_order_relaxed);
    }
    std::fprintf(stderr,"PERF SPU_DYNAMIC relocated_entries=%" PRIu64 " interpreted_instructions=%" PRIu64 "\n",relocated,interpreted);
    if(!spu_stats_on)return;
    static constexpr const char* names[]={"rchcnt0","rchcnt0_zero","rdch0","getllar","putllc","putlluc","slices","sleeps",
        "native_calls","cache_hits","interpreter_calls","blocked","collect","wrch","rdch","rchcnt","dma","native_miss","declines","steps",
        "slot_stale","cand_try","cand_fail","reloc_find","reloc_hit","wake_lat_us","block_enter","wake_count"};
    for(const auto& group:s.spu.groups)for(const auto& t:group.threads){
        std::fprintf(stderr,"PERF SPU_STATS id=%x",t.id);
        for(unsigned i=0;i<28;++i)std::fprintf(stderr," %s=%" PRIu64,names[i],i==19?t.published_steps.value.load(std::memory_order_relaxed):t.stats[i].value.load(std::memory_order_relaxed));
        std::fputc('\n',stderr);
        if(const auto table=t.native_entries){
            std::vector<std::uint64_t> rows;for(const auto& x:*table)if(const auto v=x.load(std::memory_order_relaxed))rows.push_back(v);
            std::sort(rows.begin(),rows.end(),[](auto a,auto b){return (a&0xffffffffu)>(b&0xffffffffu);});
            std::fprintf(stderr,"PERF SPU_NATIVE_ENTRIES id=%x",t.id);
            for(std::size_t i=0;i<rows.size() && i<24;++i)std::fprintf(stderr," %05x:%u",unsigned((rows[i]>>32)-1),unsigned(rows[i]&0xffffffffu));
            std::fputc('\n',stderr);
        }
        if(const auto table=t.interp_entries){
            std::vector<std::uint64_t> rows;for(const auto& x:*table)if(const auto v=x.load(std::memory_order_relaxed))rows.push_back(v);
            std::sort(rows.begin(),rows.end(),[](auto a,auto b){return (a&0xffffffffu)>(b&0xffffffffu);});
            std::fprintf(stderr,"PERF SPU_INTERP_ENTRIES id=%x",t.id);
            for(std::size_t i=0;i<rows.size() && i<40;++i)std::fprintf(stderr," %05x:%u",unsigned((rows[i]>>32)-1),unsigned(rows[i]&0xffffffffu));
            std::fputc('\n',stderr);
        }
    }
}
'''


def generate_index(images):
    lines = [CPP]
    records = []
    blobs = {}
    lines.append('static bool spu_reloc_find(NativeSpu::Thread& t,const std::uint8_t* ls,std::uint32_t& original){')
    for i, sources in enumerate(images):
        unique = list(dict.fromkeys((base, bytes(payload)) for base, payload in sources if len(payload)>=4))
        for base, payload in unique:
            if payload not in blobs:
                name = f'bytes_{len(blobs)}'
                blobs[payload] = name
                lines.append(f'static constexpr char {name}[]=')
                for offset in range(0,len(payload),256):
                    lines.append('"'+''.join(f'\\x{byte:02x}' for byte in payload[offset:offset+256])+'"')
                lines.append(';')
            records.append(f'{{{i},0x{base:x},{len(payload)},{blobs[payload]}}}')
    lines.append('static const SpuRelocIndex index({'+','.join(records)+'});')
    lines.append('return index.find(ls,t.pc,original,t.reloc_image_index);}')
    return '\n'.join(lines)
