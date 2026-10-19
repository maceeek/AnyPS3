"""Ahead-of-time multi-function translation with explicit diagnostic trap sites."""

from collections import deque
from dataclasses import dataclass
import hashlib
import json

from .sample_profile import TYPES as SAMPLE_PROFILE_TYPES
from .analysis import imports
from .elf import ElfImage
from .errors import AnyPS3Error
from .flow import FLOW_CPP, Operation, lift
from .runtime import MEMORY_CPP, STACK_POINTER, memory_loader
from .startup import TLS_CPP, process_setup
from .services import IMPORTS, SERVICES_CPP, FAST_IMPORTS
from .mmapper import MMAPPER_TYPES, MMAPPER_CPP
from .kernel_sync import TYPES as SYNC_TYPES, CPP as SYNC_CPP
from .events import TYPES as EVENT_TYPES, CPP as EVENT_CPP
from .sysutil_transport import CPP as SYSUTIL_CPP, TYPES as SYSUTIL_TYPES
from .native_fs import TYPES as FS_TYPES, CPP as FS_CPP
from .native_pad import TYPES as PAD_TYPES, CPP as PAD_CPP
from .native_hid import TYPES as HID_TYPES
from .lv2_fs_ext import TYPES as FS_EXT_TYPES
from .lv2_sync_ext import TYPES as SYNC_EXT_TYPES
from .lv2_spu_ext import TYPES as SPU_EXT_TYPES
from .lv2_rsx_net_ext import TYPES as RSX_NET_EXT_TYPES
from .native_audio import TYPES as AUDIO_TYPES, CPP as AUDIO_CPP
from .host_present import TYPES as PRESENT_TYPES
from .rsx import TYPES as RSX_TYPES, CPP as RSX_CPP
from .vulkan import CPP as VULKAN_CPP
from .spu_runtime import CPP as SPU_CPP, TYPES as SPU_TYPES
from .threads import CPP as THREAD_CPP, TYPES as THREAD_TYPES
from .par_worker import TYPES as WORKER_TYPES, CPP as WORKER_CPP, RSX_GAME_CPP, compose_execution
from .execution import TYPES as EXECUTION_TYPES, STATE_CPP as EXECUTION_STATE_CPP, CPP as EXECUTION_CPP, adapt_engines, instrument_gpu_waits

ATEXIT_RETURN = 0xfffe0010  # must match threads.py ATEXIT_RETURN
# PPUFAST M3: GPR/FPR/CR fields/LR/CTR/XER in page-function locals.
PPU_REGISTER_LOCALS = True
from .spu import generate_parts as generate_spu
from .floating import FLOAT_CPP
from .ppu_emit import PageEmitter, EmitOptions, HELPERS_CPP as PPU_EMIT_HELPERS, step_slow_cpp
from .vector import CPP as VECTOR_CPP
from .jump_tables import BCTR, detect as detect_jump_table
from .modules import module_lookup, module_available, resident_startup, MODULE_RETURN, validate_catalog, module_runtime, exported_bindings


@dataclass(frozen=True)
class Program:
    image: ElfImage
    entry: int
    toc: int
    operations: tuple[Operation, ...]
    traps: dict[int, str]
    diagnostic: bool
    process_entry: bool = False
    extra_entries: tuple[int, ...] = ()
    native_modules: tuple[dict, ...] = ()
    spu_images: tuple[ElfImage, ...] = ()
    spu_overlays: tuple[tuple[tuple[int,bytes],...],...] = ()
    jump_tables: tuple[dict, ...] = ()

    def block_entries(self) -> set[int]:
        heads = {self.entry, *self.extra_entries, *self.traps}
        heads.update(self.image.resolve_entry(address=m['start'])[0]
                     for m in self.native_modules if m.get('start'))
        for table in self.jump_tables:
            heads.update(table['targets'])
        addresses = {i.address for i in self.operations}
        for inst in self.operations:
            primary, xo = inst.word >> 26, (inst.word >> 1) & 1023
            if (primary in (16,18) or (primary == 19 and xo in (16,528))
                    or inst.word == 0 or inst.mnemonic == "sc"):
                heads.update(inst.successors)
                if inst.word == 0 or inst.mnemonic == "sc":
                    heads.add(inst.address)
                    heads.add((inst.address+4)&0xffffffff)
            if inst.address-4 not in addresses or inst.address >> 12 != (inst.address-4) >> 12:
                heads.add(inst.address)
        length = 0
        for inst in sorted(self.operations,key=lambda i:i.address):
            if inst.address in heads or length == 64:
                heads.add(inst.address)
                length = 0
            length += 1
        return heads & (addresses | self.traps.keys())

    def report(self) -> dict:
        return {"schema_version": 1, "elf_sha256": hashlib.sha256(self.image.data).hexdigest(),
                "entry": self.entry, "toc": self.toc, "diagnostic": self.diagnostic,
                "process_entry": self.process_entry,
                "extra_entries": self.extra_entries,
                "spu_images": [{"sha256":hashlib.sha256(image.data).hexdigest(),"entry":image.entry,
                                "overlays":[{"local_address":base,"size":len(payload),"sha256":hashlib.sha256(payload).hexdigest()}
                                            for base,payload in (self.spu_overlays[i] if i<len(self.spu_overlays) else ())]}
                               for i,image in enumerate(self.spu_images)],
                "native_modules": [{"ids":m['ids'],"name":m['name'],"start":m.get('start',0),"resident":m.get('resident',False)} for m in self.native_modules],
                "translated_instructions": len(self.operations),
                "ppu_dispatch_entries": len(self.block_entries()),
                "ppu_max_block_instructions": 64,
                "verified_jump_tables": list(self.jump_tables),
                "trap_count": len(self.traps),
                "traps": [{"address": a, "reason": r} for a, r in sorted(self.traps.items())],
                "note": "Static translation report, not execution or compatibility evidence. Unknown operations/services trap; none are skipped."}

    def cpp(self, *, max_steps: int = 1_000_000, trace: bool = False) -> str:
        """One translation unit; same generated code as cpp_units()."""
        p = self._parts(max_steps, trace)
        spu = [text for _, text in p["spu"]]
        return "\n".join([*p["header"], *p["declarations"], p["spu_helpers"], *spu, p["runtime"],
                          *p["operations"], *(text for _, text in p["ppu"]), p["entry"]])

    def cpp_units(self, *, max_steps: int = 1_000_000, units: int = 64, trace: bool = False) -> dict[str, str]:
        """Separate translation units for parallel compilation; link all .cpp files.

        Lifted pages see only the shared types and pure instruction helpers;
        imports/syscalls are external functions defined in program.cpp.
        """
        if not 1 <= units <= 4096:
            raise AnyPS3Error("Translation unit count must be between 1 and 4096.")
        p = self._parts(max_steps, trace)
        files = {"anyps3.hpp": "\n".join(["#pragma once", *p["header"], *p["declarations"], ""]),
                 "program.cpp": "\n".join(['#include "anyps3.hpp"', p["spu_helpers"], p["runtime"],
                                            *p["operations"], p["entry"]])}
        unit_prefix = ['// Shared static helpers unused by this unit are expected here.',
                       '#pragma GCC diagnostic ignored "-Wunused-function"', '#include "anyps3.hpp"']
        def distribute(prefix, items, extra):
            total = sum(len(text) for _, text in items)
            if not total:
                return
            # Contiguous bins by cumulative size: at most `units` files.
            bins, before = {}, 0
            for _, text in items:
                bins.setdefault(before * units // total, []).append(text); before += len(text)
            for i, texts in enumerate(bins.values()):
                files[f"{prefix}-{i:03d}.cpp"] = "\n".join([*unit_prefix, *extra, *texts, ""])
        distribute("ppu", p["ppu"], [])
        distribute("spu", p["spu"], [p["spu_helpers"]])
        return files

    def _parts(self, max_steps: int, trace: bool = False) -> dict:
        if not 1 <= max_steps <= 1_000_000_000_000:
            raise AnyPS3Error("Runtime step limit must be between 1 and 1000000000000.")
        if self.process_entry:
            spu_core, spu_functions, spu_dispatch = generate_spu(self.spu_images, self.spu_overlays)
        else:
            spu_core, spu_functions, spu_dispatch = "", [], ""
        lines = ["// AnyPS3 AOT diagnostic program. No PPU/SPU instruction decoding occurs at runtime.",
                 "#include <cstdint>", "#include <cstdio>", "#include <cinttypes>", "#include <set>", "#include <chrono>", "#include <optional>", "#include <atomic>", "#include <array>", "#include <thread>", SAMPLE_PROFILE_TYPES, MEMORY_CPP,
                 memory_loader(self.image), "constexpr std::uint32_t RETURN_SENTINEL = 0xfffffffc;",
                 MMAPPER_TYPES, SYNC_TYPES, EVENT_TYPES, SYSUTIL_TYPES, FS_TYPES, PAD_TYPES, HID_TYPES, AUDIO_TYPES, PRESENT_TYPES, EXECUTION_TYPES, instrument_gpu_waits(VULKAN_CPP), RSX_TYPES, SPU_TYPES, THREAD_TYPES, FS_EXT_TYPES, SYNC_EXT_TYPES, SPU_EXT_TYPES, RSX_NET_EXT_TYPES,
                 WORKER_TYPES, EXECUTION_STATE_CPP, "bool spu_owner_call(PpuState&,NativeSpu::Thread&,const std::function<void()>&);", FLOW_CPP, FLOAT_CPP, VECTOR_CPP, FS_CPP]
        spu_helpers = (TLS_CPP + MMAPPER_CPP + spu_core) if self.process_entry else ""
        runtime = "\n".join([(WORKER_CPP + compose_execution(EXECUTION_CPP) + adapt_engines(spu_dispatch + RSX_CPP.replace("m.regions.erase(it);","m.regions.erase(it);m.mapping_changed();")) + RSX_GAME_CPP + THREAD_CPP + SYSUTIL_CPP + SYNC_CPP + EVENT_CPP + SPU_CPP + PAD_CPP + adapt_engines(AUDIO_CPP.replace("m.regions.erase(it);","m.regions.erase(it);m.mapping_changed();")) + SERVICES_CPP + module_lookup(self.native_modules)) if self.process_entry else "",
                              module_runtime(self.image,self.native_modules) if self.native_modules else ""])
        declarations, operations = [], []
        # Page-local basic blocks keep compiler functions bounded to 4 KiB of
        # guest code. Only verified entries/continuations and bounded scheduling
        # cuts enter the switch; ordinary instructions fall through directly.
        pages = {}
        heads = self.block_entries()
        by_address = {inst.address: inst for inst in self.operations}
        lines.append("static inline void anyps3_ppu_step(PpuState& state,std::uint32_t pc) {")
        lines.extend(["    state.pc=pc;",
                      f'    if(state.steps>=UINT64_C({max_steps}))throw std::runtime_error("execution step limit reached");',
                      "    ++state.steps;",
                      '    if(state.ppu_trace)std::fprintf(stderr,"trace pc=0x%08" PRIx32 " lr=0x%08" PRIx64 " r1=0x%08" PRIx64 " r3=0x%016" PRIx64 "\\n",pc,state.lr,state.r[1],state.r[3]);',
                      "}"])
        import_names = {item[0] for item in IMPORTS.values()}
        def runtime_operation(address, mnemonic, code):
            declarations.append(f"void anyps3_op_{address:x}(PpuState& state);")
            name = mnemonic.removesuffix(" (single main thread)")
            may_defer = mnemonic == "sc" or name in ("sys_lwmutex_lock", "sys_lwcond_wait")
            deferred_scope = "    PpuDeferredScope disposition(state);\n" if may_defer else ""
            if self.process_entry and (mnemonic == "sc" or name in import_names):
                if mnemonic == "sc":
                    scope = ("    HleImportScope owner(state,state.execution.enabled?hle_syscall_metric(state.r[11]):nullptr,"
                             "false,spu_atomic_syscall(state.r[11]));\n")
                else:
                    scope = (f"    static thread_local HleMetric metric(\"import\",{json.dumps(name)});\n"
                             f"    HleImportScope owner(state,&metric,{'true' if name in FAST_IMPORTS else 'false'});\n")
            else:
                scope = "    SpuServiceScope owner(state);\n"
            # Runtime services may block or take raw permits: leave the
            # inline guest-memory section before any of their scopes.
            operations.append(f"void anyps3_op_{address:x}(PpuState& state) {{ // {mnemonic}\n"
                              f"    state.memory.inline_suspend();\n{scope}{deferred_scope}    auto& r=state.r; (void)r;\n    {code}\n}}")
            return f"anyps3_op_{address:x}(state);"
        # Page functions (ppu_emit.py) keep steps/PC (and registers) in locals:
        # block-granular accounting, exact publication before runtime calls and
        # in the single per-page catch, per-instruction cold path only for
        # trace and the final partial hard budget.
        ordered = sorted(self.operations, key=lambda x: x.address)
        blocks, block = [], []
        for inst in ordered:
            if block and inst.address in heads:
                blocks.append(block)
                block = []
            block.append(inst)
        if block:
            blocks.append(block)
        lines.extend([PPU_EMIT_HELPERS, step_slow_cpp(max_steps)])
        emitter = PageEmitter(EmitOptions(max_steps=max_steps, gpr_locals=PPU_REGISTER_LOCALS, trace=trace), heads, by_address, runtime_operation)
        for block in blocks:
            emitter.emit_block(block)
        for address, reason in sorted(self.traps.items()):
            emitter.emit_trap(address, reason)
        # Lifecycle trampolines execute one operation per dispatch, including
        # accounting before runtime calls that may throw PpuBlocked.
        for address, mnemonic, body in ([ (MODULE_RETURN,"native module return","native_module_return(state);") ] if self.native_modules else []) + ([ (ATEXIT_RETURN,"atexit callback return","ppu_atexit_continue(state);") ] if self.process_entry else []):
            call = runtime_operation(address,mnemonic,body)
            emitter.emit_lifecycle(address, call)
            # Account the synthetic operation in its runtime wrapper as well.
            operations[-1] = operations[-1].replace("    auto& r=",f"    anyps3_ppu_step(state,0x{address:x});\n    auto& r=")
        ppu = []
        pages = {}
        for page, text in emitter.functions():
            pages[page] = True
            declarations.append(f'void page_{page:x}(PpuState& state);')
            ppu.append((page, text))
        # Page dispatch tables: dense runs of guest pages (holes -> missing).
        ordered_pages = sorted(pages)
        runs = []
        for page in ordered_pages:
            if runs and page - runs[-1][-1] <= 64:
                runs[-1].append(page)
            else:
                runs.append([page])
        declarations.append('void anyps3_page_missing(PpuState& state);')
        declarations.append('void anyps3_dispatch_page(PpuState& state);')
        dispatch = ['[[gnu::always_inline]] inline void ppu_dispatch(PpuState& state){', '    const std::uint32_t page=state.pc>>12;']
        tables = []
        for index, run in enumerate(runs):
            low, size = run[0], run[-1] - run[0] + 1
            declarations.append(f'extern void (*const anyps3_pages_{index}[{size}])(PpuState&);')
            dispatch.append(f'    if(page-0x{low:x}u<0x{size:x}u){{[[gnu::musttail]] return anyps3_pages_{index}[page-0x{low:x}u](state);}}')
            entries = ','.join(f'page_{p:x}' if p in pages else 'anyps3_page_missing' for p in range(low, low + size))
            tables.append(f'void (*const anyps3_pages_{index}[{size}])(PpuState&)={{{entries}}};')
        dispatch.extend(['    [[gnu::musttail]] return anyps3_page_missing(state);', '}'])
        declarations.extend(dispatch)
        operations.extend(tables)
        operations.append('[[gnu::noinline]] void anyps3_dispatch_page(PpuState& state){[[gnu::musttail]] return ppu_dispatch(state);}')
        operations.append('void anyps3_page_missing(PpuState& state){anyps3_ppu_step(state,state.pc);throw std::runtime_error("target has no statically translated block");}')
        header, lines = lines, []
        # Exclusive main-thread wall time, sampled only at engine/wait boundaries.
        lines.extend(["[[maybe_unused]] static void anyps3_perf_report(const PpuState& state) {",
                      "    if(!state.execution.enabled)return;",
                      "    auto& last=state.execution.last;",
                      "    const auto now=std::chrono::steady_clock::now();",
                      "    if(last && now-last->at<std::chrono::seconds(2))return;",
                      "    state.execution.transition(state.execution.phase,now);",
                      "    std::uint64_t spu=0;for(const auto& group:state.spu.groups)for(const auto& thread:group.threads)spu+=state.process.spu_worker.enabled?thread.published_steps.value.load(std::memory_order_relaxed):thread.steps;",
                      "    const auto& rsx_worker=state.process.rsx_worker;",
                      "    const ExecutionTiming::Sample current{now,state.steps,spu,rsx_worker.enabled?rsx_worker.flips.load(std::memory_order_acquire):state.rsx.flips,rsx_worker.enabled?rsx_worker.draws.load(std::memory_order_acquire):state.rsx.draw.draws,state.execution.snapshot(now)};",
                      "    if(last){const double s=std::chrono::duration<double>(now-last->at).count();",
                      '        std::fprintf(stderr,"PERF interval_s=%.3f flips_per_s=%.2f ppu_steps_per_s=%.0f spu_steps_per_s=%.0f draws_per_s=%.1f flips=%" PRIu64 " ppu_steps=%" PRIu64 "\\n",',
                      "            s,(current.flips-last->flips)/s,(current.ppu-last->ppu)/s,(current.spu-last->spu)/s,(current.draws-last->draws)/s,current.flips,current.ppu);",
                      "        const auto delta=[&](ExecutionPhase phase){const auto i=static_cast<unsigned>(phase);return current.ns[i]-last->ns[i];};",
                      '        std::fprintf(stderr,"PERF WALL ppu_ns=%" PRIu64 " spu_ns=%" PRIu64 " rsx_ns=%" PRIu64 " audio_ns=%" PRIu64 " idle_ns=%" PRIu64 " flip_wait_ns=%" PRIu64 " gpu_wait_ns=%" PRIu64 " coordinator_ns=%" PRIu64 "\\n",',
                      "            delta(ExecutionPhase::Ppu),delta(ExecutionPhase::Spu),delta(ExecutionPhase::Rsx),delta(ExecutionPhase::Audio),delta(ExecutionPhase::Idle),delta(ExecutionPhase::FlipWait),delta(ExecutionPhase::GpuWait),delta(ExecutionPhase::Coordinator));}",
                      '    if(state.process.spu_worker.enabled){const auto metrics=state.process.spu_worker.metrics();std::fprintf(stderr,"PERF SPU_WORKER wall_ns_total=%" PRIu64 " quantums=%" PRIu64 "\\n",metrics.first,metrics.second);}',
                      '    if(state.process.spu_worker.enabled){state.process.spu_worker.perf_report(now);state.execution.clock.report("execution");std::fprintf(stderr,"PERF CLOCK_ELIDED execution_total=%llu\\n",(unsigned long long)state.execution.elided_clock_reads);}' if self.process_entry else "",
                      "    spu_dynamic_perf_report(state);" if self.process_entry else "",
                      "    rsx_worker_perf(state);" if self.process_entry else "",
                      '    if(state.memory.inline_enabled.load(std::memory_order_relaxed)){const auto& c=state.memory.inline_counters;std::fprintf(stderr,"PERF PPU_INLINE enabled=%d resumes=%llu blocked_resumes=%llu slow_loads=%llu slow_stores=%llu medium_loads=%llu medium_stores=%llu claim_clears=%llu flat_maps=%llu\\n",int(state.memory.inline_enabled.load(std::memory_order_relaxed)),(unsigned long long)c.resumes,(unsigned long long)c.blocked_resumes,(unsigned long long)c.slow_loads,(unsigned long long)c.slow_stores,(unsigned long long)c.medium_loads,(unsigned long long)c.medium_stores,(unsigned long long)c.claim_clears,(unsigned long long)state.memory.inline_flat.remaps);}' if self.process_entry else "",
                      "    hle_perf_report(state);" if self.process_entry else "",
                      "    for(auto& item:state.execution.ppu_metrics){auto& m=item.second;",
                      '        std::fprintf(stderr,"PERF PPU_THREAD id=%u instructions=%" PRIu64 " ppu_ns=%" PRIu64 " instructions_total=%" PRIu64 " ppu_ns_total=%" PRIu64 "\\n",item.first,m.instructions-m.reported_instructions,m.ns-m.reported_ns,m.instructions,m.ns);',
                      "        m.reported_instructions=m.instructions;m.reported_ns=m.ns;}",
                      "    last=current;",
                      # ANYPS3_DIAG_WATCH=0xADDR[,0xADDR...]: diagnostic big-endian guest words appended as PERF WATCH.
                      '    static const std::vector<std::uint64_t> watch=[]{std::vector<std::uint64_t> out;const char* v=std::getenv("ANYPS3_DIAG_WATCH");',
                      "        for(const char* p=v;p && *p;){char* end=nullptr;out.push_back(std::strtoull(p,&end,0));if(end==p)throw std::runtime_error(\"invalid ANYPS3_DIAG_WATCH\");p=*end==','?end+1:end;}return out;}();",
                      '    if(!watch.empty()){std::fprintf(stderr,"PERF WATCH");for(const auto a:watch)std::fprintf(stderr," %#" PRIx64 "=%#" PRIx64,a,const_cast<PpuState&>(state).memory.load(a,4));std::fprintf(stderr,"\\n");}',
                      "}"])
        if self.process_entry:
            # PPUFAST: engine polls cost clock reads and owner pumps. SPU keeps
            # the 256-step cadence (cheap, drives lane rotation); RSX worker
            # requests run every 1024 steps or at once when the renderer or a
            # coherence request waits for the owner; audio (5.33 ms periods,
            # catch-up loop) every 4096 steps.
            lines.extend(["[[gnu::always_inline]] static inline bool anyps3_rsx_poll_due(PpuState& state,std::uint64_t before){",
                          "    const auto& w=state.process.rsx_worker;",
                          "    return !w.enabled || state.steps/1024!=before/1024 || (w.queue && w.queue->coordinator_pending.load(std::memory_order_acquire)) ||",
                          "        state.memory.parallel->coherence_pending.load(std::memory_order_acquire);",
                          "}"])
        lines.extend(["void anyps3_entry(PpuState& state, bool trace) {",
                      "    state.ppu_trace=trace;",
                      "" if trace else '    if(trace)throw std::runtime_error("PPU trace requires a trace build (Program.cpp(trace=True))");',
                      "    HostAccessContext origin(HostAccessOrigin::Engine::Ppu,state.thread_id,state.pc,&state.steps);SpuWorkerSession worker_session(state);" if self.process_entry else "",
                      "    ExecutionScope execution_scope(state.execution,ExecutionPhase::Ppu);",
                      "    struct InlineSectionExit {GuestMemory& memory;~InlineSectionExit(){memory.inline_suspend();}} inline_section_exit{state.memory};",
                      # Lazy FR/FI/FPRF never escape translated code: the host sees an exact FPSCR on return and STOP.
                      "    struct FpLazyExit {PpuState& state;~FpLazyExit(){try{fp_materialize(state);}catch(...){}}} fp_lazy_exit{state};",
                      "    auto& r = state.r; (void)r; for (;;) {",
                      "    state.ppu_slice_end=(state.steps/256+1)*256;" if self.process_entry else "    state.ppu_slice_end=UINT64_MAX;",
                      f'    if (state.steps >= UINT64_C({max_steps})) throw std::runtime_error("execution step limit reached");',
                      "    const auto before=state.steps; state.memory.inline_enter_dispatch(); try { ExecutionPpuPage ppu_perf_page(state.execution,state.thread_id,state.steps); { anyps3_dispatch_page(state);" if self.process_entry else "    state.memory.inline_enter_dispatch(); { anyps3_dispatch_page(state);"])
        ending = "    } } catch (const PpuBlocked&) { state.memory.inline_suspend(); ppu_schedule(state,true); continue; } if(state.steps/256!=before/256 || state.ppu_block_pending || state.reschedule || state.returned)state.memory.inline_suspend(); if(state.ppu_block_pending){state.ppu_block_pending=false;ppu_schedule(state,true);continue;} if(state.steps/256!=before/256){spu_run(state,1024);if(anyps3_rsx_poll_due(state,before))rsx_run(state,1024);if(state.steps/4096!=before/4096)audio_run(state);if(state.steps/65536!=before/65536)anyps3_perf_report(state);if(anyps3_dump_requested.load(std::memory_order_relaxed) && anyps3_dump_requested.exchange(false))anyps3_dump_threads(state);} if(state.reschedule || (!state.module_loading && state.ppu_threads.size()>1 && state.steps/4096!=before/4096)) ppu_schedule(state); if(state.returned) { if(state.thread_id!=1) throw std::runtime_error(\"PPU thread returned without thread-exit service\"); worker_session.finish();return; } } }" if self.process_entry else "    } if (state.returned) return; } }"
        lines.extend([ending, "int main(int argc, char** argv) {",
                      '    if (argc < 2 || argc > 3) { std::fprintf(stderr, "Usage: program SOURCE.elf [--trace]\\n"); return 2; }',
                      '    const bool trace = argc == 3 && std::string(argv[2]) == "--trace";',
                      '    if (argc == 3 && !trace) { std::fprintf(stderr, "Unknown option\\n"); return 2; }',
                      "    PpuState state; state.rsx.trace=trace; try { anyps3_sample::Thread sample_profile(anyps3_sample::Role::Ppu,&state.pc,&state.thread_id,&state.steps); load_image(state.memory, argv[1]);",
                      '    state.fs.mount(std::getenv("ANYPS3_DISC_ROOT"));' if self.process_entry else "",
                      '    state.fs.configure_cache(std::getenv("ANYPS3_CACHE_ROOT"));' if self.process_entry else "",
                      '    state.fs.configure_hdd(std::getenv("ANYPS3_HDD_ROOT"));' if self.process_entry else "",
                      f"    state.r[1] = 0x{STACK_POINTER:x}; state.r[2] = 0x{self.toc:x}; state.pc = 0x{self.entry:x};",
                      process_setup(self.image) if self.process_entry else "",
                      resident_startup(self.image,self.native_modules),
                      "    anyps3_entry(state, trace);",
                      "    anyps3_trace_dump();",
                      '    std::printf("returned r3=%" PRIu64 " steps=%" PRIu64 "\\n", state.r[3], state.steps); return 0;',
                      "    } catch (const std::exception& error) {",
                      "        anyps3_trace_dump();",
                      '        std::fprintf(stderr, "STOP pc=0x%08" PRIx32 " lr=0x%08" PRIx64 " steps=%" PRIu64 ": %s\\n", state.pc, state.lr, state.steps, error.what());',
                      '        for (unsigned i = 0; i < 32; ++i) std::fprintf(stderr, "r%u=0x%016" PRIx64 "%s", i, state.r[i], i % 4 == 3 ? "\\n" : " ");',
                      '        std::fprintf(stderr,"CR=%08" PRIx32 " FPSCR=%08" PRIx32 " CTR=%016" PRIx64 " XER=%016" PRIx64 "\\n",state.cr,state.fpscr,state.ctr,state.xer);',
                      '        for(unsigned i=0;i<32;++i)std::fprintf(stderr,"fpr%u=%016" PRIx64 " vr%u=%08x %08x %08x %08x\\n",i,state.fpr[i],i,vmx_word(state.vr[i],0),vmx_word(state.vr[i],1),vmx_word(state.vr[i],2),vmx_word(state.vr[i],3));',
                      '        std::fprintf(stderr,"loaded_module_ids:"); for (auto id:state.loaded_modules) std::fprintf(stderr," %" PRIu64,id); std::fprintf(stderr," initializing=%s\\n",state.module_loading?"yes":"no");',
                      '        for(const auto& group:state.spu.groups)for(const auto& thread:group.threads)std::fprintf(stderr,"SPU status id=%x pc=%05x completed=%" PRIu64 " waiting=%u\\n",thread.id,thread.pc,thread.steps,unsigned(thread.waiting_event));',
                      # Diagnostic only: ANYPS3_SPU_LS_DUMP_DIR writes every SPU thread's 256 KiB local storage at STOP.
                      '        if(const char* dir=std::getenv("ANYPS3_SPU_LS_DUMP_DIR"))for(const auto& group:state.spu.groups)for(std::size_t i=0;i<group.threads.size();++i){'
                      'if(group.context.size()<(i+1)*0x40000)continue;char path[1024];std::snprintf(path,sizeof(path),"%s/spu-%x-pc%05x.ls",dir,group.threads[i].id,group.threads[i].pc);'
                      'if(FILE* f=std::fopen(path,"wb")){std::fwrite(group.context.data()+i*0x40000,1,0x40000,f);std::fclose(f);std::fprintf(stderr,"SPU LS dump %s\\n",path);}}',
                      '        if(state.rsx.dma)std::fprintf(stderr,"RSX status packets=%" PRIu64 " registers=%zu get=%08" PRIx64 " put=%08" PRIx64 " reference=%08" PRIx64 "\\n",state.rsx.commands,state.rsx.registers.size(),state.memory.load(state.rsx.dma+0x44,4),state.memory.load(state.rsx.dma+0x40,4),state.memory.load(state.rsx.dma+0x48,4));',
                      '        std::fprintf(stderr,"RSX rendered clears=%" PRIu64 " presented_frames=%" PRIu64 "\\n",state.rsx.rendered_clears,state.present.presented_frames);',
                      '        std::fprintf(stderr,"RSX captured_frames=%" PRIu64 " flips=%" PRIu64 " draws=%" PRIu64 " vblank_ticks=%" PRIu64 "\\n",state.rsx.captured_frames,state.rsx.flips,state.rsx.draw.draws,state.rsx.vblank_ticks);',
                      '        if(state.rsx.diag_skipped)std::fprintf(stderr,"RSX DIAGNOSTIC RUN: %" PRIu64 " unsupported RSX operations were skipped (ANYPS3_RSX_DIAG_SKIP=1); not evidence\\n",state.rsx.diag_skipped);',
                      "        return 1;", "    }", "}", ""])
        return dict(header=header, declarations=declarations, spu_helpers=spu_helpers,
                    spu=spu_functions, runtime=runtime, operations=operations, ppu=ppu,
                    entry="\n".join(lines))


def compile_program(image: ElfImage, *, entry: int | None = None, entry_kind: str = "descriptor",
                    diagnostic: bool = False, max_instructions: int = 65536,
                    process_entry: bool = False, extra_entries: tuple[int, ...] = (),
                    additional_imports: tuple[dict, ...] = (),
                    native_modules: tuple[dict, ...] = (),
                    code_tocs: tuple[tuple[int, int, int], ...] = ()) -> Program:
    if not 1 <= max_instructions <= 4_000_000:
        raise AnyPS3Error("Instruction limit must be between 1 and 4000000.")
    code, toc = image.resolve_entry(address=entry, kind=entry_kind)
    if process_entry and (entry is not None or entry_kind != "descriptor"):
        raise AnyPS3Error("Process entry requires the original ELF entry descriptor.")
    imported, implementations = {}, {}
    if native_modules and not process_entry:
        raise AnyPS3Error('Native module lifecycle requires process-entry mode.')
    validate_catalog(image,native_modules)
    bindings = exported_bindings(native_modules)
    redirects = {}
    for library in (*imports(image), *additional_imports):
        for function in library["functions"]:
            address = function["stub"]
            label = f"Unimplemented import {library['library']} NID={function['nid']}"
            if address in imported and imported[address] != label:
                raise AnyPS3Error("Ambiguous imported function address.")
            imported[address] = label
            key = (library['library'],function['nid'])
            if key in bindings:
                module, function = bindings[key]
                valid = module_available(module)
                redirects[address] = Operation(address,0,'native firmware export',
                    f'if (!({valid})) {{ throw std::runtime_error("native export called before module load"); }} '
                    f'state.memory.store(r[1]+40,8,r[2]); r[2]=0x{function["toc"]:x}; state.pc=0x{function["code"]:x};',
                    (function['code'],))
            if process_entry and (library['library'], function['nid']) in IMPORTS:
                implementations[address] = IMPORTS[(library['library'], function['nid'])]
    for address in extra_entries:
        image.resolve_entry(address=address, kind="code")
    starts = [image.resolve_entry(address=m['start'])[0] for m in native_modules if m.get('start')]
    pending, found, traps = deque([code, *extra_entries, *starts]), {}, {}
    tables = []
    while pending:
        pc = pending.popleft()
        if pc in found or pc in traps:
            continue
        try:
            if len(found) >= max_instructions:
                raise AnyPS3Error("Static instruction budget exhausted; block not translated")
            image.read_virtual(pc, 4, executable=True)
            if pc in imported:
                if pc in implementations:
                    name, body = implementations[pc]
                    found[pc] = Operation(pc, 0, name + " (single main thread)",
                        "state.memory.store(r[1] + 40, 8, r[2]); " +
                        f'anyps3_trace_import("import {name} pc=0x{pc:08x}\\n"); ' + body +
                        (" if(state.ppu_block_pending)return;" if name in ("sys_lwmutex_lock", "sys_lwcond_wait") else "") +
                        " state.pc = checked_target(state.lr & ~UINT64_C(3));", ())
                    continue
                if native_modules and imported[pc].endswith('cellSysmodule NID=0x32267a31'):
                    found[pc] = Operation(pc,0,'native module load',
                        'state.memory.store(r[1]+40,8,r[2]); native_module_load(state);',())
                    continue
                if pc in redirects:
                    found[pc] = redirects[pc]
                    pending.extend(redirects[pc].successors)
                    continue
                raise AnyPS3Error(imported[pc])
            word = int.from_bytes(image.read_virtual(pc, 4, executable=True), "big")
            if process_entry and word == 0x44000002:
                inst = Operation(pc, word, "sc", f"syscall(state); if(state.ppu_block_pending)return; state.pc = 0x{pc + 4:x};", (pc + 4,))
            else:
                inst = lift(word, pc)
        except AnyPS3Error as exc:
            if not diagnostic:
                raise AnyPS3Error(f"Program translation stopped at 0x{pc:08x}: {exc}") from exc
            traps[pc] = str(exc)
            continue
        found[pc] = inst
        pending.extend(inst.successors)
        if process_entry and word == BCTR:
            # Firmware module code addresses its tables through the module TOC.
            pc_toc = next((t for lo, hi, t in code_tocs if lo <= pc < hi), toc)
            table = detect_jump_table(image, pc, pc_toc)
            if table:
                tables.append(table)
                pending.extend(table['targets'])
    return Program(image, code, toc, tuple(found.values()), traps, diagnostic, process_entry, extra_entries, native_modules,
                   jump_tables=tuple(tables))
