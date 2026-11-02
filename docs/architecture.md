# Architecture of AnyPS3

AnyPS3 turns a PS3 title into a native Linux x86-64 program. PPU and SPU code is translated ahead of time to C++. It is
compiled together with the project's own LV2 kernel and system-library services and a Vulkan implementation of the RSX.
No other emulator runs at run time; reference projects (RPCS3, PSL1GHT, envytools) are used only to research interfaces.
The first and only target is Ratchet & Clank: A Crack in Time (BCES00511 01.00), which reaches its original menu and first
level. The current state and measurements are in [knowledge/CURRENT.md](knowledge/CURRENT.md).

Overview as of 2026-11-01, checked against the code. A module's docstring is the authority for its details.

## Build pipeline

1. **Inputs (local, never committed):** the original EBOOT (decrypted analysis ELF), the firmware PRX modules, the app key and
   the profile `profiles/ratchet-acit.json` (target identity, static targets with provenance).
2. **`anyps3.build_game`:** analyses the ELF and PRXs (`elf.py`, `prx.py`, `modules.py`), resolves every OPD function, its jump
   tables (`jump_tables.py`) and the SPU images/overlays (`spu_scan.py`). It writes a bundle: `units.json`, `anyps3.hpp`,
   `program.cpp` (runtime, LV2 services, `main`), ~96 `ppu-*.cpp` and ~96 `spu-*.cpp` translation units.
3. **`anyps3.native_build --vulkan`:** compiles the units in parallel (~4 min on the PC) and links Vulkan, SDL2 and
   glslang/SPIRV.
   - On the PC, `local/ratchet/tools/virtual_pad.py build` wraps this and adds the virtual pad.
   - Every build gets a new number, a source snapshot and a profile snapshot.
4. **Run:** the native program mounts the real disc image and HDD directories and runs the original game bytes, unpatched.
   Either `local/ratchet/run_native.py` on the PC or the manager launches it.

## Execution model

- **PPU:**
  - All code is translated (`ppu_emit.py`, `program.py`; the lifter in `ppu.py`, `flow.py`, `floating.py`, `vector.py`). There
    is no interpreter fallback: an untranslated target or instruction stops the run.
  - Guest threads are cooperative contexts on one host thread (`threads.py`), which approximates the Cell's two hardware
    threads. This is the "main guest thread" of the performance records.
- **SPU:**
  - Code runs as validated native superblocks (`spu_block.py`, `spu_simd.py`) from the shared lifter (`spu.py`). Changed code
    and late or relocated job code run in the exact interpreter from the same lifter (`spu_interp.py`, `spu_reloc.py`).
  - With `ANYPS3_SPU_THREADS=1` SPU threads run on host pool lanes (`par_worker.py`); the GUI always sets it.
- **LV2 and system libraries:** these are native services in the same process (`services.py`, `kernel_sync.py`,
  `lightweight_sync.py`, `lv2_*_ext.py`, `native_*.py`: filesystem, savedata, pad, audio, trophies, VSH UI). Firmware code
  that the game calls stays original and translated where it is reachable. A known service may return a real error; an
  unknown one stops the run.
- **RSX:**
  - With `ANYPS3_RSX_WORKER=1` (always set by the GUI) a FIFO owner thread decodes the guest's command buffer
    (`rsx.py`, `rsx_game_worker.py`) and prepares draws (`rsx_graphics*.py`). Vulkan records them (`rsx_vk_graphics.py`,
    optionally on the draw-recorder thread, `ANYPS3_RSX_RECORDER`).
  - Blits, texture decoding, surface/alias tracking and visual companions at render scale 2 have their own modules
    (`rsx_blit.py`, `rsx_vk_texture_decode.py`, `rsx_graphics_alias.py`, `rsx_graphics_visual_history.py`).
  - Presentation goes through SDL2 + a Vulkan swapchain (`rsx_vk_present.py`); vblank is emulated (`rsx_vblank.py`). Details
    are in [knowledge/RSX.md](knowledge/RSX.md).

## Guest memory

- **Storage and views:** one physical store with a protected raw view and an exact-access view of the same bytes
  (`guest_backing.py`). Every service and the RSX reach guest memory through admitted `GuestMemory` operations
  (`runtime.py`), with page leases and ownership shared by the CPU and the RSX (`par_memory.py`, `par_pages.py`).
- **Atomics:** concurrent access is atomic per guest word. PPU/SPU reservations (lwarx/stwcx., getllar/putllc) use 128-byte
  line versions under stripe locks.
- **PPU inline path:** translated PPU code accesses a flat view directly through one pointer, with a per-page blocker byte
  (`ppu_memory.py`); any conflict takes the admitted path.
- **GPU-resident data:** CPU writes to GPU-resident data are caught by host observers and CPU-dirty watches (`runtime.py`).
  Host accesses keep the guest operation's provenance through owner RPC (`host_access.py`, `host_fault.py`).

## Manager and GUI

- **`anyps3_manager`:** the CLI game library — import ISO, key setup, build jobs, launch, saves and pads.
- **`anyps3_gui`:** a PySide6 wrapper around the manager.
- Usage: [manager/README.md](manager/README.md); controller support: [manager/DUALSENSE.md](manager/DUALSENSE.md).

## Correctness rules

- **Address translation:** virtual addresses are converted through `PT_LOAD`, never used as file offsets. Code is read only
  from a mapped, file-backed executable segment.
- **No silent fallbacks:** an unsupported instruction, syscall, FIFO method or protocol stops the run explicitly
  (`std::runtime_error`). Unknown behaviour is never replaced by an empty body or a success. A successful return confirms
  only an operation that was performed.
- **Static targets:** a new indirect target, overlay or jump table needs verified original bytes, TOC, range and image
  identity; the provenance belongs in the profile or a knowledge record.
- **Inputs:** original game bytes are never patched, and outputs never overwrite inputs or earlier builds. A hash match
  confirms the identity of a binary, not its playability.
- **Exactness:** performance changes must be exact or gated by a switch with the old behaviour behind `0`, and each needs a
  native regression test with an independent expectation (see the root `AGENTS.md`).

The architecture page of 2026-10-14 (process start, the first lifter subset, the early milestones) is archived as
[archive/architecture-2026-10-14.md](archive/architecture-2026-10-14.md).
