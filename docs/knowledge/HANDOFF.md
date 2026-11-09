# Handoff to the next instance

## State after HIRES9-PC — 2026-11-09 (PC)

- **Goal not reached.** 1440p walk: build 1037 **43.08 / 43.22 FPS** (65–66 % one-vblank); 720p walk 52.31 / 53.21 FPS. Read
  [CURRENT.md](CURRENT.md) (results table, switch table, the new ordered plan) and the
  [HIRES9-PC record](experiments/2026-11-09-hires9-pc.md).
- **Branch:** `claude/hires9-pc` = main `a114612` (PR #15, cloud HIRES3–HIRES9) + test fixes `72fcca2`, `1e45a72`, the pool
  accounting fix `a020c8d`, geometry reuse off by default `e3b53f3`, GUI recorder on `bd93d9e`, and this documentation. Pushed
  for a PR; not merged by the agent.
- **Builds/runs (PC, `local/ratchet`):** 1036 (= `a114612`, slow: reuse thrash), 1037 (= `e3b53f3`, the new baseline);
  runs 1036w1/a1/g1, 1037a1/a2, 1037a1s1/a2s1, 1037r1/r0, 1034h1/h2, 960h1s1/h2s1. Scripts and logs:
  `local/ratchet/work/hires9pc/`. Full suite on `1e45a72`: `work/hires9pc/suite-1e45a72/` (result in the record).
- **Live processes:** none after the suite (check `pgrep -af run_suite`); no game run, no build.
- **First next action:** the frame-chain latency analysis at 1440p (CURRENT, plan item 1).
- **Disk cleanup:** prepared in `local/ratchet/work/cleanup-2026-11-09/` (≈ 108.6 GiB sure, ≈ 26.8 GiB ask, 23.1 GiB ISO);
  **not applied** — waits for the user.
- **Environment note:** PySide6 is not installed on the PC, so the GUI test modules (`tests/gui/*`, `tests/manager/test_gui*`)
  fail at import here; they are not code failures.

## State at the end of the cloud rounds — 2026-11-01 (historical)

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
    `test_rsx_perf_decode`, `test_rsx_pipeline_cache` — **refuted on the PC (HIRES9-PC):** the first three were stale oracles
    since 409f8bb (fixed `1e45a72`), `test_rsx_pipeline_cache` a real pool accounting bug (fixed `a020c8d`),
    `test_rsx_perf_decode` passes on the PC;
  - compile limits too short for the container (vary with load): `test_program`, `test_services`, `test_native_audio*`,
    `test_spufast_blocks`, `test_vulkan`.
- **HIRES9 suite result** (merge e896fed, 204 modules): 188 OK, 16 failing, all 16 identical to HIRES7 and all in the list above;
  8 HIRES7 failures now pass. Details: [HIRES9 record](experiments/2026-11-01-cloud-hires9.md#regression-tests).

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
