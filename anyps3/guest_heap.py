"""Bounded FW 3.01 sysPrxForUser heaps over actual guest backing.

Evidence: liblv2-relocated.elf SHA256 843006f84aa0f195ae85119e891238f896ae8f9faf339904709a91bd3be9bdfc.
See local/ratchet/codex/heap-result.md for ABI, capacity and unsupported cases.
"""

IMPORTS = {
    ("sysPrxForUser", "0xb2fcf2c8"): ("_sys_heap_create_heap", "r[3]=guest_heap_create(state);"),
    ("sysPrxForUser", "0x35168520"): ("_sys_heap_malloc", "r[3]=guest_heap_malloc(state);"),
    ("sysPrxForUser", "0x44265c08"): ("_sys_heap_memalign", "r[3]=guest_heap_memalign(state);"),
    ("sysPrxForUser", "0x8a561d92"): ("_sys_heap_free", "r[3]=sign_word(guest_heap_free(state));"),
    ("sysPrxForUser", "0xaede4b03"): ("_sys_heap_delete_heap", "r[3]=sign_word(guest_heap_delete(state));"),
}

CPP = r'''
struct GuestHeap {
    struct Block {std::uint64_t base,size;}; // Chunk includes 8-byte header/footer charge.
    GuestMemory* memory=nullptr;
    std::weak_ptr<std::array<std::uint32_t,8>> process;
    // Guest regions covering [base, base+size) in address order; a buffer may span
    // adjacent mappings (contiguous guest addresses), but never a gap.
    std::vector<std::weak_ptr<GuestBacking>> backing;
    std::uint32_t handle=0,base=0,size=0;
    bool owned=false;
    std::vector<Block> free;
    std::map<std::uint32_t,Block> live;
};
// No raw-PpuState-only key: the shared process token and weak backing prevent
// accepting handles when a host address is reused, without retaining guest RAM.
// All PPU contexts of this cooperative, single-host-thread runtime share token.
static std::vector<GuestHeap> guest_heaps;
static void guest_heap_prune(){
    guest_heaps.erase(std::remove_if(guest_heaps.begin(),guest_heaps.end(),
        [](const auto& h){return h.process.expired() || std::any_of(h.backing.begin(),h.backing.end(),[](const auto& b){return b.expired();});}),guest_heaps.end());
}
[[maybe_unused]] static auto guest_heap_region(PpuState& s,std::uint32_t base,std::uint32_t size){
    const auto end=std::uint64_t(base)+size;
    return std::find_if(s.memory.regions.begin(),s.memory.regions.end(),[&](const auto& r){
        return base>=r.base && end<=r.base+r.bytes->size();
    });
}
// Regions covering a contiguous guest range in address order; empty if any byte is unmapped.
static std::vector<const GuestMemory::Region*> guest_heap_cover(PpuState& s,std::uint32_t base,std::uint32_t size){
    std::vector<const GuestMemory::Region*> cover;
    auto next=std::uint64_t(base);const auto end=next+size;
    while(next<end){
        const auto region=std::find_if(s.memory.regions.begin(),s.memory.regions.end(),[&](const auto& r){
            return next>=r.base && next-r.base<r.bytes->size();});
        if(region==s.memory.regions.end())return {};
        cover.push_back(&*region);next=region->base+region->bytes->size();
    }
    return cover;
}
static void guest_heap_validate(PpuState& s,const GuestHeap& h){
    const auto cover=guest_heap_cover(s,h.base,h.size);
    if(cover.empty() || cover.size()!=h.backing.size())throw std::runtime_error("heap backing was removed or replaced");
    for(std::size_t i=0;i<cover.size();++i){
        if(cover[i]->bytes!=h.backing[i].lock())throw std::runtime_error("heap backing was removed or replaced");
        for(const auto& other:s.memory.regions)
            if(&other!=cover[i] && other.bytes==cover[i]->bytes)
                throw std::runtime_error("aliased heap backing is unsupported");
    }
    for(const auto* region:cover)if(!(region->flags&2) || (region->flags&1))throw std::runtime_error("heap backing is not writable data");
}
static GuestHeap& guest_heap_find(PpuState& s,std::uint32_t handle){
    guest_heap_prune();
    for(auto& h:guest_heaps)
        if(h.memory==&s.memory && h.process.lock()==s.thread_atexit && h.handle==handle){
            guest_heap_validate(s,h);return h;
        }
    throw std::runtime_error("unknown, foreign or deleted heap handle");
}
static std::uint64_t guest_heap_chunk(std::uint32_t size){
    // Original 0x28008b78/0x28008b94/0x28008c6c: minimum 16,
    // request+23 rounded down to 16; >=0xffffffc0 cannot allocate.
    if(size>=0xffffffc0u)return 0;
    return std::max(UINT64_C(16),(std::uint64_t(size)+23)&~UINT64_C(15));
}
static void guest_heap_coalesce(GuestHeap& h){
    std::sort(h.free.begin(),h.free.end(),[](auto a,auto b){return a.base<b.base;});
    std::size_t out=0;
    for(const auto block:h.free){
        if(out && h.free[out-1].base+h.free[out-1].size==block.base)h.free[out-1].size+=block.size;
        else h.free[out++]=block;
    }
    h.free.resize(out);
}
static GuestHeap::Block guest_heap_take(GuestHeap& h,std::uint64_t size){
    const auto end=std::uint64_t(h.base)+h.size-56;
    for(auto it=h.free.begin();it!=h.free.end();++it){
        // Original top chunk (0x28008f48): must leave a nonzero top.
        const bool top=it->base+it->size==end;
        if(it->size<size || (top && it->size<=size))continue;
        GuestHeap::Block block{it->base,size};
        const auto remaining=it->size-size;
        if(remaining<16){block.size=it->size;h.free.erase(it);}
        else {it->base+=size;it->size=remaining;}
        return block;
    }
    return {0,0};
}
[[maybe_unused]] static std::uint32_t guest_heap_create(PpuState& s){
    const auto name=std::uint32_t(s.r[3]),requested=std::uint32_t(s.r[4]);
    const auto flags=std::uint32_t(s.r[5]),option=std::uint32_t(s.r[6]);
    std::uint32_t supplied=0;
    if(option){
        if(s.memory.load(option,4)!=8)return 0; // FW 0x2800aeb8..aec4.
        if(flags&0x80000000u)supplied=std::uint32_t(s.memory.load(std::uint64_t(option)+4,4));
    }
    const bool owned=!supplied || !(flags&0x80000000u) || (flags&1);
    // Firmware uses 32-bit arithmetic when rounding syscall-348 pages.
    const auto size=owned?std::uint32_t((std::uint64_t(requested)+0xffff)&~UINT64_C(0xffff)):requested;
    if(owned && !size)return 0;
    if(!owned && ((supplied&15) || (size&15)))
        throw std::runtime_error("heap supplied buffer/size must be 16-byte aligned in this runtime");
    if(size<=584 || size>=UINT32_MAX-4096u-584u)
        throw std::runtime_error("heap size rejected by original suballocator; wrapper failure is unsupported");
    // Original name copy has a 31-byte bound, independent of requested RAM.
    for(unsigned i=0;i<31;++i)if(!s.memory.load(std::uint64_t(name)+i,1))break;
    guest_heap_prune();guest_heaps.reserve(guest_heaps.size()+1);
    GuestHeap h;h.memory=&s.memory;h.process=s.thread_atexit;h.size=size;h.owned=owned;
    h.base=owned?0:supplied;
    h.free.reserve(1);
    if(!owned){
        const auto cover=guest_heap_cover(s,h.base,h.size);
        if(cover.empty())throw std::runtime_error("unmapped supplied heap buffer");
        for(const auto* region:cover)h.backing.push_back(region->bytes);
        guest_heap_validate(s,h);
        for(const auto& other:guest_heaps)
            if(other.memory==&s.memory && other.process.lock()==s.thread_atexit &&
               h.base<std::uint64_t(other.base)+other.size && other.base<std::uint64_t(h.base)+h.size)
                throw std::runtime_error("overlapping guest heaps are unsupported");
    }else{
        // Reuse libc's address-space search, page backing and 256 MiB budget.
        h.base=guest_malloc(s,size);if(!h.base)return 0;
        h.backing={import_region(s,h.base)->bytes};
        // Kind 2 isolates heap storage from _sys_free's libc kind 0.
        import_owner(s,h.base,0)->kind=2;
    }
    h.handle=h.base+16;
    // Original aligned-base layout: 0x218 chunk start, 56-byte end fence.
    h.free.push_back({std::uint64_t(h.base)+0x218,size-0x250u});
    const auto handle=h.handle;guest_heaps.push_back(std::move(h));return handle;
}
[[maybe_unused]] static std::uint32_t guest_heap_malloc(PpuState& s){
    if(!std::uint32_t(s.r[3]))return 0; // Original wrapper diagnoses NULL and returns NULL.
    auto& h=guest_heap_find(s,std::uint32_t(s.r[3]));const auto chunk=guest_heap_chunk(std::uint32_t(s.r[4]));
    if(!chunk)return 0;
    // Host metadata failures explicitly stop rather than report guest success.
    const auto block=guest_heap_take(h,chunk);if(!block.size)return 0;
    const auto pointer=std::uint32_t(block.base+8);
    h.live.emplace(pointer,block);return pointer;
}
[[maybe_unused]] static std::uint32_t guest_heap_memalign(PpuState& s){
    if(!std::uint32_t(s.r[3]))return 0;
    auto& h=guest_heap_find(s,std::uint32_t(s.r[3]));auto alignment=std::uint32_t(s.r[4]);
    const auto requested=std::uint32_t(s.r[5]);
    if(alignment>0x80000000u)
        throw std::runtime_error("heap memalign alignment overflow in original allocator");
    if(alignment>16 && (alignment&(alignment-1))){
        std::uint32_t rounded=32;while(rounded<alignment)rounded*=2;alignment=rounded;
    }
    const auto chunk=guest_heap_chunk(requested);if(!chunk)return 0;
    if(alignment<=16){s.r[4]=requested;return guest_heap_malloc(s);}
    // Original aligned allocator first allocates nb+alignment+8 as a request,
    // then returns prefix/tail chunks to the allocator (0x280084dc..08678).
    if(std::uint64_t(requested)+alignment>=UINT64_C(0xffffffc0))return 0;
    const auto temporary=guest_heap_chunk(std::uint32_t(chunk+alignment+8));
    if(!temporary)return 0;
    h.free.reserve(h.free.size()+2);
    const auto raw=guest_heap_take(h,temporary);if(!raw.size)return 0;
    auto pointer=(raw.base+8+alignment-1)&~std::uint64_t(alignment-1);
    auto prefix=pointer-8-raw.base;
    if(prefix && prefix<16){pointer+=alignment;prefix+=alignment;}
    auto charged=raw.size-prefix;
    // FW splits a tail only when remaining >16, not at equality.
    if(charged>chunk+16){h.free.push_back({pointer-8+chunk,charged-chunk});charged=chunk;}
    if(prefix)h.free.push_back({raw.base,prefix});
    h.live.emplace(std::uint32_t(pointer),GuestHeap::Block{pointer-8,charged});
    guest_heap_coalesce(h);return std::uint32_t(pointer);
}
[[maybe_unused]] static std::uint32_t guest_heap_free(PpuState& s){
    auto& h=guest_heap_find(s,std::uint32_t(s.r[3]));const auto pointer=std::uint32_t(s.r[4]);
    if(!pointer)return 0; // Original suballocator 0x28009a28..09aa8.
    const auto it=h.live.find(pointer);
    if(it==h.live.end())throw std::runtime_error("heap free of foreign, interior or already freed pointer");
    h.free.push_back(it->second);h.live.erase(it);guest_heap_coalesce(h);return 0;
}
[[maybe_unused]] static std::uint32_t guest_heap_delete(PpuState& s){
    auto& h=guest_heap_find(s,std::uint32_t(s.r[3]));const auto handle=h.handle;
    // Original delete walks segments and releases owned backing regardless of
    // live chunks; supplied backing remains the caller's allocation.
    if(h.owned)import_release(s,h.base,2);
    guest_heaps.erase(std::remove_if(guest_heaps.begin(),guest_heaps.end(),[&](const auto& item){
        return item.memory==&s.memory && item.process.lock()==s.thread_atexit && item.handle==handle;
    }),guest_heaps.end());return 0;
}
'''
