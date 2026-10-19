"""Content index for existing AOT semantics at arbitrary LS coordinates.

Only original image/overlay bytes are indexed. A match selects coordinates,
not instruction semantics; all execution and word guards remain in spu.lift.
The index lives in program.cpp and is never duplicated into SPU units.
"""

CPP = r'''
struct SpuRelocSource {unsigned image,pc,size;const char* bytes;};
class SpuRelocIndex {
    struct Candidate {std::uint32_t word,image,pc,size;const char* bytes;};
    std::vector<Candidate> candidates;
public:
    SpuRelocIndex(std::initializer_list<SpuRelocSource> sources){
        std::size_t count=0;for(const auto& source:sources)count+=source.size/4;
        candidates.reserve(count);
        for(const auto& source:sources)for(unsigned offset=0;offset+4<=source.size;offset+=4){
            const auto* bytes=source.bytes+offset;
            candidates.push_back({spu_load_word(reinterpret_cast<const std::uint8_t*>(bytes)),
                source.image,source.pc+offset,std::min(16u,source.size-offset)&~3u,bytes});
        }
        std::sort(candidates.begin(),candidates.end(),[](const auto& a,const auto& b){
            if(a.word!=b.word)return a.word<b.word;
            if(a.size!=b.size)return a.size>b.size;
            if(a.image!=b.image)return a.image<b.image;
            return a.pc<b.pc;
        });
    }
    bool find(const std::uint8_t* ls,unsigned pc,std::uint32_t& original,std::uint32_t& image)const{
        const auto word=spu_load_word(ls+pc);
        auto it=std::lower_bound(candidates.begin(),candidates.end(),word,
            [](const auto& row,std::uint32_t key){return row.word<key;});
        for(;it!=candidates.end() && it->word==word;++it){
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
        "native_calls","cache_hits","interpreter_calls","blocked","collect","wrch","rdch","rchcnt","dma","native_miss","declines","steps"};
    for(const auto& group:s.spu.groups)for(const auto& t:group.threads){
        std::fprintf(stderr,"PERF SPU_STATS id=%x",t.id);
        for(unsigned i=0;i<20;++i)std::fprintf(stderr," %s=%" PRIu64,names[i],i==19?t.published_steps.value.load(std::memory_order_relaxed):t.stats[i].value.load(std::memory_order_relaxed));
        std::fputc('\n',stderr);
        if(const auto table=t.interp_entries){
            std::vector<std::uint64_t> rows;for(const auto& x:*table)if(const auto v=x.load(std::memory_order_relaxed))rows.push_back(v);
            std::sort(rows.begin(),rows.end(),[](auto a,auto b){return (a&0xffffffffu)>(b&0xffffffffu);});
            std::fprintf(stderr,"PERF SPU_INTERP_ENTRIES id=%x",t.id);
            for(std::size_t i=0;i<rows.size() && i<12;++i)std::fprintf(stderr," %05x:%u",unsigned((rows[i]>>32)-1),unsigned(rows[i]&0xffffffffu));
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
