# First image operation via Vulkan

AnyPS3 uses a single graphics backend: Vulkan. We are not developing our own
software rasterizer. Without an available GPU, the same code can be executed
on the CPU by the Mesa Lavapipe driver. This does not change the static
PPU/SPU recompilation and does not attach the RPCS3 backend.

## First verified scope (build 123)

On 2026-10-14 a real Vulkan render pass ran through Lavapipe; the reported
device was `llvmpipe (LLVM 23.1.1, 256 bits)`, Mesa 26.2.4. A synthetic RSX
FIFO stream executed five `NV4097_CLEAR_SURFACE` commands. The backend uploaded
the original VRAM contents into an RGBA8 image, executed `vkCmdClearAttachments`,
waited for the fence and read the result back into big-endian ARGB8 in VRAM.

The diagnostic compared **all 57,600 pixels** of the 320 × 180 surface,
including the alpha channel. It also verified the clipping of the rectangle at
the right and bottom edges, the preservation of row padding and of memory
outside the surface, zero/empty clears, and the rejection of unsupported
commands without advancing GET or changing pixels. The Khronos validation layer
with synchronization checking enabled reported neither errors nor warnings.

The output contains three colored rectangles and a small clipped rectangle on a
dark background. It is a **diagnostic pattern, not a frame of Ratchet**. It is
not evidence of triangle rendering, of RSX shader translation or of
presentation to the window. The PPM is produced from the VRAM that was read
back, not from a reference computation of the expected pixels.

The artifacts of the current verification are in the ignored
`local/vulkan-milestone/`: `rsx-vulkan-validated.ppm`, `rsx-vulkan.png`,
`validated.stdout.log`, `validated.stderr.log`, `tests-targeted.log` and
`tests-full.log`. The whole suite passed: **128 tests**, including the Vulkan
test required via `ANYPS3_REQUIRE_VULKAN=1` (no skipped tests).

The real Ratchet was also rebuilt with `-DANYPS3_VULKAN -lvulkan`
(`local/ratchet/native-bundle-123`). The run reached the same 102,817,041 PPU
steps, 24,360 completed SPU instructions and 331 RSX packets as build 121; it
stopped at `GDAT initGameData`. It reports `rendered clears=0 presented_frames=0`.
The original game load segments remained unchanged. `verification.json`
contains the segment check and the hashes of the sources, the built program, the
ELF and the logs. The Vulkan device was not yet created in this run; the pixel
path is verified by the separate diagnostic FIFO above.

## Reproduction without game data

On CachyOS/Arch:

```bash
sudo pacman -Syu vulkan-headers vulkan-validation-layers vulkan-tools vulkan-swrast
mkdir -p build
python -m anyps3.render_demo -o build/rsx-vulkan.cpp
c++ -std=c++17 -O1 -Wall -Wextra -Werror -DANYPS3_VULKAN \
  build/rsx-vulkan.cpp -lvulkan -o build/rsx-vulkan
VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.json \
VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation VK_LAYER_VALIDATE_SYNC=1 \
  build/rsx-vulkan build/rsx-vulkan.ppm
```

Both the source and the resulting PPM require a new name; an existing frame is
not overwritten. The PNG in the local artifacts is a lossless conversion of this
PPM. Without `VK_DRIVER_FILES`, the backend prefers an available discrete GPU,
then an integrated one. The GPU must be available to the process and must have a
working Vulkan ICD. The current working sandbox does not see `/dev/dri`; the
hardware path is not verified here. The backend does not require X11, Wayland or
swapchain extensions.

`vulkaninfo` on its own tried to connect to an unavailable display in this
sandbox. For headless diagnostics the following works here:

```bash
env -u DISPLAY -u WAYLAND_DISPLAY \
  VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.json vulkaninfo --summary
ANYPS3_REQUIRE_VULKAN=1 python -m unittest tests.test_vulkan -v
```

Without development headers or a usable device, the integration test is
explicitly skipped; with `ANYPS3_REQUIRE_VULKAN=1` a missing device is an error.
The test does not skip compilation failures or incorrect image output.

## Extension for a real game clear (build 131)

The game has already performed its own clear of the 960 × 704 surface through
Vulkan. The color and depth surfaces use tiled addressing and two horizontal
samples. We support the complete masks `0xf0`, `0x03` and `0xf3`; Z24S8 is
transferred during clearing as an exact 32-bit value via RGBA8, so far without
depth testing. Addressing respects pitch and bank. The full suite at this point
passed with 133 tests; the extended Vulkan test also checks Z24S8, both
samples, scissor and unchanged surrounding bytes.

For supported compressible tiles we keep the uncompressed values; this is not a
decoder of compressed data. Later reads, blits and rendering must respect this
representation. An overview of the further progress is in
[progress toward the menu](menu-progress.md).

Build 138 further completed four real game flips. The headless sink selects the
buffer designated by the game's command, converts its tiled X8R8G8B8 pixels,
performs the Vulkan upload/readback and stores a new PPM under
`ANYPS3_FRAME_DIR`. Only after a successful fence and write does it update the
flip state and send the permitted event to the original firmware. The frames are
so far entirely black. No window or swapchain was created; the
`captured_frames` counter is kept separate from `presented_frames`. The test
also compares the whole captured frame with the actually rendered pixels of the
diagnostic and verifies that an output failure does not release the waiting
consumer.

## Backend limitations

The supported clear uses an ARGB8 buffer A and/or Z24S8 in local VRAM, one or
two samples and a zero surface-clip origin. Scissor, pitch and offset are
respected. Partial channel masks or stencil masks, other formats, MRT and
main-memory surfaces explicitly fail. The same applies to drawing, shader
uploads and blits. Support for two samples during clearing is not MSAA support
for geometry drawing.

Vulkan is optional when building the generated program, via
`-DANYPS3_VULKAN` and `-lvulkan`. Without it, the existing CPU/IPC tests
continue, but a non-empty clear ends with an explicit error. Creation of the
Vulkan device is deferred until the first supported non-empty clear.

Each clear now allocates transient resources and synchronously copies the
surface there and back. This is a verifiable baseline of correctness, not a
performant renderer. Further work: persistent GPU surfaces and their coherence
with VRAM, conversion of RSX shaders to SPIR-V, draw calls and a swapchain for
display in a window.

Technical sources: local RPCS3 `gcm_enums.h` and `rsx_decode.h` for the
register layout; [Vulkan in the ArchWiki](https://wiki.archlinux.org/title/Vulkan),
[Mesa drivers](https://docs.mesa3d.org/systems.html) and the Vulkan headers.
