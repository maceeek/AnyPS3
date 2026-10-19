"""Process facade, serial coordinator and wall-time accounting for generated C++.

The optional SPU executor is composed by par_worker without changing the serial
request contract. Domain backend definitions are adapted at composition time.
"""
import re

STATE_CPP = r'''
struct ProcessState {
    std::shared_ptr<std::array<std::uint32_t,8>> thread_atexit=std::make_shared<std::array<std::uint32_t,8>>();
    GuestMemory memory; NativeFilesystem fs; NativePad pad; NativeHid hid; NativeAudio audio; NativeSpu spu; std::uint64_t steps=0;
    std::vector<PpuContext> ppu_threads; bool reschedule=false; std::uint32_t next_thread=2,tls_address=0,tls_file_size=0,tls_memory_size=0;
    std::uint64_t atexit_callback = 0, at_exit_callback = 0;
    std::uint64_t sleep_microseconds = 0; Mmapper mmapper; KernelSync kernel; Events events; NativeVideo video; NativeSystemSettings settings; NativeTrophy trophy; std::set<std::uint64_t> sysutil_connections;
    bool module_loading=false; std::uint64_t module_id=0, module_lr=0, module_toc=0; std::set<std::uint64_t> loaded_modules;
    std::set<std::string> initialized_residents;
    NativeRsx rsx;
    NativePresent present;
    std::set<std::uint64_t> mutexes, user_allocations; std::uint32_t next_mutex_id = 0x95000000;
    std::uint64_t next_wait_sequence=1;
    bool spu_initialized=false; std::uint32_t spu_raw_limit=0, spu_group_limit=6;
    std::uint32_t sdk_version = 0; Lv2FsExt fs_ext; Lv2SyncExt sync_ext; Lv2SpuExt spu_ext; Lv2RsxNetExt rsx_net_ext;
    ExecutionTiming execution; ExecutionSupervisor supervisor;
    NativeRsxWorker rsx_worker;
    NativeSpuWorker spu_worker; // Destroy/join before every borrowed service.
    ProcessState()=default;
    ProcessState(const ProcessState&)=delete; ProcessState& operator=(const ProcessState&)=delete;
};
// Per-executor facade. Shared services never follow a PpuContext assignment.
struct PpuCpu : PpuContext { bool ppu_trace=false,ppu_defer_allowed=false,ppu_block_pending=false; std::uint64_t ppu_slice_end=UINT64_MAX; };
struct PpuState : PpuCpu {
private:
    std::unique_ptr<ProcessState> owned_process;
public:
    ProcessState& process;
    decltype(ProcessState::memory)& memory;
    decltype(ProcessState::fs)& fs;
    decltype(ProcessState::pad)& pad;
    decltype(ProcessState::hid)& hid;
    decltype(ProcessState::audio)& audio;
    decltype(ProcessState::spu)& spu;
    decltype(ProcessState::steps)& steps;
    decltype(ProcessState::ppu_threads)& ppu_threads;
    decltype(ProcessState::reschedule)& reschedule;
    decltype(ProcessState::next_thread)& next_thread;
    decltype(ProcessState::tls_address)& tls_address;
    decltype(ProcessState::tls_file_size)& tls_file_size;
    decltype(ProcessState::tls_memory_size)& tls_memory_size;
    decltype(ProcessState::atexit_callback)& atexit_callback;
    decltype(ProcessState::at_exit_callback)& at_exit_callback;
    decltype(ProcessState::sleep_microseconds)& sleep_microseconds;
    decltype(ProcessState::mmapper)& mmapper;
    decltype(ProcessState::kernel)& kernel;
    decltype(ProcessState::events)& events;
    decltype(ProcessState::video)& video;
    decltype(ProcessState::settings)& settings;
    decltype(ProcessState::trophy)& trophy;
    decltype(ProcessState::sysutil_connections)& sysutil_connections;
    decltype(ProcessState::module_loading)& module_loading;
    decltype(ProcessState::module_id)& module_id;
    decltype(ProcessState::module_lr)& module_lr;
    decltype(ProcessState::module_toc)& module_toc;
    decltype(ProcessState::loaded_modules)& loaded_modules;
    decltype(ProcessState::initialized_residents)& initialized_residents;
    decltype(ProcessState::rsx)& rsx;
    decltype(ProcessState::present)& present;
    decltype(ProcessState::mutexes)& mutexes;
    decltype(ProcessState::user_allocations)& user_allocations;
    decltype(ProcessState::next_mutex_id)& next_mutex_id;
    decltype(ProcessState::next_wait_sequence)& next_wait_sequence;
    decltype(ProcessState::spu_initialized)& spu_initialized;
    decltype(ProcessState::spu_raw_limit)& spu_raw_limit;
    decltype(ProcessState::spu_group_limit)& spu_group_limit;
    decltype(ProcessState::sdk_version)& sdk_version;
    decltype(ProcessState::fs_ext)& fs_ext;
    decltype(ProcessState::sync_ext)& sync_ext;
    decltype(ProcessState::spu_ext)& spu_ext;
    decltype(ProcessState::rsx_net_ext)& rsx_net_ext;
    decltype(ProcessState::execution)& execution;
    decltype(ProcessState::supervisor)& supervisor;
    PpuState():owned_process(std::make_unique<ProcessState>()),process(*owned_process),memory(process.memory),
        fs(process.fs),
        pad(process.pad),
        hid(process.hid),
        audio(process.audio),
        spu(process.spu),
        steps(process.steps),
        ppu_threads(process.ppu_threads),
        reschedule(process.reschedule),
        next_thread(process.next_thread),
        tls_address(process.tls_address),
        tls_file_size(process.tls_file_size),
        tls_memory_size(process.tls_memory_size),
        atexit_callback(process.atexit_callback),
        at_exit_callback(process.at_exit_callback),
        sleep_microseconds(process.sleep_microseconds),
        mmapper(process.mmapper),
        kernel(process.kernel),
        events(process.events),
        video(process.video),
        settings(process.settings),
        trophy(process.trophy),
        sysutil_connections(process.sysutil_connections),
        module_loading(process.module_loading),
        module_id(process.module_id),
        module_lr(process.module_lr),
        module_toc(process.module_toc),
        loaded_modules(process.loaded_modules),
        initialized_residents(process.initialized_residents),
        rsx(process.rsx),
        present(process.present),
        mutexes(process.mutexes),
        user_allocations(process.user_allocations),
        next_mutex_id(process.next_mutex_id),
        next_wait_sequence(process.next_wait_sequence),
        spu_initialized(process.spu_initialized),
        spu_raw_limit(process.spu_raw_limit),
        spu_group_limit(process.spu_group_limit),
        sdk_version(process.sdk_version),
        fs_ext(process.fs_ext),
        sync_ext(process.sync_ext),
        spu_ext(process.spu_ext),
        rsx_net_ext(process.rsx_net_ext),
        execution(process.execution),
        supervisor(process.supervisor){thread_atexit=process.thread_atexit;}
    explicit PpuState(ProcessState& owner):process(owner),memory(process.memory),
        fs(process.fs),
        pad(process.pad),
        hid(process.hid),
        audio(process.audio),
        spu(process.spu),
        steps(process.steps),
        ppu_threads(process.ppu_threads),
        reschedule(process.reschedule),
        next_thread(process.next_thread),
        tls_address(process.tls_address),
        tls_file_size(process.tls_file_size),
        tls_memory_size(process.tls_memory_size),
        atexit_callback(process.atexit_callback),
        at_exit_callback(process.at_exit_callback),
        sleep_microseconds(process.sleep_microseconds),
        mmapper(process.mmapper),
        kernel(process.kernel),
        events(process.events),
        video(process.video),
        settings(process.settings),
        trophy(process.trophy),
        sysutil_connections(process.sysutil_connections),
        module_loading(process.module_loading),
        module_id(process.module_id),
        module_lr(process.module_lr),
        module_toc(process.module_toc),
        loaded_modules(process.loaded_modules),
        initialized_residents(process.initialized_residents),
        rsx(process.rsx),
        present(process.present),
        mutexes(process.mutexes),
        user_allocations(process.user_allocations),
        next_mutex_id(process.next_mutex_id),
        next_wait_sequence(process.next_wait_sequence),
        spu_initialized(process.spu_initialized),
        spu_raw_limit(process.spu_raw_limit),
        spu_group_limit(process.spu_group_limit),
        sdk_version(process.sdk_version),
        fs_ext(process.fs_ext),
        sync_ext(process.sync_ext),
        spu_ext(process.spu_ext),
        rsx_net_ext(process.rsx_net_ext),
        execution(process.execution),
        supervisor(process.supervisor){thread_atexit=process.thread_atexit;}
    PpuState(const PpuState&)=delete; PpuState& operator=(const PpuState&)=delete;
};
'''

TYPES = r'''
#include <exception>
#include <cstdio>
#include <atomic>
#include <mutex>
enum class ExecutionPhase : unsigned { Ppu, Spu, Rsx, Audio, Idle, FlipWait, GpuWait, Coordinator, RsxWait, Count };
struct ExecutionTiming {
    using Clock=std::chrono::steady_clock;
    using Counts=std::array<std::uint64_t,static_cast<unsigned>(ExecutionPhase::Count)>;
    // Coordinator-only counters, just like phase attribution. No atomics on the hot path.
    // The paired-read gap is a sampled estimate, not guest execution time.
    struct ClockProbe {
        bool enabled=[]{const char* v=std::getenv("ANYPS3_PERF_REPORT");return v && std::string(v)=="1";}();
        std::uint64_t reads=0,probes=0,probe_ns=0;
        Clock::time_point now(){
            const auto result=Clock::now();
            if(enabled){const auto n=reads++;
                if((n&1023)==0){const auto end=Clock::now();
                    probe_ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(end-result).count();
                    ++probes;}}
            return result;
        }
        void report(const char* owner)const{
            std::fprintf(stderr,"PERF CLOCK owner=%s reads_total=%llu probes_total=%llu paired_gap_ns_total=%llu\n",owner,
                (unsigned long long)reads,(unsigned long long)probes,
                (unsigned long long)probe_ns);
        }
    };
    ClockProbe clock;
    std::uint64_t elided_clock_reads=0;

    bool enabled=[]{const char* v=std::getenv("ANYPS3_PERF_REPORT");return v && std::string(v)=="1";}();
    struct Sample { Clock::time_point at; std::uint64_t ppu,spu,flips,draws; Counts ns; };
    mutable std::optional<Sample> last;
    Counts ns{};

    // Main executor only, process lifetime. The hot path caches a stable map
    // node; no atomics, wall-clock reads or allocations per instruction/page.
    struct PpuMetrics { std::uint64_t instructions=0,ns=0,reported_instructions=0,reported_ns=0; };
    std::map<std::uint32_t,PpuMetrics> ppu_metrics;
    PpuMetrics* active_ppu_metrics=nullptr;
    std::uint32_t active_ppu_id=0;
    std::uint64_t accounted_ppu_steps=0;
    void account_ppu(std::uint64_t steps) {
        if(active_ppu_metrics)active_ppu_metrics->instructions+=steps-accounted_ppu_steps;
        accounted_ppu_steps=steps;
    }
    void select_ppu(std::uint32_t id,std::uint64_t steps) {
        account_ppu(steps);
        if(active_ppu_metrics && active_ppu_id==id)return;
        // Only an open PPU interval belongs to the previous id. Other phases
        // need no boundary when the active id changes.
        if(phase==ExecutionPhase::Ppu)transition(phase,clock.now());
        else ++elided_clock_reads;
        active_ppu_id=id;active_ppu_metrics=&ppu_metrics[id];
    }

    ExecutionPhase phase=ExecutionPhase::Count;
    Clock::time_point at{};
    void transition(ExecutionPhase next,Clock::time_point now) {
        if(phase!=ExecutionPhase::Count){
            const auto elapsed=std::chrono::duration_cast<std::chrono::nanoseconds>(now-at).count();
            ns[static_cast<unsigned>(phase)]+=elapsed;
            if(phase==ExecutionPhase::Ppu && active_ppu_metrics)active_ppu_metrics->ns+=elapsed;
        }
        phase=next;at=now;
    }
    Counts snapshot(Clock::time_point now) const {
        auto result=ns;
        if(phase!=ExecutionPhase::Count)result[static_cast<unsigned>(phase)]+=
            std::chrono::duration_cast<std::chrono::nanoseconds>(now-at).count();
        return result;
    }
};

// A page can finish normally, throw PpuBlocked, or call nested guest code.
// Account the monotonic process counter once, before scheduling the next id.
struct ExecutionPpuPage {
    ExecutionTiming* timing=nullptr;
    const std::uint64_t& steps;
    ExecutionPpuPage(ExecutionTiming& owner,std::uint32_t id,const std::uint64_t& count):steps(count){
        if(owner.enabled){timing=&owner;owner.select_ppu(id,count);}
    }
    ~ExecutionPpuPage(){if(timing)timing->account_ppu(steps);}
    ExecutionPpuPage(const ExecutionPpuPage&)=delete;
    ExecutionPpuPage& operator=(const ExecutionPpuPage&)=delete;
};

// TLS only selects the serial coordinator's metrics, never synchronizes guest data.
inline thread_local ExecutionTiming* execution_timing=nullptr;
struct ExecutionScope {
    ExecutionTiming* timing=nullptr;
    ExecutionTiming* previous_timing=nullptr;
    ExecutionPhase previous_phase=ExecutionPhase::Count,previous_owner_phase=ExecutionPhase::Count;
    ExecutionScope(ExecutionTiming& owner,ExecutionPhase next) {
        if(!owner.enabled)return;
        timing=&owner;previous_timing=execution_timing;previous_phase=owner.phase;
        // Distinct process reentry must pause the outer owner as well.
        if(previous_timing==timing && previous_phase==next){++owner.elided_clock_reads;return;}
        const auto now=owner.clock.now();
        if(previous_timing && previous_timing!=timing){previous_owner_phase=previous_timing->phase;previous_timing->transition(ExecutionPhase::Count,now);}
        owner.transition(next,now);execution_timing=timing;
    }
    explicit ExecutionScope(ExecutionPhase next) {
        if(execution_timing){timing=execution_timing;previous_timing=timing;previous_phase=timing->phase;
            if(previous_phase!=next)timing->transition(next,timing->clock.now());
            else ++timing->elided_clock_reads;}
    }
    ~ExecutionScope() {
        if(!timing)return;
        if(previous_timing==timing && timing->phase==previous_phase){
            ++timing->elided_clock_reads;execution_timing=previous_timing;return;
        }
        const auto now=timing->clock.now();
        timing->transition(previous_phase,now);execution_timing=previous_timing;
        if(previous_timing && previous_timing!=timing)previous_timing->transition(previous_owner_phase,now);
    }
    ExecutionScope(const ExecutionScope&)=delete;
    ExecutionScope& operator=(const ExecutionScope&)=delete;
};
template<class Function,class... Args>
static inline auto execution_gpu_wait(Function function,Args... args) {
    ExecutionScope scope(ExecutionPhase::GpuWait);return function(args...);
}
enum class ExecutionEngine : unsigned { Spu, Rsx, Audio };
struct EngineStepRequest { ExecutionEngine engine; unsigned budget=0; };
struct EngineStepReply {
    // Only SPU/RSX have a progress predicate. Audio completion says only that its
    // existing tick ran; it is not a claim of submitted or audible samples.
    bool progress=false; bool completed=false;
};
struct ExecutionSupervisor {
    std::mutex mutex;
    std::atomic<std::uint64_t> sequence{0};
    std::uint64_t failed_sequence=0;
    std::uint64_t next_sequence(){return sequence.fetch_add(1,std::memory_order_relaxed)+1;}
    void record(ExecutionEngine engine,std::uint64_t seq,std::uint32_t pc,std::uint32_t tid,std::exception_ptr error){
        std::lock_guard<std::mutex> lock(mutex);
        if(!first_error){first_error=error;failed_engine=engine;failed_sequence=seq;failed_pc=pc;failed_thread=tid;}
    }
    std::exception_ptr first_error;
    ExecutionEngine failed_engine=ExecutionEngine::Spu;
    std::uint32_t failed_pc=0,failed_thread=0;
};
'''

CPP = r'''
static bool spu_run_inline(PpuState&,unsigned);
static bool rsx_run_inline(PpuState&,unsigned);
static void audio_run_inline(PpuState&);
[[maybe_unused]] static EngineStepReply engine_step(PpuState& s,EngineStepRequest request) {
    auto& supervisor=s.supervisor;const auto sequence=supervisor.next_sequence();
    const auto phase=request.engine==ExecutionEngine::Spu?ExecutionPhase::Spu:
        request.engine==ExecutionEngine::Rsx?ExecutionPhase::Rsx:ExecutionPhase::Audio;
    ExecutionScope scope(s.execution,phase);
    try {
        switch(request.engine){
        case ExecutionEngine::Spu:return {spu_run_inline(s,request.budget),true};
        case ExecutionEngine::Rsx:return {rsx_run_inline(s,request.budget),true};
        case ExecutionEngine::Audio:audio_run_inline(s);return {false,true};
        }
        throw std::runtime_error("unknown execution engine");
    } catch(...) {
        supervisor.record(request.engine,sequence,s.pc,s.thread_id,std::current_exception());
        throw; // Preserve the original stop, PC, steps and exception type.
    }
}
[[maybe_unused]] static bool spu_run(PpuState& s,unsigned budget) {
    return engine_step(s,{ExecutionEngine::Spu,budget}).progress;
}
[[maybe_unused]] static bool rsx_run(PpuState& s,unsigned budget) {
    return engine_step(s,{ExecutionEngine::Rsx,budget}).progress;
}
[[maybe_unused]] static void audio_run(PpuState& s) {
    (void)engine_step(s,{ExecutionEngine::Audio,0});
}
'''

# Skip C++ literals/comments: diagnostics and GLSL source must stay byte-identical.
_GPU_WAITS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?\*/|\b(vkWaitForFences|vkWaitSemaphores|vkQueueWaitIdle|vkDeviceWaitIdle)\(', re.S)


def instrument_gpu_waits(source: str) -> str:
    return _GPU_WAITS.sub(lambda m: f'execution_gpu_wait({m[1]},' if m[1] else m[0], source)


def adapt_engines(source: str) -> str:
    """Wrap definitions, preserving every call (including nested flip/SPU ticks)."""
    source = re.sub(r'(static (?:bool|void) )(spu_run|rsx_run|audio_run)(\(PpuState&)',
                    r'\1\2_inline\3', source)
    # This is a lexical scope around the existing vblank wait, including its
    # polling overhead. Nested SPU time is charged exclusively to SPU.
    source = source.replace('while(std::chrono::steady_clock::now()<until){',
        'ExecutionScope flip_wait(ExecutionPhase::FlipWait);\n'
        '        while(std::chrono::steady_clock::now()<until){')
    return instrument_gpu_waits(source)
