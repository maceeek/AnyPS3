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

**Answered (2026-10-16 onwards, PC):** the guest draws the original menu and `great_clock_a` through the `gpu` backend.
The open RSX questions are now the worker's CPU time per draw and the GPU time at render scale 2
([CURRENT](CURRENT.md)). Keep the host window, a PPM capture, a clear and a flip separate as evidence.

The public CELL PDFs describe the CPU/MFC, not the RSX shader ISA. For RSX use the public implementations above and record the derivation and its limitations.

## Runtime structure (verified in the code, 2026-11-01)

- **Threads:**
  - With `ANYPS3_RSX_WORKER=1`, a FIFO owner thread decodes and executes the guest FIFO
    (`anyps3/rsx_game_worker.py`); every GUI launch sets it, while the binary's default is serial.
  - With `ANYPS3_RSX_RECORDER=1`, an optional draw-recorder thread records Vulkan command buffers. It is off by default
    (`anyps3/rsx_vk_graphics.py` `recorder_start`) and the GUI keeps it off (624b teal-frame question). Since HIRES7 it also
    runs batch reset/begin, the submit and plane clears (`ANYPS3_RSX_RECORDER_SUBMIT`, default 1).
- **Draw backends** (`ANYPS3_RSX_BACKEND`, `anyps3/rsx_draw.py`): `gpu` (default, the Vulkan graphics pipeline); `cpu` and
  `compute` are diagnostic reference rasterizers. A draw the GPU path rejects stops the run unless
  `ANYPS3_RSX_GPU_FALLBACK=1`.
- **Render scale** `ANYPS3_RENDER_SCALE` (integer 1–4, default 1; the goal uses 2): visual companion surfaces with two sample planes per pixel at scale 2.
  Optional visual 2× MSAA working images (`ANYPS3_RENDER_SCALE_MSAA`, default 0).
- **Recording:**
  - MSAA plane runs keep plane 0 of consecutive draws in one pass (`ANYPS3_RSX_PLANE_RUNS`, default 1).
  - Occlusion-query elision while ZPASS counting is off is opt-in (`ANYPS3_RSX_LAZY_VISIBILITY=1`, default 0, no game A/B).
  - The opt-in multiview variant raised GPU wait 5.6 → 30 ms/frame on the RTX 4070 SUPER (comment at
    `anyps3/vulkan.py:282-284`).
- **Presentation:** `ANYPS3_PRESENT=sdl` presents through an SDL2 Vulkan swapchain; headless runs need no window system.
  PPM captures happen only with `ANYPS3_FRAME_DIR` (the first 8 flips, then every `ANYPS3_FRAME_STRIDE`-th).
- **Device selection:** `ANYPS3_VK_DEVICE=auto|gpu|lavapipe`.
- **Performance work and A/B switches** since 2026-10-27 are in [CURRENT](CURRENT.md); the history of draw-cost work
  before 2026-10-25 is in the archived [cloud optimization record](../archive/knowledge/experiments/2026-10-19-cloud-optimization.md#rsx).

## Reproduction without game data (lavapipe)

```bash
sudo pacman -Syu vulkan-headers vulkan-validation-layers vulkan-tools vulkan-swrast
mkdir -p build
python -m anyps3.render_demo -o build/rsx-vulkan.cpp
c++ -std=c++17 -O1 -Wall -Wextra -Werror -DANYPS3_VULKAN build/rsx-vulkan.cpp -lvulkan -o build/rsx-vulkan
VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.json \
VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation VK_LAYER_VALIDATE_SYNC=1 \
  build/rsx-vulkan build/rsx-vulkan.ppm
ANYPS3_REQUIRE_VULKAN=1 python -m unittest tests.test_vulkan -v   # the same path as a test
```

With `ANYPS3_REQUIRE_VULKAN=1` a missing device is an error, not a skip. The game's renderer itself is built with
`python -m anyps3.native_build --vulkan` (glslang/SPIRV libraries required).
