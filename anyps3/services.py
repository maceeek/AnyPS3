"""Explicitly bounded native services for the startup experiment.

Unsupported scheduling and error-path semantics trap rather than returning success.
This is not a replacement for a complete PS3 kernel/module runtime.
"""

from .lightweight_sync import CPP as LIGHTWEIGHT_CPP
from .guest_libc import IMPORTS as LIBC_IMPORTS, CPP as LIBC_CPP
from .import_resources import IMPORTS as RESOURCE_IMPORTS, CPP as RESOURCE_CPP
from .guest_printf import IMPORTS as PRINTF_IMPORTS, CPP as PRINTF_CPP
from .guest_heap import IMPORTS as HEAP_IMPORTS, CPP as HEAP_CPP
from .guest_misc import IMPORTS as MISC_IMPORTS, CPP as MISC_CPP
from .native_hid import IMPORTS as HID_IMPORTS, CPP as HID_CPP
from .lv2_fs_ext import CPP as FS_EXT_CPP
from .lv2_sync_ext import CPP as SYNC_EXT_CPP
from .lv2_spu_ext import CPP as SPU_EXT_CPP
from .lv2_rsx_net_ext import CPP as RSX_NET_EXT_CPP

from .native_audio_api import IMPORTS as AUDIO_IMPORTS

IMPORTS = {
    # PS3 stack address-space classification, also used by FIOS DMA setup.
    ("sysPrxForUser", "0x4f7172c9"): ("sys_process_is_stack", "r[3]=((r[3]&UINT32_MAX)>>28)==0xd;"),
    ("sysPrxForUser", "0xda0eb71a"): ("sys_lwcond_create", "r[3]=sign_word(lwcond_service(state,0));"),
    ("sysPrxForUser", "0x1c9a942c"): ("sys_lwcond_destroy", "r[3]=sign_word(lwcond_service(state,1));"),
    ("sysPrxForUser", "0x2a6d9d51"): ("sys_lwcond_wait", "if(!lwsync_trace_enabled()){r[3]=sign_word(lwcond_service(state,2));}else{const auto o=r[3];const auto mx=lwsync_cond_mutex(state,o);const auto b=lwsync_owner(state,mx);r[3]=sign_word(lwcond_service(state,2));lwsync_trace(state,\"cwait\",o,mx,b);}"),
    ("sysPrxForUser", "0xef87a695"): ("sys_lwcond_signal", "if(!lwsync_trace_enabled()){r[3]=sign_word(lwcond_service(state,3));}else{const auto o=r[3];const auto mx=lwsync_cond_mutex(state,o);const auto b=lwsync_owner(state,mx);r[3]=sign_word(lwcond_service(state,3));lwsync_trace(state,\"csignal\",o,mx,b);}"),
    ("sysPrxForUser", "0xe9a1bd84"): ("sys_lwcond_signal_all", "if(!lwsync_trace_enabled()){r[3]=sign_word(lwcond_service(state,4));}else{const auto o=r[3];const auto mx=lwsync_cond_mutex(state,o);const auto b=lwsync_owner(state,mx);r[3]=sign_word(lwcond_service(state,4));lwsync_trace(state,\"csignalall\",o,mx,b);}"),
    # Firmware 3.01 libsysmodule at 0x312c only returns zero; initialization
    # happens in module_start, represented here by the linked native catalog.
    # Verified analysis ELF SHA256 8e23e1fa3ef7f35b06ed5260bd836a95ad61c55d886da674a8a22d10fa639f02.
    ("cellSysmodule", "0x63ff6ff9"): ("cellSysmoduleInitialize", "r[3]=0;"),
    ("cellSysutil", "0x1e7bff94"): ("cellSysCacheMount", "r[3]=sign_word(native_cache_mount(state));"),
    ("cellSysutil", "0x744c1544"): ("cellSysCacheClear", "r[3]=sign_word(state.fs.clear_cache());"),
    # Public presentation policy is owned by the native backend. Avoid the
    # firmware's undocumented sys_rsx_attribute queries; no success stub for 677.
    # Diagnostic only: the guest back chain identifies which game call site
    # selected the presentation policy (ELF64 ABI: LR save slot at 16(back chain)).
    ("cellGcmSys", "0x4ae8d215"): ("cellGcmSetFlipMode", "rsx_set_flip_mode(state,1);"
        "{auto frame=r[1];std::fprintf(stderr,\"native GCM flip mode callers lr=%#llx\",(unsigned long long)state.lr);"
        "for(unsigned i=0;i<3 && frame;++i){frame=state.memory.load(frame,8);if(!frame)break;"
        "std::fprintf(stderr,\" %#llx\",(unsigned long long)state.memory.load(frame+16,8));}std::fprintf(stderr,\"\\n\");}"),
    ("cellGcmSys", "0x55b4350b"): ("cellGcmSetFlipMode2", "rsx_set_flip_mode(state,0);"),
    ("cellAudio", "0x0b168f92"): ("cellAudioInit", "r[3]=sign_word(audio_service(state,0));"),
    ("cellAudio", "0xca5ac370"): ("cellAudioQuit", "r[3]=sign_word(audio_service(state,1));"),
    ("cellAudio", "0xcd7bc431"): ("cellAudioPortOpen", "r[3]=sign_word(audio_service(state,2));"),
    ("cellAudio", "0x74a66af0"): ("cellAudioGetPortConfig", "r[3]=sign_word(audio_service(state,3));"),
    ("cellAudio", "0x89be28f2"): ("cellAudioPortStart", "r[3]=sign_word(audio_service(state,4));"),
    ("cellAudio", "0x5b1e2c73"): ("cellAudioPortStop", "r[3]=sign_word(audio_service(state,5));"),
    ("cellAudio", "0x4129fe2d"): ("cellAudioPortClose", "r[3]=sign_word(audio_service(state,6));"),
    ("sys_io", "0x1cf98800"): ("cellPadInit", "r[3]=sign_word(pad_service(state,0));"),
    ("sys_io", "0x4d9b75d5"): ("cellPadEnd", "r[3]=sign_word(pad_service(state,1));"),
    ("sys_io", "0xa703a51d"): ("cellPadGetInfo2", "r[3]=sign_word(pad_service(state,2));"),
    ("sys_io", "0x578e3c98"): ("cellPadSetPortSetting", "r[3]=sign_word(pad_service(state,3));"),
    ("sys_io", "0x8b72cda1"): ("cellPadGetData", "r[3]=sign_word(pad_service(state,4));"),
    ("sys_io", "0xf65544ee"): ("cellPadSetActDirect", "r[3]=sign_word(pad_service(state,5));"),
    ("sysPrxForUser", "0xe0998dbf"): ("sys_prx_get_module_id_by_name", "native_module_by_name(state);"),
    ("sysPrxForUser", "0x24a1ea07"): ("sys_ppu_thread_create", "ppu_thread_create(state);"),
    ("sysPrxForUser", "0xaff080a4"): ("sys_ppu_thread_exit", "ppu_thread_exit_wrapper(state);"),
    ("sysPrxForUser", "0x052d29a6"): ("_sys_strcat", "guest_strcpy(state,true);"),
    ("sysPrxForUser", "0x99c88692"): ("_sys_strcpy", "guest_strcpy(state);"),
    ("sysPrxForUser", "0xebe5f72f"): ("sys_spu_image_import", "guest_spu_image_import(state);"),
    ("sysPrxForUser", "0xdc578057"): ("sys_mmapper_map_memory", "r[11]=334; r[3]=sign_word(mmapper_syscall(state));"),
    ("sysPrxForUser", "0x4643ba6e"): ("sys_mmapper_unmap_memory", "r[11]=335; r[3]=sign_word(mmapper_syscall(state));"),
    ("sysPrxForUser", "0x409ad939"): ("sys_mmapper_free_memory", "r[11]=329; r[3]=sign_word(mmapper_syscall(state));"),
    ("sysPrxForUser", "0xb257540b"): ("sys_mmapper_allocate_memory", "r[6]=r[5]&UINT32_MAX; r[5]=r[4]; r[4]=r[3]&UINT32_MAX; r[3]=UINT64_C(0xffff000000000000); r[11]=332; r[3]=sign_word(mmapper_syscall(state));"),
    ("sysPrxForUser", "0x459b4393"): ("_sys_strcmp", "r[3] = guest_strcmp(state,r[3],r[4]);"),
    ("sysPrxForUser", "0x04e83d2c"): ("_sys_strncmp", "r[3] = guest_strncmp(state,r[3],r[4],std::uint32_t(r[5]));"),
    ("sysPrxForUser", "0x2d36462b"): ("_sys_strlen", "r[3] = guest_strlen(state,r[3]);"),
    ("sysPrxForUser", "0x6bf66ea7"): ("_sys_memcpy", "guest_copy(state,false);"),
    ("sysPrxForUser", "0x68b9b011"): ("_sys_memset", "guest_copy(state,true);"),
    ("sysPrxForUser", "0xb5d5f64e"): ("sysPrxForUser_64k_address_area", "mmapper_address_area(state);"),
    ("sysPrxForUser", "0x8461e528"): ("sys_time_get_system_time", "r[3] = monotonic_microseconds();"),
    ("sysPrxForUser", "0x744680a2"): ("sys_initialize_tls", "initialize_tls(state);"),
    ("sysPrxForUser", "0x2f85c0ef"): ("sys_lwmutex_create", "lwmutex_create(state);"),
    ("sysPrxForUser", "0x1573dc3f"): ("sys_lwmutex_lock", "lwmutex_import(state,false);"),
    ("sysPrxForUser", "0x1bc200f4"): ("sys_lwmutex_unlock", "lwmutex_import(state,true);"),
    ("sysPrxForUser", "0xc3476d0c"): ("sys_lwmutex_destroy", "r[3] = sign_word(lwmutex_destroy(state));"),
    ("sysPrxForUser", "0x350d454e"): ("sys_ppu_thread_get_id", "state.memory.store(r[3], 8, state.thread_id); r[3] = 0;"),
    ("sysPrxForUser", "0x2c847572"): ("_sys_process_atexitspawn", "if (!state.atexit_callback) state.atexit_callback = r[3];"),
    ("sysPrxForUser", "0x96328741"): ("_sys_process_at_Exitspawn", "if (!state.at_exit_callback) state.at_exit_callback = r[3];"),
}
assert not set(LIBC_IMPORTS) & set(IMPORTS)
IMPORTS.update(LIBC_IMPORTS)
assert not set(RESOURCE_IMPORTS) & set(IMPORTS)
IMPORTS.update(RESOURCE_IMPORTS)
assert not set(PRINTF_IMPORTS) & set(IMPORTS)
IMPORTS.update(PRINTF_IMPORTS)
assert not set(HEAP_IMPORTS) & set(IMPORTS)
IMPORTS.update(HEAP_IMPORTS)
assert not set(MISC_IMPORTS) & set(IMPORTS)
IMPORTS.update(MISC_IMPORTS)
assert not set(HID_IMPORTS) & set(IMPORTS)
IMPORTS.update(HID_IMPORTS)

# Only validated load/store/transfer APIs, coordinator fields or host time.
# Deliberately exclude strcpy/strcat/strchr/strncpy/strncasecmp: retained raw
# access() pointers require the compatibility scope. Heap/audio/RSX/SPU APIs
# likewise keep their existing admission. Mutex imports do their own fallback.
FAST_IMPORTS = frozenset({
    "sys_ppu_thread_get_id", "sys_time_get_system_time", "sys_process_is_stack",
    "_sys_strlen", "_sys_strcmp", "_sys_strncmp", "_sys_memcpy", "_sys_memset",
    "_sys_memcmp", "_sys_memmove", "sys_lwmutex_lock", "sys_lwmutex_unlock",
})

SERVICES_CPP = r'''
// Coordinator-only counters: one cached TLS node per generated import,
// direct indexing for ordinary syscalls; no map lookup/clock in counted calls.
struct HleMetric;
inline thread_local HleMetric* hle_metric_head=nullptr;
struct HleMetric {
    const char* kind=nullptr;const char* name=nullptr;std::uint64_t number=0;
    HleMetric* next=nullptr;
    struct Counts {std::uint64_t calls=0,samples=0,enter_ns=0,exit_ns=0;} total,reported;
    HleMetric()=default;
    HleMetric(const char* k,const char* n):kind(k),name(n),next(hle_metric_head){hle_metric_head=this;}
    void syscall(std::uint64_t op){kind="syscall";name="syscall";number=op;next=hle_metric_head;hle_metric_head=this;}
};
[[maybe_unused]] static HleMetric* hle_syscall_metric(std::uint64_t op){
    static thread_local std::array<HleMetric,1024> common;
    static thread_local std::map<std::uint64_t,HleMetric> other;
    auto& metric=op<common.size()?common[op]:other[op];
    if(!metric.kind)metric.syscall(op);
    return &metric;
}
// The ordinary executor also pumps every 256 guest instructions. Bound HLE-only
// host loops independently (including tests/nested calls), without raw admission
// or an unconditional service()/SPU pause on every small import.
inline thread_local unsigned hle_light_ticks=0;
[[maybe_unused]] static void hle_light_poll(PpuState& s){
    if(!s.process.spu_worker.enabled && !s.process.rsx_worker.enabled)return;
    if(++hle_light_ticks<64)return;
    hle_light_ticks=0;
    GuestMemory::RawYield yield(s.memory);
    auto& w=s.process.spu_worker;
    if(w.enabled){w.service();w.check();}
    else {rsx_worker_pump(s);s.memory.service_coherence();}
}
// GCC 16 -O2 reports -Wmaybe-uninitialized for the destroy path of the disengaged optional inside emplace()
// (the static-local timing guard makes `legacy` escape); the path is unreachable. Scoped to this type only.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"
#endif
struct HleImportScope {
    using Clock=std::chrono::steady_clock;
    HleMetric* metric=nullptr;bool sampled=false;
    std::optional<SpuServiceScope> legacy;
    explicit HleImportScope(PpuState& s,HleMetric* counts,bool light,bool atomic_only=false){
        if(s.execution.enabled && counts){metric=counts;++metric->total.calls;
            static const bool timing=[] {const char* v=std::getenv("ANYPS3_HLE_SCOPE_TIMING");return v && std::string(v)=="1";}();
            sampled=timing && metric->total.calls%1021==1;
        }
        const auto begin=sampled?Clock::now():Clock::time_point{};
        try{if(light)hle_light_poll(s);else legacy.emplace(s,atomic_only);}
        catch(...){if(sampled){++metric->total.samples;metric->total.enter_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-begin).count();}throw;}
        if(sampled){++metric->total.samples;metric->total.enter_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-begin).count();}
    }
    ~HleImportScope(){
        if(sampled){const auto begin=Clock::now();legacy.reset();metric->total.exit_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now()-begin).count();}
    }
    HleImportScope(const HleImportScope&)=delete;
};
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC diagnostic pop
#endif
// Hook once in the existing two-second PERF report; see outside-ownership.patch.
[[maybe_unused]] static void hle_perf_report(const PpuState& s){
    if(!s.execution.enabled)return;
    for(auto* m=hle_metric_head;m;m=m->next){const auto& t=m->total;auto& r=m->reported;
        if(t.calls!=r.calls)std::fprintf(stderr,"PERF HLE kind=%s name=%s number=%" PRIu64 " calls=%" PRIu64 " calls_total=%" PRIu64 " scope_samples=%" PRIu64 " scope_enter_ns=%" PRIu64 " scope_exit_ns=%" PRIu64 "\n",
            m->kind,m->name,m->number,t.calls-r.calls,t.calls,t.samples-r.samples,t.enter_ns-r.enter_ns,t.exit_ns-r.exit_ns);
        r=t;
    }
}

[[maybe_unused]] static std::uint64_t guest_strlen(PpuState& state, std::uint64_t address) {
    if (!address) return 0; // Verified local firmware _sys_strlen behavior.
    for (std::uint64_t size=0;size<1024*1024;++size)
        if (!state.memory.load(address+size,1)) return size;
    throw std::runtime_error("guest string exceeds diagnostic limit");
}
[[maybe_unused]] static std::uint64_t guest_strcmp(PpuState& state,std::uint64_t a,std::uint64_t b) {
    for (std::uint64_t i=0;i<1024*1024;++i) {
        const auto x=state.memory.load(a+i,1), y=state.memory.load(b+i,1);
        if (x!=y) return x<y?UINT64_MAX:1;
        if (!x) return 0;
    }
    throw std::runtime_error("guest strcmp exceeds diagnostic limit");
}
[[maybe_unused]] static std::uint64_t guest_strncmp(PpuState& state,std::uint64_t a,std::uint64_t b,std::uint32_t count){
    for(std::uint32_t i=0;i<count;++i){
        if(i==1024*1024)throw std::runtime_error("guest strncmp exceeds diagnostic limit");
        const auto x=state.memory.load(a+i,1),y=state.memory.load(b+i,1);
        if(x!=y)return std::uint64_t(std::int64_t(x)-std::int64_t(y));
        if(!x)return 0;
    }
    return 0;
}
[[maybe_unused]] static void guest_strcpy(PpuState& state,bool append=false) {
    const auto src=state.r[4],size=guest_strlen(state,src)+1;
    const auto dst=state.r[3]+(append?guest_strlen(state,state.r[3]):0);
    if(dst<src+size && src<dst+size)throw std::runtime_error("overlapping guest strcpy");
    auto* out=state.memory.access(dst,size,true);
    const auto* in=state.memory.access(src,size,false);std::copy_n(in,size,out);
}
[[maybe_unused]] static void guest_copy(PpuState& state, bool fill) {
    const auto dst=state.r[3], src=state.r[4], size=state.r[5];
    if (size>16*1024*1024) throw std::runtime_error("guest memory operation exceeds diagnostic limit");
    if (!size) return;
    // Ranges may span separately mapped but adjacent regions; transfer validates all of it.
    std::vector<std::uint8_t> bytes(size,static_cast<std::uint8_t>(src));
    if (fill) state.memory.transfer(dst,bytes.data(),unsigned(size),true);
    else {
        if (dst<src+size && src<dst+size) throw std::runtime_error("overlapping guest memcpy");
        state.memory.transfer(src,bytes.data(),unsigned(size),false);
        state.memory.transfer(dst,bytes.data(),unsigned(size),true);
    }
}

[[maybe_unused]] static void lwmutex_create(PpuState& state) {
    const auto object = state.r[3], attr = state.r[4];
    auto& mem = state.memory;
    mem.access(object, 24, true);
    const auto protocol = mem.load(attr, 4), recursive = mem.load(attr + 4, 4);
    if ((protocol != 1 && protocol != 2 && protocol != 4) || (recursive != 16 && recursive != 32))
        throw std::runtime_error("unsupported/invalid lightweight mutex attributes");
    if (state.mutexes.count(object)) throw std::runtime_error("duplicate lightweight mutex creation");
    state.mutexes.insert(object);
    mem.store(object, 8, UINT64_C(0xffffffff00000000));
    mem.store(object + 8, 4, protocol | recursive);
    mem.store(object + 12, 4, 0);
    mem.store(object + 16, 4, state.next_mutex_id++);
    state.r[3] = 0;
}
[[maybe_unused]] static void lwmutex_lock(PpuState& state) {
    const auto object = state.r[3]; auto& mem = state.memory;
    if (!state.mutexes.count(object)) throw std::runtime_error("unknown lightweight mutex");
    const auto owner = mem.load(object, 4);
    if (owner == UINT32_MAX) {
        if(state.lwmutex_waiting){
            if(state.lwmutex_waiting!=object || !mem.load(object+4,4))throw std::runtime_error("inconsistent lightweight mutex waiter");
            mem.store(object+4,4,mem.load(object+4,4)-1);state.lwmutex_waiting=0;
        }
        mem.store(object,4,state.thread_id);state.wait_deadline=0;state.wait_timed_out=false;
    }
    else if (owner == state.thread_id && (mem.load(object + 8, 4) & 16)) {
        const auto count = mem.load(object + 12, 4);
        if (count == UINT32_MAX) throw std::runtime_error("mutex recursion exhausted");
        mem.store(object + 12, 4, count + 1);
    } else if(owner==state.thread_id){state.r[3]=0x80010008;return;}
    else {
        if(state.r[4]){ // timeout in microseconds (sys_lwmutex_lock); deadline survives retries
            if(state.wait_timed_out){
                state.wait_timed_out=false;state.wait_deadline=0;
                if(state.lwmutex_waiting){mem.store(object+4,4,mem.load(object+4,4)-1);state.lwmutex_waiting=0;}
                state.r[3]=0x8001000b;return; // CELL_ETIMEDOUT
            }
            if(!state.wait_deadline)state.wait_deadline=sync_ext_now()+state.r[4];
        }else if(state.ppu_threads.size()<2)throw std::runtime_error("lightweight mutex wait requires an active peer");
        if(!state.lwmutex_waiting){
            const auto count=mem.load(object+4,4);
            if(count==UINT32_MAX)throw std::runtime_error("lightweight mutex waiter count overflow");
            mem.store(object+4,4,count+1);state.lwmutex_waiting=object;
        }
        ppu_defer_block(state,7,object);return;
    }
    state.r[3] = 0;
}
[[maybe_unused]] static void lwmutex_unlock(PpuState& state) {
    const auto object = state.r[3]; auto& mem = state.memory;
    if (!state.mutexes.count(object)) throw std::runtime_error("unlock of unknown lightweight mutex");
    if (const auto owner = mem.load(object, 4); owner != state.thread_id) {
        // Firmware 3.01 liblv2 sys_lwmutex_unlock (0x2800d044 in local/ratchet/liblv2-relocated.elf) compares the
        // owner word with the caller's TLS thread id and returns CELL_EPERM without touching the mutex.
        static std::atomic<unsigned> reported{0};
        const auto count = reported.fetch_add(1, std::memory_order_relaxed);
        if (count < 16)
            std::fprintf(stderr, "lwmutex unlock by non-owner -> CELL_EPERM thread=%u mutex=0x%08x owner=%08x lr=0x%08x\n",
                unsigned(state.thread_id), unsigned(object), unsigned(owner), unsigned(state.lr));
        static const bool history = anyps3_trace_enabled("ANYPS3_TRACE_LWSYNC");
        if (history && count < 4) anyps3_trace_dump();
        state.r[3] = 0x80010009; return;
    }
    const auto count = mem.load(object + 12, 4);
    if (count) mem.store(object + 12, 4, count - 1);
    else {
        mem.store(object, 4, UINT32_MAX);
        if(mem.load(object+4,4))state.reschedule=true;
    }
    state.r[3] = 0;
}
// Diagnostic only (ANYPS3_TRACE_LWSYNC=1): lightweight mutex/condition transitions in the STOP trace history.
[[maybe_unused]] static bool lwsync_trace_enabled(){static const bool enabled=anyps3_trace_enabled("ANYPS3_TRACE_LWSYNC");return enabled;}
[[maybe_unused]] static void lwsync_trace(PpuState& state,const char* op,std::uint64_t object,std::uint32_t mutex,std::uint32_t before){
    const std::uint32_t after=state.mutexes.count(mutex)?std::uint32_t(state.memory.load(mutex,4)):0;
    char line[192];std::snprintf(line,sizeof line,"lwsync %s thread=%u obj=0x%08x mutex=0x%08x owner %08x->%08x r3=%08x%s lr=%08x\n",
        op,unsigned(state.thread_id),unsigned(object),mutex,before,after,unsigned(state.r[3]),state.ppu_block_pending?" BLOCKED":"",unsigned(state.lr));
    anyps3_trace_history.append(line);
}
[[maybe_unused]] static std::uint32_t lwsync_cond_mutex(PpuState& state,std::uint64_t object){
    return state.kernel.lwconditions.count(object)?std::uint32_t(state.memory.load(object,4)):0;
}
[[maybe_unused]] static std::uint32_t lwsync_owner(PpuState& state,std::uint32_t mutex){
    return state.mutexes.count(mutex)?std::uint32_t(state.memory.load(mutex,4)):0;
}
[[maybe_unused]] static std::uint32_t lwmutex_destroy(PpuState& state) {
    // Firmware sys_lwmutex_destroy: own or held mutex and waiters are EBUSY;
    // success marks the user-space owner word as dead (0xfffffffe).
    const auto object = state.r[3]; auto& mem = state.memory;
    if (!state.mutexes.count(object)) return MM_ESRCH;
    if (mem.load(object, 4) != UINT32_MAX || mem.load(object + 4, 4)) return MM_EBUSY;
    for (const auto& thread : state.ppu_threads) {
        if (thread.wait_kind == 7 && thread.wait_id == object) return MM_EBUSY;
        if (thread.wait_kind == 8) {
            const auto c = state.kernel.lwconditions.find(thread.wait_id);
            if (c != state.kernel.lwconditions.end() && c->second.mutex == object) return MM_EBUSY;
        }
    }
    mem.store(object, 4, 0xfffffffe); state.mutexes.erase(object);
    return 0;
}
''' + LIGHTWEIGHT_CPP + LIBC_CPP + RESOURCE_CPP + PRINTF_CPP + HEAP_CPP + MISC_CPP + HID_CPP + FS_EXT_CPP + SYNC_EXT_CPP + SPU_EXT_CPP + RSX_NET_EXT_CPP + r'''
[[maybe_unused]] static std::uint64_t sleep_budget_microseconds(const char* value){
    if(!value)return 5000000;
    std::uint64_t limit=0;
    for(const char* p=value;*p;++p){
        if(*p<'0' || *p>'9' || limit>(UINT64_MAX-unsigned(*p-'0'))/10)
            throw std::runtime_error("invalid ANYPS3_SLEEP_BUDGET_US: expected positive decimal uint64");
        limit=limit*10+unsigned(*p-'0');
    }
    if(!limit)throw std::runtime_error("invalid ANYPS3_SLEEP_BUDGET_US: expected positive decimal uint64");
    return limit;
}
[[maybe_unused]] static void syscall(PpuState& state) {
    auto& r = state.r; auto& mem = state.memory;
    // Bounded trace history (dumped on STOP); live stderr only with ANYPS3_TRACE_SYSCALLS=1.
    // Per-syscall stderr writes cost the menu ~20k write(2) calls/s (profile 331ts).
    { char line[64]; std::snprintf(line, sizeof line, "syscall number=%" PRIu64 " pc=0x%08" PRIx32 "\n", r[11], state.pc);
      anyps3_trace_history.append(line);
      static const bool live = anyps3_trace_enabled("ANYPS3_TRACE_SYSCALLS");
      if (live) std::fputs(line, stderr); }
    switch (r[11]) {
    case 1: r[3] = 1; break; // sys_process_getpid: this runtime owns one process, ID 1.
    case 14: { // SPU reservation eligibility for mapped main-memory regions.
        const auto address=static_cast<std::uint32_t>(r[3]),flags=static_cast<std::uint32_t>(r[4]);
        if(!flags || (r[4]&~UINT64_C(6))){r[3]=MM_EINVAL;break;}
        if(address>>28==0xd){r[3]=MM_EPERM;break;}
        bool mapped=false;
        for(const auto& region:mem.regions)if(address>=region.base && address-region.base<region.bytes->size())mapped=true;
        r[3]=mapped?0:MM_EINVAL;break;
    }
    case 25: // sys_process_get_sdk_version, read from original process parameters
        if (r[3] != 1) throw std::runtime_error("SDK query for unknown process");
        mem.store(r[4],4,state.sdk_version); r[3]=0; break;
    case 41: ppu_thread_exit(state); break;
    case 44: r[3]=ppu_thread_join(state); break;
    case 47:case 48: { // Set/get priority of an existing native PPU context.
        const auto id=static_cast<std::uint32_t>(r[3]),value=static_cast<std::uint32_t>(r[4]);
        if(r[11]==47 && value>3071){r[3]=MM_EINVAL;break;}
        PpuContext* target=id==state.thread_id?&state:nullptr;
        if(!target)for(auto& thread:state.ppu_threads)if(thread.thread_id==id){target=&thread;break;}
        // An exited detached thread is removed by LV2 (RPCS3 sys_ppu_thread.cpp:82-130); only joinable
        // threads stay as zombies until joined. The game polls get_priority for ESRCH to detect exit.
        if(!target || (target!=&state && target->returned && !target->joinable)){r[3]=MM_ESRCH;break;}
        if(r[11]==48)mem.store(r[4],4,target->priority);
        else if(target->priority!=value){target->priority=value;state.reschedule=true;}
        r[3]=0;break;
    }
    case 90: case 91: case 92: case 93: case 94: case 95:
    case 100: case 101: case 102: case 103: case 104: case 105: case 106: case 107: case 108: case 109:
        r[3]=kernel_sync(state); break;
    case 128: case 129: case 130: case 131: case 133: case 134: case 135: case 136: case 137: case 138:
        r[3]=event_syscall(state); break;
    case 141: { // sys_timer_usleep
        // Only the calling PPU thread sleeps; other PPU threads, SPUs and RSX keep
        // running (RPCS3 sys_timer.cpp:450-483: lv2_obj::sleep + wait_timeout; 0 = yield).
        // Same per-thread timed wait as sys_timer_sleep (142); the retried syscall returns 0.
        if (state.sync_ext.waits.count(state.thread_id)) { r[3] = sync_ext_result(state); break; }
        // Diagnostic cap on total slept time (ANYPS3_SLEEP_BUDGET_US, default 5 s); not a semantic limit.
        static const auto budget=sleep_budget_microseconds(std::getenv("ANYPS3_SLEEP_BUDGET_US"));
        if (r[3] > 1000000 || state.sleep_microseconds + r[3] > budget)
            throw std::runtime_error("native sleep diagnostic budget exhausted");
        if (!r[3]) { state.reschedule = true; break; }
        state.sleep_microseconds += r[3];
        if (state.module_loading) {
            // Module initialization (e.g. libsysutil module_start) runs no other PPU
            // thread in this runtime (threads.py stops on scheduling there), so only
            // the host waits; SPUs get the slept interval at 3.2 GHz, 1 instruction/cycle.
            spu_run(state, static_cast<unsigned>(r[3] * 3200));
            { ExecutionScope idle(state.execution,ExecutionPhase::Idle);
              std::this_thread::sleep_for(std::chrono::microseconds(r[3])); }
            r[3] = 0; break;
        }
        sync_ext_arm(state, 12, 0, r[3]);ppu_defer_block(state,12,0);break;
    }
    case 144: { // Explicit runtime configuration: UTC, no daylight-saving offset.
        mem.access(r[3], 4, true); mem.access(r[4], 4, true);
        mem.store(r[3], 4, 0); mem.store(r[4], 4, 0); r[3] = 0; break;
    }
    case 145: { // sys_time_get_current_time
        mem.access(r[3], 8, true); mem.access(r[4], 8, true);
        const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        mem.store(r[3], 8, ns / 1000000000);
        mem.store(r[4], 8, ns % 1000000000);
        r[3] = 0; break;
    }
    case 147: r[3] = TIMEBASE_HZ; break; // sys_time_get_timebase_frequency
    case 169: // sys_spu_initialize sets process resource limits; no SPU code executes here.
        if(r[4]>5){r[3]=MM_EINVAL;break;}
        {
            const auto limit=6-r[4];unsigned pinned=0,largest=0;
            for(const auto& group:state.spu.groups){if(group.type==0x18)pinned+=group.count;else largest=std::max(largest,group.count);}
            if(pinned>=limit || largest>limit){r[3]=MM_EBUSY;break;}
        }
        state.spu_raw_limit=static_cast<std::uint32_t>(r[4]);state.spu_group_limit=6-state.spu_raw_limit;
        state.spu_initialized=true;r[3]=0;break;
    case 403: { // sys_tty_write, bounded native stdout sink
        if (r[3] > 15 || r[5] > 1024 * 1024) throw std::runtime_error("unsupported TTY channel/length");
        mem.access(r[6], 4, true);
        std::size_t written = 0;
        if (r[5]) written = std::fwrite(mem.access(r[4], static_cast<unsigned>(r[5]), false), 1, r[5], stdout);
        mem.store(r[6], 4, written);
        if (written != r[5]) throw std::runtime_error("native TTY write failed");
        r[3] = 0; break;
    }
    case 324: case 325: case 341: case 342: case 343:
    case 329: case 330: case 331: case 332: case 334: case 335: case 337:
        r[3] = mmapper_syscall(state); break;
    case 668:case 670:case 672:case 674:case 675: r[3]=rsx_syscall(state);break;
    case 801:case 802:case 803:case 804:case 808:case 809:case 817:case 818:case 819:case 820:case 822:case 840: r[3]=native_fs_syscall(state);break;
    case 170: case 171: case 172: case 173: case 178: case 184: case 185: case 186: case 187: case 188: case 251: case 252:
        r[3]=spu_syscall(state); break;
    case 348: { // sys_memory_allocate, zeroed host backing with PS3 page alignment
        const auto size=r[3], flags=r[4], out=r[5];
        const std::uint64_t align=(flags==0 || flags==0x400)?0x100000:flags==0x200?0x10000:0;
        if(!size || (align && size%align)){r[3]=MM_EALIGN;break;}
        if(!align){r[3]=MM_EINVAL;break;}
        const auto used=main_memory_used(state);
        if(used>0x10000000 || size>0x10000000-used){r[3]=MM_ENOMEM;break;}
        mem.access(out,4,true);
        std::uint64_t base=0x10000000;
        for(;;) {
            auto next=base;
            for(const auto& region:mem.regions)
                if(base<region.base+region.bytes->size() && region.base<base+size)next=std::max(next,region.base+region.bytes->size());
            for(const auto& area:state.mmapper.areas)
                if(base<area.base+area.size && area.base<base+size)next=std::max(next,area.base+area.size);
            if(next==base)break;
            base=(next+align-1)&~(align-1);
        }
        if(base+size>0xc0000000){r[3]=MM_ENOMEM;break;}
        state.mmapper.page_metadata.reserve(state.mmapper.page_metadata.size()+1);
        mem.map(base,size,6,nullptr,0);state.user_allocations.insert(base);
        mmapper_record_pages(state,base,static_cast<std::uint32_t>(align)); // for syscall 351
        mem.store(out,4,base);r[3]=0;break;
    }
    case 349: { // sys_memory_free: only allocation bases owned by this service
        GuestMemory::ConfigurationPermit configuration(mem);
        if(!state.user_allocations.count(r[3])){r[3]=MM_EINVAL;break;}
        auto it=std::find_if(mem.regions.begin(),mem.regions.end(),[&](const auto& region){return region.base==r[3];});
        if(it==mem.regions.end())throw std::runtime_error("missing native user allocation");
        mem.mark_write(it->bytes->data(),it->bytes->size());
        mem.allocated-=it->bytes->size();mem.regions.erase(it);mem.mapping_changed();state.user_allocations.erase(r[3]);mmapper_forget_pages(state,r[3]);r[3]=0;break;
    }
    case 352: { // sys_memory_get_user_memory_size
        constexpr std::uint64_t total = 256 * 1024 * 1024;
        const auto used=main_memory_used(state);
        if (used > total) throw std::runtime_error("configured main memory exhausted");
        mem.access(r[3], 8, true);
        mem.store(r[3], 4, total-state.mmapper.container_reserved);
        mem.store(r[3] + 4, 4, total - used);
        r[3] = 0; break;
    }
    default:
        // Extension groups (anyps3/lv2_*_ext.py) handle only syscalls they implement.
        if (syscall_fs_ext(state) || syscall_sync_ext(state) || syscall_spu_ext(state) || syscall_rsx_net_ext(state)) break;
        throw std::runtime_error("unimplemented LV2 syscall " + std::to_string(r[11]));
    }
}
'''

# Additional cellAudio ABI; audioOut continues through the original firmware.
IMPORTS.update(AUDIO_IMPORTS)
