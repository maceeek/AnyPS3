# RSX: FIFO, draw and the difference from presentation

## Verified implementation mapping

The local RPCS3 and envytools contain mutually comparable methods. The table below documents the naming and address in these checkouts; it does not by itself prove all RSX semantics.

| Byte offset | RPCS3 | envytools |
| --- | --- | --- |
| `0x1808` | `NV4097_SET_BEGIN_END` | `VERTEX_BEGIN_END` |
| `0x1814` | `NV4097_DRAW_ARRAYS` | `VB_VERTEX_BATCH` |
| `0x1824` | `NV4097_DRAW_INDEX_ARRAY` | `VB_INDEX_BATCH` |

Source: `local/reference/rpcs3/rpcs3/Emu/RSX/gcm_enums.h:674`, `:677`, `:681`; `local/reference/local/reference/envytools/rnndb/graph/nv30-40_3d.xml:471`, `:477`, `:487`, revisions in [SOURCES.md](SOURCES.md). **Fact (RPCS3):** its enum uses the byte offset `>> 2`, for example `gcm_enums.h:674`. Therefore do not compare the internal word index with the envytools byte offset without conversion.

## Sources for the specific next step

- `local/reference/PSL1GHT/ppu/librsx/commands_impl.h:35` and further functions show command emission into the buffer; `:119` begins the DMA surface configuration. The public producer is useful for comparing packets with the guest FIFO.
- `local/reference/PSL1GHT/ppu/include/rsx/commands_inc.h` describes the commands, `nv40.h` contains symbolic offsets, `gcm_sys.h` the public GCM interface.
- `local/reference/rpcs3/rpcs3/Emu/RSX/rsx_methods.cpp:1627` binds begin/end, `:1629` draw arrays and `:1630` draw index array; this dispatch is not itself an implementation of shaders/rendering.
- `local/reference/local/reference/envytools/rnndb/graph/nv30-40_3d.xml` describes the NV30/NV40 packet fields. Verify the offsets, variants and unknown fields; NVIDIA NV40/G70 documentation is not automatically a complete RSX specification.
- The [RPCS3 wiki Developer Information](../../local/reference/local/reference/rpcs3-wiki/Developer-Information.md) lists the RSX FIFO wiki as an incomplete source: the public [RSXFIFOCommands](https://www.psdevwiki.com/ps3/RSXFIFOCommands). This remote page is not passed off here as a currently verified local manual.

## Diagnosing missing geometry

Distinguish guest command production, FIFO decoding, draw submission, shader execution, surface contents and flip/presentation. Record the counts of guest begin/end and draw methods, the vertex/index buffer range, type/stride/offset/endianness, the validity of programs and surfaces. Then verify viewport/scissor, depth/blend/clear and the display buffer.

**Open ACIT question:** does the real guest get as far as draw, or is it still executing initialization? The answer should come from the current log, not from four historical black images. The host window, PPM file, clear and flip are separate results. The original menu requires real geometry/content and subsequently a verified gamepad state change.

The public CELL PDFs describe the CPU/MFC, not the RSX shader ISA. For RSX use the public implementations above and record the derivation and its limitations.
