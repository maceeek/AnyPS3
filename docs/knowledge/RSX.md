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

## Host cost of draws: cloud optimization round (2026-10-19/20)

Scope: AnyPS3 runtime and its native tests, Lavapipe in a container without a GPU; no game frame was rendered. Evidence, benchmarks and
limitations: [cloud optimization record, RSX](experiments/2026-10-19-cloud-optimization.md#rsx).

- **Register epochs (verified by `tests/test_rsx_register_epochs.py`):** per-256-byte-block epochs are bumped only when a tracked `write`
  changes presence or value; a mutable borrow marks the block exposed, so it is always verified by value, and copy/assignment forces a full rebuild.
- **MSAA plane runs (verified for pixels/depth/stencil and Khronos plus sync validation; effect on GPU time unmeasured):** plane 0 of
  consecutive draws on the same attachments stays in one render pass and planes >=1 are recorded as runs when that pass ends; every
  command outside a pass replays pending planes first. `ANYPS3_RSX_PLANE_RUNS=0` restores the old recording. **Hypothesis (weak):** part
  of the ~30 ms/frame GPU busy time in the level is render-pass switching (~1712 passes per frame before this change). Caveat
  (code comment `anyps3/vulkan.py:222-224`): the opt-in multiview variant with 76 passes raised GPU wait 5.6 → 30 ms/frame on the RTX 4070 SUPER.
  Distinguishing test: A/B `ANYPS3_RSX_PLANE_RUNS=0/1` in the same level window, comparing `PERF GPU busy_ns`, passes per frame and FPS.
- **Stale `tests/test_rsx_gpu_upload.py` oracle (verified on a worktree of `43c6b27`):** it pasted `raw_vertex_glsl` into its own compute
  shader, but since build 460 that code reads the state arena (`state_`), so the module failed with `'state_' : undeclared identifier`.
  The oracle now maps `state_` to its single buffer; no runtime change was needed (production vertex shaders insert `state_glsl`).
- **Occlusion-query elision is a candidate only, not implemented:** every draw wraps each plane in a precise occlusion query; the counts
  are guest-visible only while ZPASS pixel counting (register 0x17cc) is enabled, otherwise they feed the diagnostic `draw.pixels`, an
  oracle of 20 test modules. Eliding queries while 0x17cc is 0 needs a separate diagnostic mode and is worth it only if a PC profile shows query cost.
