"""Bounded, single-process shared memory with real aliasing and VM reservations.

The sysutil system-owned 64 KiB object contains two initialized empty rings. It is a storage
object, not an implementation of VSH requests, callbacks, or system dialogs.
"""

MMAPPER_TYPES = r'''
struct Mmapper {
    struct Object {
        std::uint64_t key; std::uint32_t id, alignment; bool system, opened;
        std::shared_ptr<GuestBacking> bytes;
        std::uint32_t container=0;
    };
    struct Area { std::uint64_t base, size, alignment; };
    struct Mapping { std::uint64_t base; std::uint32_t id; };
    struct PageMetadata {
        std::uint64_t base;
        std::weak_ptr<GuestBacking> backing;
        std::uint32_t page_size;
        std::uint64_t access_right;
    };
    struct Container {std::uint32_t id;std::uint64_t size,used=0;};
    std::vector<Object> objects;
    std::vector<Area> areas;
    std::vector<Mapping> mappings;
    std::vector<PageMetadata> page_metadata;
    std::vector<Container> containers;
    std::uint64_t container_reserved=0;
    std::uint32_t next_container=0x3f000000;
    std::uint32_t next_id=0x08000100;
    std::uint64_t cached_area=0;
    Mmapper() {
        auto storage=std::make_shared<GuestBacking>(0x10000,0);
        // Firmware 3.01 libsysutil producer initialization (module offsets
        // 0x3d14..0x3d38): two 32 KiB rings, data offset 0x400, size 0x7c00.
        for (unsigned channel=0;channel<2;++channel) {
            const auto base=channel*0x8000;
            (*storage)[base+2]=4;
            (*storage)[base+6]=0x7c;
        }
        objects.push_back({UINT64_C(0x8006010000000010), 0x08000000, 0x10000, true, false,
                          std::move(storage)});
    }
};
'''

MMAPPER_CPP = r'''
static constexpr std::uint32_t MM_EINVAL=0x80010002, MM_ENOMEM=0x80010004,
    MM_ESRCH=0x80010005, MM_EPERM=0x80010009, MM_EBUSY=0x8001000a,
    MM_EALIGN=0x80010010, MM_EEXIST=0x80010014;
// Mapping producers must supply the page size they actually requested; neither
// base alignment nor backing length distinguishes 64 KiB from 1 MiB pages.
// Weak backing identity prevents stale metadata surviving free/address reuse.
static void mmapper_forget_pages(PpuState& s,std::uint64_t base){
    auto& entries=s.mmapper.page_metadata;
    entries.erase(std::remove_if(entries.begin(),entries.end(),[&](const auto& entry){
        return entry.base==base || entry.backing.expired();
    }),entries.end());
}
[[maybe_unused]] static void mmapper_record_pages(PpuState& s,std::uint64_t base,
        std::uint32_t page_size,std::uint64_t access_right=15){
    if(page_size!=4096 && page_size!=0x10000 && page_size!=0x100000)
        throw std::runtime_error("unsupported mapping page metadata");
    const auto region=std::find_if(s.memory.regions.begin(),s.memory.regions.end(),
        [&](const auto& r){return r.base==base;});
    if(region==s.memory.regions.end())throw std::runtime_error("page metadata requires a live mapping");
    auto& entries=s.mmapper.page_metadata;
    mmapper_forget_pages(s,base);
    entries.push_back({base,region->bytes,page_size,access_right});
}
static std::uint32_t mmapper_page_attribute(PpuState& s,std::uint32_t address,std::uint32_t output){
    const auto region=std::find_if(s.memory.regions.begin(),s.memory.regions.end(),
        [&](const auto& r){return address>=r.base && address-r.base<r.bytes->size();});
    // RPCS3 sys_memory.cpp:345; SPUThread.h:179 (SPU_FAKE_BASE_ADDR).
    if(region==s.memory.regions.end() || address>=0xe8000000)return MM_EINVAL;
    if(!output)return 0x8001000d;
    std::uint32_t page=0;std::uint64_t rights=15;
    if(region->base>=0xd0000000 && region->base<0xe0000000){page=4096;rights=8;}
    for(const auto& entry:s.mmapper.page_metadata)
        if(entry.base==region->base && entry.backing.lock()==region->bytes){
            page=entry.page_size;rights=entry.access_right;break;
        }
    if(!page)throw std::runtime_error("page attribute requires allocation page-size metadata");
    // sys_page_attr_t: BE u64 attribute, BE u64 access_right, BE u32 page_size/pad.
    // Use actual runtime permissions; RPCS3's non-stack RW value is marked TODO.
    s.memory.access(output,24,true);
    s.memory.store(output,8,(region->flags&2)?0x40000:0x80000);
    s.memory.store(output+8,8,rights);s.memory.store(output+16,4,page);
    s.memory.store(output+20,4,0);return 0;
}
static std::uint64_t main_memory_used(const PpuState& s){
    auto used=s.memory.allocated+s.mmapper.container_reserved;
    for(const auto& object:s.mmapper.objects)if(!object.system && !object.container)used+=object.bytes->size();
    return used;
}
static std::uint32_t reserve_area(PpuState& s, std::uint64_t size, std::uint64_t flags,
                                 std::uint64_t alignment, std::uint64_t output) {
    if (!size || size%0x10000000) return MM_EALIGN;
    if (size>UINT32_MAX) return MM_ENOMEM;
    if (!alignment) alignment=0x10000000;
    if (alignment!=0x10000000 && alignment!=0x20000000 && alignment!=0x40000000 && alignment!=0x80000000) return MM_EALIGN;
    const auto page=flags&0xf00;
    if (page && page!=0x200 && page!=0x400) throw std::runtime_error("unsupported reservation page size");
    s.memory.access(output,4,true);
    for (std::uint64_t base=(UINT64_C(0x30000000)+alignment-1)&~(alignment-1); base+size<=0xc0000000; base+=alignment) {
        bool conflict=false;
        for (const auto& a:s.mmapper.areas) if (base<a.base+a.size && a.base<base+size) conflict=true;
        for (const auto& a:s.memory.regions) if (base<a.base+a.bytes->size() && a.base<base+size) conflict=true;
        if (conflict) continue;
        s.mmapper.areas.push_back({base,size,page==0x200?0x10000u:0x100000u});
        s.memory.store(output,4,base); return 0;
    }
    return MM_ENOMEM;
}
[[maybe_unused]] static void mmapper_address_area(PpuState& s) {
    const auto flags=s.r[3], output=s.r[4];
    if ((flags&0xf00)!=0x200 || ((flags&255)!=0 && (flags&255)!=15)) { s.r[3]=MM_EINVAL; return; }
    s.memory.access(output,4,true);
    if (!s.mmapper.cached_area) {
        s.r[3]=reserve_area(s,0x10000000,0x20f,0x10000000,output);
        if (s.r[3]) return;
        s.mmapper.cached_area=s.memory.load(output,4);
    }
    s.memory.store(output,4,s.mmapper.cached_area); s.r[3]=0;
}
static std::uint32_t mmapper_syscall(PpuState& s) {
    auto& r=s.r; auto& m=s.mmapper; auto& mem=s.memory;
    switch (r[11]) {
    case 324:case 341:{
        const auto size=r[4]&~UINT64_C(0xfffff),used=main_memory_used(s);
        if(!size || used>0x10000000 || size>0x10000000-used)return MM_ENOMEM;
        mem.access(r[3],4,true);const auto id=m.next_container++;
        m.containers.push_back({id,size,0});m.container_reserved+=size;mem.store(r[3],4,id);return 0;
    }
    case 325:case 342:{
        for(auto it=m.containers.begin();it!=m.containers.end();++it)if(it->id==r[3]){
            if(it->used)return MM_EBUSY;
            m.container_reserved-=it->size;m.containers.erase(it);return 0;
        }
        return MM_ESRCH;
    }
    case 343:{
        for(const auto& container:m.containers)if(container.id==r[4]){
            mem.access(r[3],8,true);mem.store(r[3],4,container.size);mem.store(r[3]+4,4,container.size-container.used);return 0;
        }
        return MM_ESRCH;
    }
    case 330: return reserve_area(s,r[3],r[4],r[5],r[6]);
    case 332: case 362: {
        const bool from_container=r[11]==362;
        const auto key=r[3], size=r[4], flags=from_container?r[6]:r[5], out=from_container?r[7]:r[6], page=flags&0xf00;
        if (page && page!=0x200 && page!=0x400) return MM_EINVAL;
        const std::uint32_t alignment=page==0x200?0x10000:0x100000;
        if (!size || size%alignment) return MM_EALIGN;
        if (size>256*1024*1024) return MM_ENOMEM;
        if(from_container && (flags&0xc000))return MM_EINVAL;
        Mmapper::Container* container=nullptr;
        if(from_container){
            for(auto& c:m.containers)if(c.id==static_cast<std::uint32_t>(r[5]))container=&c;
            if(!container)return MM_ESRCH;
        }
        const bool shared=key!=UINT64_C(0xffff000000000000);
        for (auto& o:m.objects) if (shared && o.key==key) {
            if (from_container || (flags&0xc000)==0xc000) return MM_EEXIST;
            if (o.bytes->size()!=size || o.alignment!=alignment) return MM_EPERM;
            if (o.opened) return MM_EEXIST;
            mem.access(out,4,true); o.opened=true; mem.store(out,4,o.id); return 0;
        }
        if (shared && !from_container && !(flags&0x8000)) return MM_ESRCH;
        const auto used=main_memory_used(s);
        if(container){if(size>container->size-container->used)return MM_ENOMEM;}
        else if (used+size>256*1024*1024) return MM_ENOMEM;
        mem.access(out,4,true);
        auto bytes=std::make_shared<GuestBacking>(size,0);
        const auto id=m.next_id; m.next_id+=0x100;
        m.objects.push_back({key,id,alignment,false,true,std::move(bytes),container?container->id:0});
        if(container)container->used+=size;
        mem.store(out,4,id); return 0;
    }
    case 329: {
        for (auto it=m.objects.begin();it!=m.objects.end();++it) if (it->id==r[3] && it->opened) {
            for (const auto& v:m.mappings) if (v.id==it->id) return MM_EBUSY;
            if (it->system) it->opened=false;
            else {
                if(it->container)for(auto& c:m.containers)if(c.id==it->container)c.used-=it->bytes->size();
                m.objects.erase(it);
            }
            return 0;
        }
        return MM_ESRCH;
    }
    case 331: {
        for (auto it=m.areas.begin();it!=m.areas.end();++it) if (it->base==r[3]) {
            for (const auto& v:m.mappings) if (v.base>=it->base && v.base<it->base+it->size) return MM_EBUSY;
            if (m.cached_area==it->base) m.cached_area=0;
            m.areas.erase(it); return 0;
        }
        return MM_EINVAL;
    }
    case 334: case 337: {
        GuestMemory::ConfigurationPermit configuration(mem);
        const Mmapper::Area* area=nullptr;
        for (const auto& a:m.areas) if (r[3]>=a.base && r[3]<a.base+a.size) { area=&a; break; }
        if (!area || (r[11]==337 && area->base!=r[3])) return MM_EINVAL;
        Mmapper::Object* object=nullptr;
        for (auto& o:m.objects) if (o.id==r[4] && o.opened) { object=&o; break; }
        if (!object) return MM_ESRCH;
        if (object->alignment<area->alignment) return MM_EINVAL;
        if (r[3]%area->alignment) return MM_EALIGN;
        if (r[5]!=0x40000 && r[5]!=0x80000) throw std::runtime_error("unsupported shared memory access flags");
        if (r[11]==337) mem.access(r[6],4,true);
        const auto size=object->bytes->size();
        for (std::uint64_t base=r[3];base+size<=area->base+area->size;base+=object->alignment) {
            bool conflict=false;
            for (const auto& a:mem.regions) if (base<a.base+a.bytes->size() && a.base<base+size) conflict=true;
            if (conflict) { if (r[11]==334) return MM_EBUSY; continue; }
            m.page_metadata.reserve(m.page_metadata.size()+1);
            mem.regions.push_back({base,r[5]==0x80000?4u:6u,object->bytes,
                "sys_mmapper_map_memory object="+std::to_string(object->id)+" key="+std::to_string(object->key)+
                " container="+std::to_string(object->container)+" area="+std::to_string(area->base)});mem.mapping_changed();
            std::fprintf(stderr,"mmapper mapped ea=%08" PRIx64 " size=%08" PRIx64 " object=%08x container=%08x area=%08" PRIx64 "\n",
                base,std::uint64_t(size),object->id,object->container,area->base);
            mmapper_record_pages(s,base,object->alignment);
            m.mappings.push_back({base,object->id});
            if (r[11]==337) mem.store(r[6],4,base);
            return 0;
        }
        return MM_ENOMEM;
    }
    case 335: {
        GuestMemory::ConfigurationPermit configuration(mem);
        for (auto it=m.mappings.begin();it!=m.mappings.end();++it) if (it->base==r[3]) {
            mem.access(r[4],4,true); const auto id=it->id;
            // Output must remain accessible after unmapping.
            for (const auto& region:mem.regions) if (region.base==it->base && r[4]>=region.base && r[4]<region.base+region.bytes->size())
                throw std::runtime_error("unmap output lies in removed mapping");
            for(const auto& region:mem.regions)if(region.base==it->base)mem.mark_write(region.bytes->data(),region.bytes->size());
            mem.regions.erase(std::remove_if(mem.regions.begin(),mem.regions.end(),[&](const auto& v){return v.base==it->base;}),mem.regions.end());mem.mapping_changed();
            mmapper_forget_pages(s,it->base);
            m.mappings.erase(it); mem.store(r[4],4,id); return 0;
        }
        return MM_EINVAL;
    }
    default: throw std::runtime_error("unsupported mmapper call");
    }
}
'''
