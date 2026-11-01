# Handoff to the next instance

## State at the end of the cloud rounds — 2026-11-01

- **Goal not reached.** Nothing has been measured in the game since HIRES2 (2026-10-27, PC). Read [CURRENT.md](CURRENT.md):
  the last PC numbers, the cloud changes since, the switch table and **one ordered PC measurement plan**. The next step
  belongs on the PC. Another cloud round before that measurement would optimise blind: the H8/H9 targets were already
  missed, and only the PC shows which thread is still critical.
- **Branches:**
  - `main` = 95756e2 (HIRES2 merged, PR #14).
  - The work branch `ccr-b1a3987e-aypb5o` = main + HIRES3–HIRES9 + the documentation cleanup of 2026-11-01. It is pushed; no PR
    is open.
  - The remote branch `ccr-8139488c-79xg7b` (HIRES3) is fully contained in the work branch; the user deletes it, since the
    cloud session may not.
- **Live processes:** none from the cloud rounds. Agents, benches and the suite have ended, and their worktrees and branches
  are removed. The PC's state (builds, queued jobs) is not visible from the cloud.
- **Documentation:** cleaned up on 2026-11-01. Superseded material is in [../archive/](../archive/README.md), which also gives the
  pre-cleanup commit.

## If another cloud round is started (what worked, what the container can do)

- **Container:** 4 vCPU, lavapipe only, GCC 13, valgrind; no game data, no `local/` tree, no NVIDIA. Deliver exact or gated
  source changes, each with a native test against an independent oracle plus mutation checks. The PC measures.
- **Benches (in `bench/`):**
  - `rsx_fifo_bench.py`: the real RSX worker on synthetic FIFO streams. Use mode `tracked=1`; it is game-shaped.
  - `rsx_draw_bench.py`, `rsx_fifo_actions_bench.py`.
  - `spu7_job_bench.py`: a synthetic SPU job through the production lifter and runtime. The metric is non-FP Ir = `spu_step`
    inclusive minus `spu_single_vector`, since valgrind has no AVX-512, plus native AVX-512 ns/iteration over three alternating
    rounds; also compare the output digest.
  - `ppu_bench.py`: run with `BENCH_CALLGRIND=20`, `-O2 -march=x86-64-v3 -ffp-contract=off -DBENCH_MREMAP_SHIM`; it reports host
    Ir per guest instruction per kernel and the page text.
  - Always run base vs merge alternating on snapshots of both trees.
- **Agents:**
  - Each agent works in its own worktree and branch from the coordinator's tip and writes a result file.
  - The coordinator reviews every exactness argument against the code, merges, and repeats the integration bench.
  - At most two agents in parallel on 4 vCPU.
- **Full suite:**
  - Run every `tests/test_*.py` module in its own process, 3 in parallel, `nice -n 5`, `ANYPS3_REQUIRE_VULKAN=1`, on a
    `git archive` snapshot. It takes ~4 h.
  - Compare the FAIL/ERROR lines mechanically with the previous suite.
  - Re-run alone the modules that fail only under load (`test_rsx_perf`, `test_spu_pool_poll`, `test_spuperf2_simd`, compile limits).
- **Modules that cannot pass in the container** (they fail on every base the same way):
  - `local/` tools missing: `test_sample_profile`, `test_sample_profile_integration`, `test_spudyn_profile`,
    `test_spuperf2_capture`;
  - SDL offscreen without Vulkan: `test_present_window`;
  - GCC 13 `-Werror`: `test_ppu_fp_lazy`, `test_ppuperf3`, `test_savedata_vsh`, `test_rsx_residency`;
  - lavapipe-sensitive cache asserts: `test_rsx_fp_cache`, `test_rsx_sampler_identity`, `test_rsx_resident_sampler`,
    `test_rsx_perf_decode`, `test_rsx_pipeline_cache`;
  - compile limits too short for the container (vary with load): `test_program`, `test_services`, `test_native_audio*`,
    `test_spufast_blocks`, `test_vulkan`.
- **HIRES9 suite result:** running at the time of the cleanup commit (117 of 204 modules, failures identical to HIRES7); the final result is in the [HIRES9 record](experiments/2026-11-01-cloud-hires9.md#regression-tests).

## Template for an experiment or handoff

```text
Date and author/model:
Goal and completion criterion:
Source state: commit + uncommitted files/hashes
Input: profile, game identification, relevant configuration
Verified fact: what exactly was observed
Sources: file:line @ commit / PDF title-version page N + SHA256
Runtime: exact command, log, exit/timeout, artifacts and counters
Changes: what is done and where
Hypothesis: reason, alternative, distinguishing test
Refuted: attempt and reason
Open: what we do not know yet
Next step: concrete first action
Limitations: what the test does not cover
```

## Check before takeover

- The paths actually exist; the reference checkout has a recorded revision.
- A fact comes from a specific source or a runtime artifact.
- The result matches the goal's criterion (1440p, 60 FPS acceptance, gamepad); a diagnostic presentation is not mistaken for
  guest rendering.
- Failed attempts and hypotheses are visibly separated from confirmed behavior.
- The next step addresses the real last blocker, not a long-removed error.
