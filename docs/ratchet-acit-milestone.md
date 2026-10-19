# Ratchet & Clank: A Crack in Time — first milestone

Historical state of the first pass. The download has since finished; the current results
of the full extraction and the memory runtime are in the [second milestone](ratchet-acit-memory-milestone.md).

Date: 2026-10-13. Goal: to verify AnyPS3 on a real binary, obtain a concrete
map of further work and natively run the first translated functions.

## Result achieved

The whole EBOOT was obtained from the files provided by the user. All the program
segments taken over and the section table passed the HMAC-SHA1 check. Two small
functions were translated to C++, built with the local compiler and run
natively. They return the expected values 5 and 21.

These are simple return functions with four instructions in the reachable path,
not engine initialization, rendering or the launch of the whole game.

## Input and completeness

| Item | Finding |
|---|---|
| Identity from PARAM.SFO | BCES00511, APP_VER 01.00, VERSION 01.00 |
| Minimum system version from PARAM.SFO | 03.0100 |
| ISO volume | RATCHET5_5 |
| Downloaded archive | 18,197,200,824 bytes, extension `.zip.crdownload` |
| Declared compressed size of the ISO in the ZIP | 20,806,817,955 bytes |
| Recovered part of the ISO | 21,956,151,873 bytes |
| Declared size of the ISO | 24,772,083,712 bytes |
| Files | 811 unique files; 661 extracted, 150 unavailable |
| Extracted data | 21,949,243,531 bytes |
| EBOOT.BIN | 14,517,840 bytes, retail APP SELF, key revision 1 |
| Analysis ELF | 14,515,408 bytes, ELF64 big-endian PowerPC64 |

The initial inventory contained 812 file records. The file
`GLOBAL_UNCACHED.PSARC` has two extents; after merging them the count is 811.
The extraction supports them and must not overwrite the first with the second extent.

The archive is incomplete and the CRC of the whole ISO cannot be verified. Only files
all of whose extents lie in the recovered part of the image were extracted. The original
downloaded file was not modified. The list of missing files is in the local extraction manifest.

SHA-256 of EBOOT.BIN:
`0d3c7e78fc66a8f964fbaab9d90436b717a8f60ec999f1245d1134131e7e9f83`

SHA-256 of the analysis ELF:
`6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`

## Scope of the SELF verification

Five program sections were taken over (including three empty ones) and the ELF section table.
All six have a successfully verified HMAC. Unallocated auxiliary SELF data of type 3 is
not carried over; this block has no meaning as a load segment. The original section names are not
available and are identified by indices and addresses.

The analysis ELF **is not a byte-exact reconstruction of the original ELF**. Gaps
and unrecovered data outside the load segments are zero. The SHA-1 given in the original
SELF therefore does not match; the state is stated explicitly in the report. The ECDSA signature of the SELF
was not verified. A matching HMAC verifies the content taken over, not the authenticity of the whole distribution.

The format and the choice of parameters for APP revision 1 were verified against
[RPCS3 unself](https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Crypto/unself.cpp)
and [RPCS3 key vault](https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Crypto/key_vault.cpp).
The parameters for the local extraction are stored only under the ignored `local/`;
the tool neither contains nor downloads any keys.

## Binary map

| Item | Result |
|---|---|
| ELF entry / OPD | `0x00ca6d48` |
| First instruction | `0x00010280` |
| TOC | `0x00d34a88` |
| OPD section | index 22, address `0x00ca6d38`, size 548,176 bytes |
| Valid OPD descriptors | 68,522 |
| Unique code addresses from the OPD | 34,512 |
| Imported functions | 246 from 17 libraries |
| Embedded structurally valid SPU ELFs | 3 |
| Entries accepted by the current translator | 1,370, limit of 256 instructions per entry |

The addresses from the OPD are not a complete list of all game functions. The number of accepted
entries includes empty returns, trivial functions and stubs. It is not a percentage
of playability; a bulk pass is a static check, not a test run of all functions.

The embedded SPU ELFs begin at the file offsets `0x00be4480`,
`0x00bedc00` and `0x00bfc300`. The SPU programs have not been executed yet.

| Library | Imports |
|---|---:|
| cellSpurs | 36 |
| cellSysutil | 32 |
| cellHttp | 28 |
| sysPrxForUser | 27 |
| sys_fs | 19 |
| sceNp | 18 |
| cellGcmSys | 16 |
| cellNetCtl | 12 |
| sys_io | 12 |
| cellSsl | 11 |
| sceNpTrophy | 11 |
| sys_net | 8 |
| cellAudio | 7 |
| cellSysmodule | 4 |
| cellHttpUtil | 2 |
| cellRtc | 2 |
| cellUserInfo | 1 |

The NIDs and addresses of the imports are in the JSON report; the names of the individual functions
are not resolved yet. A static import does not mean that the game calls it at startup.

## Native verification

| OPD | Code address | Instruction count | Result |
|---|---|---:|---|
| `0x00d21cd8` | `0x009d5548` | 4 | `r3=5` |
| `0x00d21d00` | `0x009d5588` | 4 | `r3=21` |

The outputs were built via `c++ -std=c++17 -O2 -Wall -Wextra -Werror`.
These specific functions need no host equivalent of memory or system
services. The test `main` is not the entry point of the whole game.

## First real blocker

The translation of the startup processes three instructions setting the register `r2` and stops:

```text
0x0001028c: 0x80420004 — lwz r2,4(r2)
```

`stdu`, `std` and `bl` follow. Therefore the following takes priority in further work:

1. The ELF memory map, big-endian load/store and stack initialization.
2. `lwz`, `ld`, `std`, `stdu`, LR preservation and function calls.
3. Conditional branching and diagnostics of the first really invoked system service.
4. SPU/SPURS and graphics depending on where the real run gets to.

## Local artifacts and repetition

The game data and generated code remain in the ignored folder `local/ratchet/`.

| Path relative to the project | Content |
|---|---|
| `local/ratchet/disc.partial.iso` | Recovered part of the ISO (deleted 2026-10-14, replaced by `disc.complete.iso`) |
| `local/ratchet/game/` | 661 extracted files and a manifest (deleted 2026-10-14, replaced by `game-complete/`) |
| `local/ratchet/recovery.json` | State of the recovery of the incomplete ZIP |
| `local/ratchet/disc-inventory.json` | Inventory and availability of files |
| `local/ratchet/extraction.json` | Hash of every extracted file and missing paths |
| `local/ratchet/EBOOT.analysis.elf` | Analysis ELF |
| `local/ratchet/self-integrity.json` | HMAC verification, hashes and reconstruction limitations |
| `local/ratchet/analysis.json` | Sections, imports, SPU and results of the translator pass |
| `local/ratchet/leaf-return5`, `leaf-return21` | Built native tests |

```bash
python3 -m anyps3 sfo local/ratchet/game/PS3_GAME/PARAM.SFO
python3 -m anyps3 inspect local/ratchet/EBOOT.analysis.elf \
  --profile profiles/ratchet-acit.json --json
python3 -m anyps3 analyze local/ratchet/EBOOT.analysis.elf
./local/ratchet/leaf-return5
./local/ratchet/leaf-return21
python3 -m unittest discover -s tests -v
```

A new extraction can be performed via `recover-zip`, `disc-extract --allow-partial`
and `unself --key-file`. The output files and the target folder must be new.
The current test suite has 43 tests; all passed, including a synthetic
encrypted SELF, ZIP64, multi-extent ISO and the native build.
