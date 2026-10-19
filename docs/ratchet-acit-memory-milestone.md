# Ratchet & Clank: A Crack in Time — complete disc and memory runtime

Date: 2026-10-13. Follows the [first milestone](ratchet-acit-milestone.md).

The download finished. The whole image passed the CRC, all 811 files are extracted,
and the first translated function that actually reads game data was built and run
natively. The game does not start yet; function calls and system services are not finished.

## Integrity and extraction

| Item | Verified result |
|---|---|
| ZIP | 20,806,818,527 bytes, valid readable central directory, 2 entries |
| ISO | 24,772,083,712 bytes, CRC32 `7b8065f1` matches the ZIP |
| README in the ZIP | CRC verified by a separate read via `zipfile` |
| ISO extraction | 811 files, 0 missing, 24,767,909,758 bytes |
| Comparison with the partial extraction | SHA-256 of all 661 previously obtained files match |
| Newly obtained files | 150, including `DEBUG.DAT` and other videos/data |

The ISO CRC check took place during the full decompression by the `recover-zip` tool.
Reading the central directory and the README was additionally verified via the standard
Python library. `recover-zip` itself still checks only the ISO entry,
so its report keeps `source_archive_complete: null`.
A summary of both checks is in `local/ratchet/complete-verification.json`.

The EBOOT and the analysis ELF have the same hashes as in the first milestone. The profile
BCES00511 01.00 remains valid. The limitations of the SELF reconstruction and of the unverified
ECDSA signature described in the first milestone do not change.

`DEBUG.DAT` is 502,843 bytes and begins with `IGHW`. A simple pass found
15,299 printable ASCII strings of length at least 5, among them names of classes,
enums and objects. This is a lead for further analysis of the data; so far we have no verified
link between these names and the addresses of PPU functions, nor a parser of the whole format.

## New implementation

The translator supports the D-form instructions `lwz`, `lbz`, `lhz`, `lha`, `stw`,
`stb`, `sth`, their update variants and the DS-form `ld`, `ldu`, `lwa`, `std`,
`stdu`. It rejects invalid update operands and reserved DS variants.

The generated C++ memory runtime loads the PT_LOAD segments from the original ELF,
fills BSS with zeros and creates a 1 MiB stack with the initial SP `0xd00fff00`.
Big-endian accesses check ranges and permissions; a game address is never directly
a host pointer. Writes to executable segments, mapping overlaps,
accesses across a segment boundary and a map over 512 MiB are rejected. Memory
errors report the address of the executed instruction.

The program requires the ELF as an argument. Before loading it compares the size and CRC32
with the input at generation time. The CRC serves against accidental mix-ups, it is not a signature
or a cryptographic verification. The memoryless demo still works without an argument.
The stack is for testing; it has no process arguments, TLS or engine initialization.

## Native evidence on a real function

| Item | Value |
|---|---|
| OPD | `0x00d2bec8` |
| Code | `0x009011b8` |
| TOC | `0x00d34a88` |
| Instructions | `lwz r3,-6352(r2); blr` |
| Address read | `0x00d331b8` |
| Bytes in the ELF | `00 de 50 d8` |
| Expected and actual r3 | `14569688` (`0x00de50d8`) |
| Return code of the native program | 0 |

The expected value was independently read from the ELF mapping in Python and compared
with the output of the native program. This is an isolated two-instruction function over the
initial ELF data, not an engine run or evidence that a loaded object works.

The static pass now accepts 2,971 of 34,512 unique entries from the OPD
(previously 1,370), with a limit of 256 instructions. The count includes trivial functions
and does not verify the validity of arguments or runtime state. It is not a percentage of playability.

The translation of the startup now passes the first `lwz`, `stdu` and `std`, but stops at:

```text
0x0001029c: 0x48000115 — bl 0x000103b0
```

The whole startup was neither generated nor run. The next concrete task is LR,
calls/returns, then conditional branching and the first service actually reached.

## Verification and repetition

All **54 tests passed**. The memory tests build and run synthetic
programs for endianness, BSS zeroing, load widths and signs, update forms,
negative DS displacement, preservation of the old SP at `stdu`, invalid mappings,
access permissions and a mix-up of the input ELF. Selected opcodes were additionally verified
by the local LLVM assembler. The native build uses C++17,
`-O2 -Wall -Wextra -Werror`.

```bash
python3 -m unittest discover -s tests -v
python3 -m anyps3 inspect local/ratchet/EBOOT.analysis.elf \
  --profile profiles/ratchet-acit.json --json
./local/ratchet/leaf-memory-verified local/ratchet/EBOOT.analysis.elf
```

For a new translation use a new output name:

```bash
python3 -m anyps3 recompile local/ratchet/EBOOT.analysis.elf \
  --entry 0xd2bec8 -o local/ratchet/leaf-memory-new.cpp
c++ -std=c++17 -O2 -Wall -Wextra -Werror \
  local/ratchet/leaf-memory-new.cpp -o local/ratchet/leaf-memory-new
./local/ratchet/leaf-memory-new local/ratchet/EBOOT.analysis.elf
```

| Local artifact | Content |
|---|---|
| `local/ratchet/disc.complete.iso` | Complete ISO, CRC verified |
| `local/ratchet/game-complete/` | Complete extraction with a manifest and the SHA-256 of every file |
| `local/ratchet/complete-verification.json` | Summary of integrity and the comparison with the partial extraction |
| `local/ratchet/recovery-complete.json` | Verification of the decompression and of the CRC of the ISO entry |
| `local/ratchet/disc-inventory-complete.json` | Complete ISO inventory |
| `local/ratchet/analysis-memory.json` | New static pass of the EBOOT |
| `local/ratchet/native-memory-verification.json` | Comparison of the expected and the natively loaded value |
| `local/ratchet/leaf-memory-verified.cpp`, `leaf-memory-verified` | Generated code and native program |
| `local/ratchet/debug-strings.txt` | Auxiliary dump of the strings of DEBUG.DAT |
| `local/ratchet/tests-memory.log` | Results of the whole test suite |

The game data, local key parameters, derived code and large images remain
in the ignored folder `local/`. The original archive and the partial extraction were not modified.
