# SPU: ISA, channels and semantics checking

The primary reference is [SPU ISA v1.2, 27 January 2007](../../local/reference/cell-docs/pdf/spu-isa-v1.2.pdf), IBM/Sony/Toshiba, 279 physical PDF pages. The document states 278 pages in its footer; physical page 279 is a questionnaire. For citations use `PDF page N`, not the total from the footer.

## Verified facts from the manual

| Topic | Exact meaning | PDF page |
| --- | --- | --- |
| Registers | 128 GPRs, each 128 bits; floating-point semantics must be read per instruction | 25 |
| Preferred slot | the first four bytes of the register for scalar values/addresses; not an arbitrarily chosen host lane | 28 |
| Load/store | aligned 16-byte quadwords, the lowest 4 address bits are ignored; LSLR additionally masks the effective address | 31 |
| `rdch` | waits for data; an invalid readable channel leads to a stop according to the ISA | 248 |
| `rchcnt` | returns the capacity in the preferred slot, other bytes zero; an unimplemented channel has capacity zero | 249 |
| `wrch` | waits for free capacity; a 32-bit channel uses the preferred slot | 250 |

These conclusions are **facts of the public ISA**, not results of AnyPS3 tests. The [searchable conversion](../../local/reference/cell-docs/md/spu-isa-v1.2.md) preserves the pseudocode text, not the exact drawn bit boundaries. For opcodes and field ranges, open the PDF. The notation `2^32` may lose its superscript after extraction; the text `232` is not the decimal number 232.

## Reference implementation

The RPCS3 checkpoint is in [SOURCES.md](SOURCES.md): `local/reference/rpcs3/rpcs3/Emu/Cell/SPUOpcodes.h` for fields and opcodes; `SPUInterpreter.cpp` for individual operations; `SPUThread.cpp` for channels and MFC; `SPUThread.h:41` and `:61` for the SPU/MFC channel enumerations. Look up the specific function via `rg` and record its current line and commit before use. Use the reference emulator's behavior as an implementation comparison, the manual as the architectural source.

## What to record when fixing

The input instruction word, the decoded mnemonic/fields, the PC, the source registers, the required output and the channel/DMA state. With source/destination aliasing, verify that the original operands are read before the write. For shuffle/shift/rotate, record the byte order and the boundary shift counts. For FP, separate SPU from PPU and PowerXCell; generic host IEEE behavior may not match the target instruction.

Active runtime questions belong in [CURRENT.md](CURRENT.md). The creation of this overview does not change the SPU runtime or the tests.

## Pool lanes: cloud optimization round (2026-10-19/20)

Scope: AnyPS3 runtime and its native tests; nothing here is verified in the game. Evidence, benchmarks and limitations:
[cloud optimization record, SPU](experiments/2026-10-19-cloud-optimization.md#spu).

- **Reservation-lost change-counter filter (exactness derived by main-agent review, behaviour verified by
  `tests/test_spu_reservation_filter.py`):** every version bump already calls `notify()`, which now also increments a per-line-hash
  (4096 buckets) or global counter. An SPU samples the counters before a successful `reservation_token` compare and skips the full
  compare while they are unchanged for the same granule and memory. Exact because the writer bumps the version under the stripe lock and
  publishes afterwards while the reader samples before its locked compare, so either the compare sees the new version or the next
  sample differs; collisions only force a full compare, and a line with a synchronous host-access observer always takes the full compare.
  Limitation: a future version bump that bypasses `notify()` would break the filter silently (`test_par4` pins that every current bump notifies).
- **Busy-poll parking (verified by `tests/test_spu_pool_poll.py`, pool lanes only):** a repeated GETLLAR of an intact reservation or
  `rchcnt` of channel 0 returning 0, at the same site with identical register file/fpscr and only idempotent channel traffic in
  between, spins 50 µs and then throws `SpuBlocked` before any side effect; the retry executes the poll for real, so results never
  change, only timing. `ANYPS3_SPU_POLL_PARK=0` disables it. **Hypothesis:** the level's CellSpursKernel0 (SPU 0x100) matches one of
  the two patterns. Risk: a poll loop whose only changing state is a counter in local store would be slowed by the park timeout.
- **Slice 4096 (verified by `tests/test_spu_pool_quantum.py`):** a pool lane free-runs 4096-instruction slices for requests of at
  least the PPU tick budget (1024); smaller requests stay exact for stepping tests. The lane loop reads the host clock only with `ANYPS3_PERF_REPORT=1`.
