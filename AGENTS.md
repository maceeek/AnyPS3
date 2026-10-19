# AnyPS3: workflow for agents

## Project goal and boundaries

The current goal is **to get the original Ratchet & Clank: A Crack in Time
to its menu and make it navigable with a gamepad**. Preserve this goal
also across handoffs between models and runs. An instruction count, a
completed clear, a black flip or a synthetic test are not a reached menu.

The project uses its own static PPU/SPU translation and its own Vulkan
runtime. Reference projects serve for researching interfaces and
algorithms. The original game bytes are not patched. Do not insert a
substitute menu, pre-unpacked assets or another emulator as a route to a
supposed fulfilment of the goal.

## At the start of every run

1. Read this file, `docs/knowledge/INDEX.md`,
   `docs/knowledge/CURRENT.md` and `docs/knowledge/HANDOFF.md`.
   For history use `docs/menu-progress.md`; do not read all the old logs.
2. Check the current files, the profile, the last verified build and its
   `verification.json`. The knowledge base is an index, not evidence of the
   current runtime state. Verify that the links exist and what they
   actually contain.
3. Determine the actual state of running processes and available
   tool/session handles. Observe a live build or run first; do not restart
   it after a mere observation timeout. An old log or status file alone
   does not prove a live process.
4. If Git is available, read the status and diff, and preserve changes made
   by others. An empty or inaccessible `.git` is not permission to
   initialize a new repository.
5. Formulate the next concrete blocker from the last actual stop.
   Prefer removing it over unrelated refactoring.

## Implementation and evidence cycle

1. **Reproduction:** open the latest stderr/stdout, register state, the
   relevant original instructions and the original ELF/PRX/SPU image.
   Identify the actual arguments, addresses, versions and expected result.
2. **Research:** search with `rg` in local references and manuals.
   For bit layouts and tables, check the original PDF; the Markdown
   conversion is a navigation aid. Distinguish precisely between firmware
   3.01, PowerPC, PS3 Cell, SPU and PowerXCell 8i. NVIDIA NV40/G70 is not
   automatically identical to RSX.
3. **Implementation:** modify the generator or runtime in `anyps3/`.
   Never skip unsupported behavior. A successful return may only confirm
   an operation that was actually performed; a known service may return a
   real error. An unknown protocol or instruction must explicitly stop the
   run.
4. **Static targets:** add a new indirect function or overlay only
   after verifying the original OPD/code bytes, TOC, range and image
   identity. For jump tables, verify the base, the sign/width of the
   offsets, the count and all targets. Evidence and provenance belong in
   the profile or a knowledge record.
5. **Test:** for a semantics change, add a native regression test with an
   independent expectation. Verify endianness, aliases, boundary values,
   errors and status flags as relevant. The test must not merely copy the
   algorithm of the change.
6. **New build:** always a new directory, a profile snapshot and the source
   hash. Do not overwrite previous artifacts. A one-off change in the
   generated C++ is not a permanent fix; a standalone diagnostic must be
   labeled as such.
7. **Real run:** execute the original game with real data and file
   storage. Preserve stderr/stdout, the return code, the stop and the
   captured frames. After a modification, do not demonstrate progress
   merely with a successful build.
8. **Verification:** compare the original PT_LOAD bytes, artifact hashes,
   the actual output and the scope of the tests. Keep clear/flip counts
   separate from geometry, captured frames, presentation to the window
   and reaching the menu.
9. **Write-up:** update `CURRENT.md`, a suitable topic record and the brief
   history in `docs/menu-progress.md`. Then address the next actual stop.

## Standard commands

From the project root, with the local game inputs available:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json \
  --profile profiles/ratchet-acit.json --max-steps 500000000 \
  -o local/ratchet/native-bundle-N

# The translation is split into units (units.json: anyps3.hpp, program.cpp
# with the runtime and main, ppu-*.cpp, spu-*.cpp); they are compiled in parallel.
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build -j 24 \
  local/ratchet/native-bundle-N local/ratchet/native-virtual-N
# without the virtual pad wrapper: python -m anyps3.native_build --vulkan local/ratchet/native-bundle-N

ANYPS3_AUDIO_SINK=null ANYPS3_FRAME_STRIDE=600 \
  ANYPS3_VIRTUAL_PAD_CONTROL=$PWD/local/ratchet/native-virtual-N/control.txt \
  PYTHONPATH=. python local/ratchet/run_native.py native-virtual-N 500000000

ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q
```

Replace `N` with a number that has not been used yet. Save the
build/compile/test/run outputs to their own logs. The read-only
verification helper `local/ratchet/run_native.py` sets the real disc,
cache, HDD, frame directory and SPU budget. Verify its current content
before use. The standard SPU limit is 10 million instructions per thread;
`ANYPS3_SPU_MAX_STEPS` explicitly sets a different positive uint64 budget.
Changing the limit is neither a semantic fix nor evidence of the menu.

Run targeted tests before an expensive game build. Run the whole
regression suite after a completed group of runtime changes; for RSX/Vulkan
changes, the Vulkan test must actually run. Do not repeat already
successful tests without a new change, a failure or a concrete doubt. A
mere documentation edit does not require compiling the game; verify links,
manifests and the availability of artifacts.

## Knowledge base and documentation

- `docs/knowledge/INDEX.md`: index and reading order.
- `docs/knowledge/CURRENT.md`: current verified state and the first next step.
- `docs/knowledge/HANDOFF.md`: handoff between models/runs.
- `docs/knowledge/WORKFLOW.md`: rules for knowledge records and documents.
- `local/reference/cell-docs/`: original PDFs, conversions, downloader and
  manifest.
- `local/reference/`: the actually available copies of RPCS3, QEMU,
  PSL1GHT, envytools and the RPCS3 wiki. Their exact paths are recorded
  by the knowledge base; do not assume that all of them are at the same
  depth.

State every important finding as **verified**, **derived** or
**hypothesis**. Attach the exact version of the source, the file and line
or the PDF page, the concrete runtime/test evidence, the limitations and
the consequence for further work. Also store refuted hypotheses if they
prevent repeating the same dead end. Do not leave key findings only in the
chat or in the memory of a specific model.

For PDFs, record the original URL, the title/version from the cover page,
SHA-256, page count, date of acquisition and conversion method. Preserve
the mapping to PDF pages. Do not label a tutorial as a handbook and do not
silently replace an unavailable version with a newer or older one. Text
conversion is not an exact reconstruction of diagrams/tables.

## Subagents and communication

Isolated documentation research can be delegated to the GPT-6.1 Sol
subagent, if available. Give it concrete outputs, files, rules and
ownership of paths. The runtime and the profile are managed by the main
agent; the documentation agent must not edit the same files at the same
time. Before handing over the work, wait for all assigned side tasks and
verify their results.

Do not modify reference repositories without a special reason, and read
their local `AGENTS.md` if you work inside them. Do not carry over the
rules for their upstream contributions onto changes to the AnyPS3 project
itself.

Communicate in Czech, concisely and continuously. State the blocker found,
the evidence of progress and what the next step addresses. Routine
reversible steps within the authorized scope do not require repeated
confirmation.

## Completion and handoff

The whole goal is done only with a demonstrable original game menu and
demonstrable gamepad navigation through the game's input path. A
standalone SDL controller test does not prove menu navigation. State
separately the virtual controller, the physical gamepad and any
verification that was not performed.

When the user allows finishing after a major advance, complete the side
tasks, verify the new run and, before leaving, write up: the changes, the
last actual stop, the exact commands and artifacts, tests, persisting
limitations, live processes and the first next action. Do not hide an
unfinished menu behind a partial milestone. Do not turn the original goal
into a narrower substitute goal. A pause for handoff is not completion;
continue or stop according to the user's latest instruction.
