"""Bounded sysPrxForUser services verified against original liblv2 3.01.

ELF SHA256 843006f84aa0f195ae85119e891238f896ae8f9faf339904709a91bd3be9bdfc.
See local/ratchet/codex/batch/imports3-result.md for ABI and remaining traps.
"""

IMPORTS = {
    ("sysPrxForUser", "0x3ef17f8c"): ("__sys_look_ctype_table", "r[3]=guest_misc_ctype(r[3]);"),
    ("sysPrxForUser", "0x791b9219"): ("_sys_vsprintf", "guest_misc_vsprintf(state);"),
    ("sysPrxForUser", "0xe6f2c1e7"): ("sys_process_exit", "guest_misc_process_exit(state);"),
    ("sysPrxForUser", "0xc4fd6121"): ("_sys_qsort", "guest_misc_qsort(state);"),
    ("sysPrxForUser", "0xdd0c1e09"): ("_sys_spu_printf_attach_group", "r[3]=sign_word(guest_misc_spu_printf(state,0));"),
    ("sysPrxForUser", "0x5fdfb2fe"): ("_sys_spu_printf_detach_group", "r[3]=sign_word(guest_misc_spu_printf(state,1));"),
    # Include registration/reset so an installed callback never looks absent.
    ("sysPrxForUser", "0x45fe2fce"): ("_sys_spu_printf_initialize", "guest_misc_spu_printf_initialize(state); r[3]=0;"),
    ("sysPrxForUser", "0xdd3b27ac"): ("_sys_spu_printf_finalize", "guest_misc_spu_printf_finalize(state); r[3]=0;"),
}

# Keep these as actual translation traps, rather than claiming an implementation
# merely by giving a runtime_error a different name. Names verified by NID hash;
# the unknown export deliberately has no guessed official name.
UNSUPPORTED = {
    ("sysPrxForUser", "0x42b23552"): ("sys_prx_register_library", "requires export linking and library lifecycle"),
    ("sysPrxForUser", "0x26090058"): ("sys_prx_load_module", "requires catalog identity, relocation and linking"),
    ("sysPrxForUser", "0xaa6d9bff"): ("sys_prx_load_module_on_memcontainer", "requires loader and container accounting"),
    ("sysPrxForUser", "0xa2c7ba64"): ("sys_prx_exitspawn_with_level", "requires module enumeration and guest callbacks"),
    ("sysPrxForUser", "0xb6369393"): ("_sys_heap_get_total_free_size", "requires firmware mallinfo accounting over HLE heap metadata"),
    ("sysPrxForUser", "0xd1ad4570"): ("_sys_heap_get_mallinfo", "requires ten verified firmware allocator counters"),
    ("sysPrxForUser", "0x67f9fedb"): ("sys_game_process_exitspawn2", "requires process replacement and exit callbacks"),
    ("sysPrxForUser", "0xfc52a7a9"): ("sys_game_process_exitspawn", "requires process replacement and exit callbacks"),
    ("sysPrxForUser", "0x9fb6228e"): (None, "unknown diagnostic/fatal protocol; printf-only return is incorrect"),
    ("sysPrxForUser", "0xe75c40f2"): ("sys_process_get_paramsfo", "requires original process 64-byte PARAM.SFO cache"),
    ("cellLibprof", "0x05893e7c"): ("cellUserTraceRegister", "trace protocol/error ABI unverified"),
    ("cellLibprof", "0x6d045c2e"): ("cellUserTraceUnregister", "trace protocol/error ABI unverified"),
}

CPP = r'''
// Original 0x2800809c..0x280080b4: sign-extended halfword at table[ch+1].
// BE table 0x280129b0..0x28012ab1 (129 entries, EOF and ASCII).
[[maybe_unused]] static std::uint64_t guest_misc_ctype(std::uint64_t argument){
    static constexpr std::uint16_t table[]={
        0,
        0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x408,8,8,8,8,
        0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,0x20,
        0x18,
        0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,0x10,
        4,4,4,4,4,4,4,4,4,4,
        0x10,0x10,0x10,0x10,0x10,0x10,0x10,
        0x41,0x41,0x41,0x41,0x41,0x41,
        1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,
        0x10,0x10,0x10,0x10,0x10,0x10,
        0x42,0x42,0x42,0x42,0x42,0x42,
        2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,2,
        0x10,0x10,0x10,0x10,0x20,
    };
    static_assert(sizeof(table)/sizeof(*table)==129);
    const auto ch=static_cast<std::int32_t>(argument);
    if(ch < -1 || ch > 127)
        throw std::runtime_error("__sys_look_ctype_table outside verified EOF/ASCII range");
    return std::uint64_t(std::int64_t(static_cast<std::int16_t>(table[ch+1])));
}
[[maybe_unused]] static void guest_misc_vsprintf(PpuState& s){
    // Original wrapper 0x28004ff8: r3=dst,r4=fmt,r5=BE 8-byte slots.
    // Same formatter as vsnprintf; callback 0x28005dd8 writes all bytes
    // and NUL on the formatter's final 0x201 notification.
    const auto dst=std::uint32_t(s.r[3]);
    GuestPrintfArgs args{s,true,0,std::uint32_t(s.r[5])};
    auto result=guest_printf_format(s,std::uint32_t(s.r[4]),args);
    guest_printf_address(dst,result.size()); // Include terminator in overflow check.
    result.push_back('\0');
    s.memory.transfer(dst,reinterpret_cast<std::uint8_t*>(result.data()),unsigned(result.size()),true);
    s.r[3]=result.size()-1;
}
[[maybe_unused,noreturn]] static void guest_misc_process_exit(PpuState& s){
    // Original 0x28011380 sets r4=r5=0, runs exit handlers, then syscall 22.
    // A real process-wide exit must never return to the generated import stub.
    if(s.atexit_callback || s.at_exit_callback)
        throw std::runtime_error("sys_process_exit requires registered process atexit callbacks");
    const auto status=static_cast<std::int32_t>(s.r[3]);
    std::fprintf(stderr,"native process exit status=%" PRId32 "\n",status);
    if(std::fflush(nullptr)!=0)throw std::runtime_error("sys_process_exit host stream flush failed");
    // This standalone runtime owns exactly one guest process. Terminate its
    // host worker threads too; no C++ stack unwinding into guest code.
    // POSIX exposes the low 8 bits; the full guest s32 is recorded above.
    std::_Exit(status);
}
[[maybe_unused]] static void guest_misc_qsort(PpuState& s){
    const auto count=std::uint32_t(s.r[4]),size=std::uint32_t(s.r[5]);
    // Original 0x28004258..0x28004360 returns without data/comparator access.
    if(count<=1)return;
    if(std::uint64_t(count)*size>UINT32_MAX-std::uint64_t(std::uint32_t(s.r[3]))+1)
        throw std::runtime_error("_sys_qsort guest range overflow");
    // Even size=0 can call the comparator in this firmware: no blanket no-op.
    throw std::runtime_error("_sys_qsort requires resumable guest comparator OPD callbacks on the calling PPU thread");
}
struct GuestMiscSpuPrintf {
    std::weak_ptr<std::array<std::uint32_t,8>> process;
    std::array<std::uint32_t,4> callbacks{};
};
static std::vector<GuestMiscSpuPrintf> guest_misc_spu_printf_registry;
static GuestMiscSpuPrintf& guest_misc_spu_printf_state(PpuState& s){
    auto& entries=guest_misc_spu_printf_registry;
    entries.erase(std::remove_if(entries.begin(),entries.end(),[](const auto& e){return e.process.expired();}),entries.end());
    for(auto& e:entries)if(e.process.lock()==s.thread_atexit)return e;
    entries.push_back({s.thread_atexit,{}});return entries.back();
}
[[maybe_unused]] static void guest_misc_spu_printf_initialize(PpuState& s){
    // Original 0x2800bf74..0x2800bf98 stores all four callback OPDs.
    std::array<std::uint32_t,4> callbacks{};
    for(unsigned i=0;i<4;++i)callbacks[i]=std::uint32_t(s.r[3+i]);
    guest_misc_spu_printf_state(s).callbacks=callbacks;
}
[[maybe_unused]] static void guest_misc_spu_printf_finalize(PpuState& s){
    guest_misc_spu_printf_state(s).callbacks={}; // Original 0x2800bf48.
}
[[maybe_unused]] static std::uint32_t guest_misc_spu_printf(PpuState& s,unsigned index){
    // Original attach 0x2800beec / detach 0x2800be90: no handler => ESTAT.
    if(!guest_misc_spu_printf_state(s).callbacks.at(index))return 0x8001000f;
    throw std::runtime_error("SPU printf requires execution of registered guest OPD callback");
}
'''
