"""Out-of-line diagnostics and provenance for dynamically executed SPU code."""

CPP = r'''
// Keep diagnostics, hashing and the decoder out of all static SPU TUs.
// DMA provenance only serves the explicitly requested ANYPS3_SPU_DYNAMIC_LOG (and the
// last_DMA part of dynamic fault messages in such runs). Without the log the map stays
// empty, so spu_ls_forget on every STQ costs one empty() check.
static bool spu_provenance_enabled(){
    static const bool enabled=[]{const char* path=std::getenv("ANYPS3_SPU_DYNAMIC_LOG");return path && *path;}();
    return enabled;
}
void spu_ls_write(NativeSpu::Thread& t,unsigned base,unsigned size,std::uint64_t ea){
    if(!size || !spu_provenance_enabled())return;
    const unsigned end=base+size;
    if(base>0x40000 || size>0x40000-base)throw std::runtime_error("invalid SPU provenance range");
    // Entries are disjoint and sorted by base, hence also by end: find the
    // overlapped run [first,last) in O(log n) and touch only that run.
    auto& w=t.dma_writes;
    const auto first=std::lower_bound(w.begin(),w.end(),base,[](const auto& r,unsigned b){return r.end<=b;});
    auto last=first;while(last!=w.end() && last->base<end)++last;
    if(first==last){if(ea!=UINT64_MAX)w.insert(first,{base,end,ea});return;}
    // Streaming jobs reuse the same GET/GETL and reservation buffers: update in place.
    if(last-first==1 && first->base==base && first->end==end){
        if(ea==UINT64_MAX)w.erase(first);else first->ea=ea;
        return;
    }
    const auto head=*first,tail=*(last-1);
    NativeSpu::Thread::DmaWrite pieces[3];unsigned count=0;
    if(head.base<base)pieces[count++]={head.base,base,head.ea};
    if(ea!=UINT64_MAX)pieces[count++]={base,end,ea};
    if(tail.end>end)pieces[count++]={end,tail.end,tail.ea+end-tail.base};
    const auto at=w.erase(first,last);
    w.insert(at,pieces,pieces+count);
}
[[noreturn]] void spu_dynamic_fault(const NativeSpu::Thread& t,const std::uint8_t* ls,const char* reason){
    char message[384];
    const auto word=t.pc<=0x3fffc && !(t.pc&3)?spu_load_word(ls+t.pc):0;
    std::snprintf(message,sizeof(message),"%s: SPU id=%x pc=%05x word=%08x",reason,t.id,t.pc,word);
    std::string result=message;
    for(const auto& r:t.dma_writes)if(t.pc>=r.base && t.pc<r.end){
        std::snprintf(message,sizeof(message)," last_DMA ls=%05x size=%x ea=%016" PRIx64 " pc_ea=%016" PRIx64,
            r.base,r.end-r.base,r.ea,r.ea+t.pc-r.base);result+=message;break;
    }
    throw std::runtime_error(result);
}
// FIPS 180-4 SHA-256, used only when a log was explicitly requested.
static std::string spu_dynamic_sha256(const std::uint8_t* bytes,unsigned size){
    static constexpr std::uint32_t k[]={
        0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
        0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
        0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
        0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
        0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
        0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
        0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
        0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
    std::uint32_t h[]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    std::vector<std::uint8_t> data(bytes,bytes+size);data.push_back(0x80);
    while(data.size()%64!=56)data.push_back(0);
    const auto bits=std::uint64_t(size)*8;for(int i=7;i>=0;--i)data.push_back(bits>>(8*i));
    const auto rotr=[](std::uint32_t v,unsigned n){return (v>>n)|(v<<(32-n));};
    for(unsigned at=0;at<data.size();at+=64){
        std::uint32_t w[64];for(unsigned i=0;i<16;++i)w[i]=spu_load_word(data.data()+at+i*4);
        for(unsigned i=16;i<64;++i){const auto a=w[i-15],b=w[i-2];
            w[i]=w[i-16]+(rotr(a,7)^rotr(a,18)^(a>>3))+w[i-7]+(rotr(b,17)^rotr(b,19)^(b>>10));}
        auto a=h[0],b=h[1],c=h[2],d=h[3],e=h[4],f=h[5],g=h[6],z=h[7];
        for(unsigned i=0;i<64;++i){
            const auto t1=z+(rotr(e,6)^rotr(e,11)^rotr(e,25))+((e&f)^(~e&g))+k[i]+w[i];
            const auto t2=(rotr(a,2)^rotr(a,13)^rotr(a,22))+((a&b)^(a&c)^(b&c));
            z=g;g=f;f=e;e=d+t1;d=c;c=b;b=a;a=t1+t2;
        }
        h[0]+=a;h[1]+=b;h[2]+=c;h[3]+=d;h[4]+=e;h[5]+=f;h[6]+=g;h[7]+=z;
    }
    char result[65];for(unsigned i=0;i<8;++i)std::snprintf(result+i*8,9,"%08x",h[i]);return result;
}
static void spu_dynamic_record(NativeSpu::Thread& t,const std::uint8_t* ls,const char* mode="interpreted"){
    static const char* path=std::getenv("ANYPS3_SPU_DYNAMIC_LOG");
    if(!path || !*path)return;
    // A diagnostic block is a fixed 64-byte LS cell, not a guessed job extent.
    const auto base=t.pc&~63u;const unsigned size=64;
    std::vector<NativeSpu::Thread::DmaWrite> overlap;
    for(const auto& r:t.dma_writes)if(r.base<base+size && r.end>base)overlap.push_back(r);
    const auto same=[](const auto& a,const auto& b){return a.base==b.base && a.end==b.end && a.ea==b.ea;};
    auto& seen=t.dynamic_seen[base];
    if(seen.valid && seen.mode==mode && !std::memcmp(seen.bytes.data(),ls+base,size) && std::equal(overlap.begin(),overlap.end(),seen.origins.begin(),seen.origins.end(),same))
        return;
    seen.valid=true;seen.mode=mode;std::memcpy(seen.bytes.data(),ls+base,size);seen.origins=overlap;
    const auto hash=spu_dynamic_sha256(ls+base,size);
    std::string origins,key=std::to_string(base)+":"+hash;
    for(const auto& r:t.dma_writes)if(r.base<base+size && r.end>base){
        char row[192];std::snprintf(row,sizeof(row),"%s{\"ls_base\":%u,\"size\":%u,\"ea\":%" PRIu64 "}",
            origins.empty()?"":",",r.base,r.end-r.base,r.ea);origins+=row;
    }
    key+=":"+origins+":"+mode;
    if(t.dynamic_blocks.count(key))return;
    static std::mutex mutex;std::lock_guard<std::mutex> lock(mutex);
    FILE* file=std::fopen(path,"ab");
    if(!file)throw std::runtime_error("cannot open ANYPS3_SPU_DYNAMIC_LOG");
    std::string hex;static constexpr char digits[]="0123456789abcdef";
    for(unsigned i=0;i<size;++i){hex+=digits[ls[base+i]>>4];hex+=digits[ls[base+i]&15];}
    const int written=std::fprintf(file,"{\"schema_version\":1,\"execution\":\"%s\",\"spu_id\":%u,\"image_index\":%u,\"pc\":%u,\"word\":%u,\"ls_base\":%u,\"size\":%u,\"sha256\":\"%s\",\"data_hex\":\"%s\",\"last_dma\":[%s]}\n",
        mode,t.id,t.image_index,t.pc,spu_load_word(ls+t.pc),base,size,hash.c_str(),hex.c_str(),origins.c_str());
    const int closed=std::fclose(file);
    if(written<0 || closed)throw std::runtime_error("cannot write ANYPS3_SPU_DYNAMIC_LOG");
    t.dynamic_blocks.insert(std::move(key));
}
'''
