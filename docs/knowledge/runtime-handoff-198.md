> **Superseded** by the handoff [runtime-handoff-237.md](runtime-handoff-237.md); the stop `0x1b8` was resolved by linking the PRX import refs. Kept as a historical record; the heavy artifacts of run 198 remain.

# Runtime handoff after build 198

Date: 2026-10-14. Original goal: the original Ratchet & Clank: A Crack in Time menu and its gamepad navigation. **The goal is not complete.** The user allowed finishing after a major advance and the completion of side documentation tasks, so that another AI agent can take over the work.

## Verified advance

- The SPU no longer stops at the original fixed limit of 10 million instructions. `ANYPS3_SPU_MAX_STEPS` allows an explicit positive uint64 budget; the default limit remains 10 million. A native test checks exceeding the old boundary and stopping before the next instruction past the new limit.
- PPU arithmetic preserves the NaN payload, sign and PowerPC operand priority, quiets signaling NaNs and updates FPSCR. Subtraction does not change the NaN sign. The exact original FSUBS at `0x3acfc0` now runs.
- Subnormal operands and results in binary32/binary64 use integer arithmetic and a single final rounding. An exact subnormal does not report UX, a tiny inexact reports UX/XX. Rational tests verify boundary values, midpoints, signs and the transition to the normal range. The original FSUBS `0 - 2^-142` at `0x3af508` now runs.
- Another original callback via OPD `0xd0af48` (`0x4eb05c`, TOC `0xd34a88`), its ten-entry jump table and the fourteen methods of the vtable `0xc38280` are verified and translated in advance. The original game bytes remain the same.
- The sysutil query for the number of audio devices `get 0x4c05`, length 8, returns the real configuration: no audio backend yet. `set 0x4c09`, length 28, returns an explicit NOT_IMPLEMENTED error, not a successful configuration. The original game accepted this error and continued.
- The original `TrophyInitThread` (thread 10, OPD `0xd08f60`, entry `0x4c59b8`) now actually executes instructions. Previously it was only created.

## Last authoritative run

Directory: `local/ratchet/native-bundle-198/`.

- `verification.json`: check of the original game PT_LOAD bytes, SHA-256 of sources/artifacts, the stop and the resulting frames.
- `build.json`, `profile.json`, `program.elf`, `program.cpp`, `program`: the exact reproducible inputs and AOT outputs.
- `stderr.log`, `stdout.log`, `frames/`: the real output of the run.
- `local/ratchet/run-198-summary.log`: a brief machine summary.

The PPU completed **118,208,831 steps**. SPU `0x100` completed **30,465,692 instructions**; the other contexts execute the original job manager and wait for events. The RSX completed 1629 FIFO packets and 4 clears. The four captured frames of 1280 × 720 are still completely black; presentation to the window is zero. **No game geometry, menu or gamepad navigation is verified by this run.**

The code `anyps3/*.py` and `profiles/ratchet-acit.json` was compared after the run with the snapshot of build 198: the hashes of all sources and the exact bytes of the profile match. The original game ELF has SHA-256 `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`. The build profile has SHA-256 `abba22a03a9377653b820a1a9f7833fcf335f742e374a74cd97eff836502d016`.

PPU counts between runs are not a deterministic time meter: scheduling and vblank use real monotonic time. A newer run may have a smaller step count when another thread is scheduled earlier. Judge the branch reached and the evidence, not just a higher number.

## Current stop and first next actions

Stop: PC **`0x2011056c`**, LR **`0x20221a98`**, a 4-byte read from the unmapped guest address **`0x1b8`**. The real `TrophyInitThread` is active, inside the original firmware.

**Verified trail:**

1. The `libsysutil_np_trophy` code at `0x20221a68` loads `r0` from its TOC slot (`r2 - 0x7eec`) and at `0x20221a78` stores the value into the command context `+0x6c`.
2. The module TOC according to the relocation report is `0x20238360`; the slot `0x20230474` in the original relocated ELF contains `0x1b8`. No entry was found for this slot in the original PRX relocation table. The neighboring slots have relocations.
3. The `libsysutil` code at `0x20110520` loads the context `+0x6c` into `r10`, converts it to `r9` and at `0x2011056c` dereferences it as an OPD before an indirect call.

**Not yet proven:** why the constant `0x1b8` got into the field that the callee uses as an OPD. The candidates are the TOC/ABI during a cross-module call, unfinished initialization of the context, or incorrect argument passing. This is not yet a proven bug of the PRX relocator.

**First next step:** trace the real `r2` and the arguments before `0x20221a68` and at the binding to `libsysutil`, and compare the original import/export ABI and the initialization of the command context. Do not add the module base to `0x1b8` and do not map the zero page as a fix without this evidence. Do not remove the trophy thread from the scheduler.

For this purpose a separate read-only `diagnostic.cpp` is being created in directory 198, which only logs instructions and selected registers of thread 10. The regular `program.cpp` remains unchanged. The status and result of the diagnostics are added in the last section of this report; the mere existence of the source does not prove a completed process.

## Reproduction

From the project root and into a directory not used so far:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json \
  --max-steps 500000000 -o local/ratchet/native-bundle-199
c++ -std=c++17 -Og -Wall -Wextra -Werror -DANYPS3_VULKAN \
  local/ratchet/native-bundle-199/program.cpp -lvulkan \
  -o local/ratchet/native-bundle-199/program
PYTHONPATH=. python local/ratchet/run_native.py native-bundle-199 500000000
```

The helper sets the real disc/cache/HDD root, ASCII case-fold only for the ISO9660 extract, Vulkan Lavapipe, the validation layer, a new frame directory and the SPU budget. It checks the identity of the original game segments and writes a verification report; the menu flag is explicitly false, automatic interpretation of menu content is not implemented. Its successful exit may mean a properly documented guest stop: read `returncode` in the report.

## Tests and limitations

`ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q` passed: **156 tests**, 97.714 s. Log: `local/ratchet/tests-native-198.log`, its hash is in the verification report. Vulkan was mandatory. The separate NaN/subnormal tests and the sysutil wire audio error tests are also saved in `local/ratchet/tests-floating-195.log` and `tests-sysutil-197.log`.

Infinities beyond the supported NaN propagation, overflow, alternative rounding modes and enabled FP traps continue to fail explicitly. The audio backend is missing; unknown services are not simulated by a successful return. SDL2 input is verified with a virtual device, not in the original menu. Geometry/shaders and window presentation are still missing.

The Git metadata directory in the current environment is empty/restricted; `git status` reported that it is not a repository. No commit and no new Git repository was created. The available source hashes and snapshots replace the missing commit reference for this handoff.


## Completed read-only diagnostics and the state at handoff

`diagnostic.cpp` and `diagnostic` were built and run; the run ended
with a nonzero return code 1 at the **same** read of `0x1b8` at PC
`0x2011056c`, at 118,209,228 PPU steps. Outputs:
`diagnostic-stderr.log`, `diagnostic-stdout.log`, `diagnostic-frames/`
and `diagnostic-verification.json` in the build 198 directory. The last file
contains the hashes of the separate diagnostics and the logs.

**New direct evidence:** the trace before `0x20221a68` shows the real `r2=0x20238360`;
after this instruction `r0=0x1b8` and before `0x20221a78` the same value is
passed into the context. An incorrect TOC register directly at the point of this load
is therefore refuted. The cause of the faulty allocator OPD is still not proven:
check the original initialization of this TOC slot and of the command context,
the module lifecycle and its ABI. The bug must not be masked.

At handoff all created build/run/test/diagnostic processes are
terminal and the documentation subagent GPT-6.1 Sol has completed its task.
No run is waiting for further polling. The documentation has five verified original
PDFs and five page-wise MD conversions (2755 physical pages), valid local
links and a SHA-256 manifest. The root AGENTS.md is the main workflow.
The work is paused for handoff at the user's wish; the original goal remains
incomplete.
