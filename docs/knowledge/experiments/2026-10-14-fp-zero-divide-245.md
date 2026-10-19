# PPU division by zero and the original loading clamp (245)

The goal remains the original functional ACIT menu and gamepad navigation. This record
does not confirm the menu or navigation. Date 2026-10-14; Git is not available.

## Reproduction and original instructions

**Verified:** `native-virtual-244/verification.json`, `stderr.log` and the independent
`terminal-identity.json` document the stop `0x69f2b4`, LR `0x69f060`,
308,205,078 PPU steps, `FP division by zero is unsupported`.
f13=154.0 (`0x4063400000000000`), f6=+0.0; FPSCR `0x82004000`.
Original ELF SHA256 `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`.
Exact words, lift and operand evidence: `local/ratchet/target-245-evidence.json`.

Original sequence `0x69f2b0..0x69f2c0`: FRSP, FDIVS (`ed8d3024`),
FSUB, FSEL, STFS. FSEL selects f8=0.75 when the result of FSUB is non-negative.
**Derived:** a correct +Infinity from the division and the subsequent subtraction of 0.75
let the original code store 0.75; the division does not require skipping the instruction.

## Sources and boundaries

**Verified from the text of the original manual:** IBM *PowerPC User Instruction Set
Architecture, Book I*, v2.02, January 28, 2005,
[original PDF on a mirror](https://powerpc.dev/general/PPC_Vers202_Book1_public.pdf):
physical pages 97–99 (FPSCR, FPRF), 101 (infinity/NaN), 107–108
(invalid operation and zero divide; printed pages 97–98).
With ZE=0, a finite nonzero number / zero returns infinity with the XOR of the signs,
sets ZX, clears FR/FI and classifies FPRF. 0/0 and Infinity/Infinity are
different, invalid operations (VXZDZ/VXIDI); opposite-signed infinities in addition give VXISI.
The generated quiet NaN is `0x7ff8000000000000`.

Cell Architecture v1.02, original local PDF page 47, section 4.1,
states Book I compatibility without incompatibilities and at the same time warns that CBEA
does not automatically adopt later PowerPC extensions. Therefore a newer Power ISA is not used
here as a silent substitute. The local hash of the Cell PDF is in the manifest.

Supplementary primary source: Motorola/IBM *PowerPC Microprocessor Family:
The Programming Environments*, Rev.1, 1/97,
[PDF at NXP](https://www.nxp.com/docs/en/user-guide/MPCFPE_AD_R1.pdf),
physical page 145, Table 3-13, confirms the ±Infinity result for ZE=0.
RPCS3 `PPUInterpreter.cpp:6198` and `:6536` at revision
`ffba2bf09ef3980638f9327b25240d81414034ab` performs host division; this
reference by itself is not evidence of the complete FPSCR semantics and the algorithm was not copied.

**Source acquisition limitations:** the web tool opened Book I (230 pages) and the NXP PDF
(828 pages). The local download of Book I via curl failed on DNS (log
`/tmp/anyps3-powerpc-download.log`); a new local PDF and its SHA256 were not
obtained. Requests for web screenshots of the tables/exception pages mostly ended in a
cache miss. The conclusions are supported by the text of the original PDFs and by the agreement of both primary
sources, not by a claim of a complete new local library or a reconstruction of the tables.

## Permanent change and tests

`anyps3/floating.py:36`: the new exception flags set FX only on a
0→1 transition of the specific sticky bit; invalid cases set VX.
`:165` supports special division in both precisions; `:205` supports
the subsequent addition/subtraction with infinities. FR/FI and FPRF are updated,
older exception bits remain sticky. The finite arithmetic algorithm is unchanged.
Unsupported rounding/NI/enabled-trap modes still stop explicitly.
Other instructions with infinities are not generally solved by this change.

`tests/test_floating.py:34`: the native regression vectors have literal IEEE/FPSCR
expectations, test both precisions, sign XOR, subnormal/zero,
0/0, Infinity/Infinity, Infinity/0, finite/Infinity, NaN precedence,
sticky FX/ZX/VX, FR/FI, CR1 record, destination aliases and the original five
instructions including the big-endian guest store of the value `0x3f400000`.
The unsupported-mode test checks that FPR, PC, CR and FPSCR are unchanged.
The new test demonstrably failed before the fix on the original explicit stop:
`local/ratchet/tests-fp-245-before.log`. After the change, six targeted tests OK
(10.194 s), `local/ratchet/tests-fp-245-final.log`.

Source hash of `floating.py`: `e295daf36fc5155ae1d357931e6102322c28a67b939eacbdc37542bf2597dcab`;
test hash: `053da24063cd9c49c04218ec769d65d3240bdfde646b38bcdecf65a3eaad4ca6`.
Profile 245 is byte-identical to 244; the only runtime source change is floating.py.

## New build and verification

```bash
python -m unittest tests.test_floating -q > local/ratchet/tests-fp-245-final.log 2>&1
ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q > local/ratchet/tests-native-245.log 2>&1
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 5000000000 -o local/ratchet/native-bundle-245 > local/ratchet/build-245.log 2>&1
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-245 local/ratchet/native-virtual-245 > local/ratchet/build-virtual-245.log 2>&1
```

At the time of the first write, the full regression suite is live in session 10130; the original
build 245 in session 72516 completed successfully. The C++ compilation of the diagnostic SDL
wrapper is live in session 50860. Game 245 has not been launched yet. The controller will be
neutral; exact confirmation of the runtime progress requires a subsequent real run.

### Update after compilation and the full suite

**Verified:** 175 tests, 202.918 s, OK with no skips; Vulkan required and actually
executed according to `tests/test_vulkan.py`. `tests-native-245-result.json` confirms
exit 0. Session 10130 terminated; compilation 50860 exit 0.
The original PT_LOAD of the new ELF, the source/profile hashes and the byte-identical base C++
verified; the three diagnostic manifest hashes verified after compilation. The evidence
`native-virtual-245/input-identity.json` is input evidence, not terminal/menu evidence.

```bash
ANYPS3_VIRTUAL_PAD_CONTROL=/home/martink/AnyPS3/local/ratchet/native-virtual-245/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-245 5000000000 > local/ratchet/run-virtual-245.log 2>&1
```

The real game 245 is live in session **6682**. The game inputs/control remain
neutral. 244 was not re-run and this is not merely a synthetic test.
First next action: poll the same handle, compare its actual stop/captures;
after the terminal state, include its own verification and the diagnostic manifest/base hash.

At 17:14:00 UTC the same handle was confirmed live, without STOP and without terminal
verification: 231 reads / 11,634,763 bytes, draw 6400, 13 captures.
The frame `frame-000013.png` visually confirms the original loading planet.
The FP stop has not yet been passed by this run; no menu/nav claim.

### Final real run 245 (17:27:41 UTC)

**Verified:** session 6682 terminated, game returncode 1; new stop
`pc=0x69dd30`, `lr=0x69dabc`, 308,156,479 PPU steps:
`target has no statically translated block`. FPSCR `0x86002000` contains
sticky ZX; the original game thus actually executed the supported division by zero.
The log does not trace the exact preceding PC of this operation; do not confuse this evidence
with an instruction trace of the specific `0x69f2b4`. Its exact original sequence
is separately demonstrated by the native regression. FP zero-divide no longer stopped this run.

444 reads / 17,897,130 bytes, both original front_end archives opened.
SPU 0x100 878,746,602 instructions, waiting; 15,252 flips, 30,496 draws,
33 captures, presentation to the window 0. Frame 33 is still the loading planet.
Its own `verification.json` and the independent `terminal-identity.json` verified:
six artifact and three diagnostic hashes, identical base translation and all
original PT_LOAD bytes. The controller stayed neutral, menu/nav are not demonstrated.
The new stop is case 13 of the original table; continued in
[jump table 246](2026-10-14-byte-switch-246.md).
