"""Bounded import lifecycles using existing guest mappings and PPU contexts.

Reference: RPCS3 ffba2bf09ef3980638f9327b25240d81414034ab,
sys_libc_.cpp:371, sys_ppu_thread_.cpp:192, sys_spu_.cpp:356.
Ownership is tied to backing lifetime, never to reusable host state addresses.
"""

IMPORTS = {
    ("sysPrxForUser", "0xaeb78725"): ("sys_lwmutex_trylock", "r[3]=sign_word(lwmutex_trylock(state));"),
    ("sysPrxForUser", "0x3dd4a957"): ("sys_ppu_thread_register_atexit", "r[3]=sign_word(thread_atexit_service(state,false));"),
    ("sysPrxForUser", "0xac6fc404"): ("sys_ppu_thread_unregister_atexit", "r[3]=sign_word(thread_atexit_service(state,true));"),
    ("sysPrxForUser", "0xbdb18f83"): ("_sys_malloc", "r[3]=guest_malloc(state,std::uint32_t(r[3]));"),
    ("sysPrxForUser", "0xf7f7fb20"): ("_sys_free", "guest_free(state,std::uint32_t(r[3]));"),
    ("cellSysmodule", "0x112a5ee9"): ("cellSysmoduleUnloadModule", "native_module_unload_unsupported(state);"),
    # Original FW 3.01 export at 0x3154..0x3178 is a leaf returning zero.
    # libsysmodule.analysis.elf SHA256 8e23e1fa3ef7f35b06ed5260bd836a95ad61c55d886da674a8a22d10fa639f02.
    ("cellSysmodule", "0x96c07adf"): ("cellSysmoduleFinalize", "r[3]=0;"),
    ("sysPrxForUser", "0xe0da8efd"): ("sys_spu_image_close", "r[3]=sign_word(guest_spu_image_close(state));"),
}

CPP = r'''
// Defined in lv2_spu_ext, after this import block in the generated runtime.
static std::uint32_t spu_close_kernel_image(PpuState&,std::uint32_t);
[[maybe_unused]] static std::uint32_t thread_atexit_service(PpuState& s,bool remove){
    const auto opd=std::uint32_t(s.r[3]);auto& callbacks=*s.thread_atexit;
    // Atomic within the single-host-thread cooperative scheduler: no yielding.
    // Registration stores OPDs without dereferencing, as the original FW does.
    const auto found=std::find(callbacks.begin(),callbacks.end(),opd);
    if(remove){if(found==callbacks.end())return MM_ESRCH;*found=0;return 0;}
    if(found!=callbacks.end())return MM_EPERM;
    const auto empty=std::find(callbacks.begin(),callbacks.end(),0);
    if(empty==callbacks.end())return MM_ENOMEM;
    *empty=opd;return 0;
}

struct ImportOwnedMapping {
    std::weak_ptr<GuestBacking> backing;
    std::uint64_t base;
    unsigned kind; // 0: libc malloc; 1: imported SPU segment table
    std::uint32_t entry=0,count=0;
};
// Weak ownership prevents either keeping guest memory alive or accepting a
// foreign allocation after a PpuState/GuestMemory address has been reused.
static std::vector<ImportOwnedMapping> import_owned_mappings;
static void import_prune_ownership(){
    import_owned_mappings.erase(std::remove_if(import_owned_mappings.begin(),import_owned_mappings.end(),
        [](const auto& o){return o.backing.expired();}),import_owned_mappings.end());
}
static auto import_region(PpuState& s,std::uint64_t base){
    return std::find_if(s.memory.regions.begin(),s.memory.regions.end(),[&](const auto& region){return region.base==base;});
}
static auto import_owner(PpuState& s,std::uint64_t base,unsigned kind){
    const auto region=import_region(s,base);
    return std::find_if(import_owned_mappings.begin(),import_owned_mappings.end(),[&](const auto& o){
        return region!=s.memory.regions.end() && o.base==base && o.kind==kind && o.backing.lock()==region->bytes;
    });
}
static void import_release(PpuState& s,std::uint64_t base,unsigned kind){
    GuestMemory::ConfigurationPermit configuration(s.memory);
    auto owner=import_owner(s,base,kind);
    if(owner==import_owned_mappings.end())throw std::runtime_error("free of foreign or closed import allocation");
    auto region=import_region(s,base);
    for(const auto& other:s.memory.regions)if(other.base!=base && other.bytes==region->bytes)
        throw std::runtime_error("free of aliased import allocation is unsupported");
    s.memory.mark_write(region->bytes->data(),region->bytes->size());
    s.memory.allocated-=region->bytes->size();s.memory.regions.erase(region);s.memory.mapping_changed();
    import_owned_mappings.erase(owner);
}
[[maybe_unused]] static std::uint32_t guest_malloc(PpuState& s,std::uint32_t requested){
    // RPCS3 vm.h:111 and vm.cpp:1461: 64 KiB alignment and page rounding;
    // zero size or budget/address exhaustion returns NULL, not a CELL errno.
    if(!requested)return 0;
    const auto size=(std::uint64_t(requested)+0xffff)&~UINT64_C(0xffff);
    const auto used=main_memory_used(s);
    if(used>0x10000000 || size>0x10000000-used)return 0;
    std::uint64_t base=0x10000000;
    for(;;){
        auto next=base;
        for(const auto& region:s.memory.regions)
            if(base<region.base+region.bytes->size() && region.base<base+size)next=std::max(next,region.base+region.bytes->size());
        for(const auto& area:s.mmapper.areas)
            if(base<area.base+area.size && area.base<base+size)next=std::max(next,area.base+area.size);
        if(next==base)break;
        base=(next+0xffff)&~UINT64_C(0xffff);
    }
    if(base+size>0xc0000000)return 0;
    import_prune_ownership();
    // Reserve metadata before mapping so host allocation failure cannot leave
    // an unowned mapping. Host memory failure traps, rather than masking bugs.
    import_owned_mappings.reserve(import_owned_mappings.size()+1);
    s.memory.map(base,size,6,nullptr,0);
    import_owned_mappings.push_back({import_region(s,base)->bytes,base,0});
    return std::uint32_t(base);
}
[[maybe_unused]] static void guest_free(PpuState& s,std::uint32_t base){
    // NULL/foreign/double-free FW policy is not established: explicit stop.
    import_release(s,base,0);
}
[[maybe_unused,noreturn]] static void native_module_unload_unsupported(PpuState& s){
    const auto id=std::uint16_t(s.r[3]);
    throw std::runtime_error("native module stop/unload is unsupported: ID "+std::to_string(id)+
        " loaded="+std::to_string(s.loaded_modules.count(id))+" initializing="+std::to_string(s.module_loading));
}
[[maybe_unused]] static void guest_spu_image_import(PpuState& s){
    // Wrap the existing importer rather than duplicate its ELF/segment model.
    const auto output=std::uint32_t(s.r[3]);
    s.r[3]=output;s.r[4]=std::uint32_t(s.r[4]);
    // Preserve the importer's invalid/protected-mode handling before pointers.
    if(s.r[5]!=1){spu_image_import(s);return;}
    s.memory.access(output,16,true);
    if(import_owner(s,s.memory.load(output+8,4),1)!=import_owned_mappings.end())
        throw std::runtime_error("reimport over an owned SPU image is unsupported");
    import_prune_ownership();import_owned_mappings.reserve(import_owned_mappings.size()+1);
    spu_image_import(s);
    if(s.r[3])return;
    const auto table=s.memory.load(output+8,4);const auto region=import_region(s,table);
    if(region==s.memory.regions.end())throw std::runtime_error("SPU importer lost segment allocation");
    import_owned_mappings.push_back({region->bytes,table,1,
        std::uint32_t(s.memory.load(output+4,4)),std::uint32_t(s.memory.load(output+12,4))});
}
[[maybe_unused]] static std::uint32_t guest_spu_image_close(PpuState& s){
    const auto image=std::uint32_t(s.r[3]);auto& m=s.memory;
    const auto type=m.load(image,4);
    if(type==1)return spu_close_kernel_image(s,image);
    if(type!=0)return MM_EINVAL;
    m.access(image,16,false);
    const auto table=m.load(image+8,4);const auto owner=import_owner(s,table,1);
    if(owner==import_owned_mappings.end())throw std::runtime_error("close of foreign or closed SPU image");
    if(m.load(image+4,4)!=owner->entry || m.load(image+12,4)!=owner->count)
        throw std::runtime_error("SPU image ownership metadata changed");
    // SPU initialization copied descriptors; startup copied payload to owned LS.
    // Closing the image releases only its table, never source bytes or SPU LS.
    import_release(s,table,1);return 0;
}
'''
