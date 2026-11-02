# RSX draw: software renderer into the guest surface (builds 216–233)

Date: 2026-10-14, Claude Opus 5.5. The goal remains the original ACIT menu and gamepad navigation.

## Verified (runtime)

- After the PPU/trophy stops were removed, the game sends real RSX draw commands (FIFO put `0x17c096c`, bundle 216+).
- Bundle 220: the game's first real draw: QUAD 1280×720 (921,600 px), FP `MOV H0,{0,0,0,0}`.
- Bundle 228: draw #2 rendered 17,161 px from an original texture (960×704 LN UN A8R8G8B8 and others); frame 5 contains the loading icon
  (a planet) at the bottom left. Bundle 231: 256 flips with an animated icon (loading screen), stopped only at the capture cap.
- Diagnostics (bundle 218/219): VP of 14 instructions (2D pixel→clip conversion: `c[6]=(640,360,640,360)`), stride 28, position float4,
  UV float2 (attr1), color UB4 (attr9), viewport offset (640,360) scale (640,−360), surface A8R8G8B8 linear pitch 5120.

## Implementation (`anyps3/rsx_draw.py`, `anyps3/rsx.py`)

- Guest memory remains the source of truth (as for clear/scanout). Shading in host binary32 (the RSX is not an IEEE-exact unit).
- `0x1808/0x1814/0x1824` begin/draw arrays/draw index array; register vertex attributes `SET_VERTEX_DATA{1F,2F,3F,4F,4UB}_M`
  and immediate-mode vertices (completing ATTR0 inside begin/end emits a vertex with a snapshot of the registers; RPCS3 `rsx_vertex_data.cpp`).
- `0x0b80–0x0bfc` VP upload per RPCS3 `nv4097.cpp:186–241` (load += (count+index%4)/4).
- VP interpreter: bit fields per RPCS3 `RSXVertexProgram.h`; ADD = src0+src2, scalar op reads src2; outputs o[0..15].
  CC/indexed constants/branching fail explicitly.
- FP interpreter: `RSXFragmentProgram.h`, halfword swap, inline constants, H/R aliasing (H(2k),H(2k+1) ⊂ R(k), packHalf2x16
  order — **hypothesis**), CC0/CC1 (set_cond, conditional write, KIL), precision clamp with RPCS3 exceptions, lockstep 2×2 quad
  (coarse derivatives, DDX/DDY, helper pixels). Output H0 or R0 according to SHADER_CONTROL bit 0x40.
- Rasterization: top-left rule, pixel centers, perspective-correct interpolation, flat shading, cull/front face,
  scissor, z-clip (clip min/max — **hypothesis**), alpha test, depth Z24S8 test/write, blend (GL factors/equations), color mask,
  A8R8G8B8 write including tile addressing. Clipping against w≤0 fails explicitly for now.
- Textures: A8R8G8B8 (LN), DXT1/3/5; LN/UN, wrap/mirror/clamp/border, nearest/bilinear, mip levels (LN = same pitch,
  others packed; RPCS3 `TextureUtils.cpp:626–704`), LOD from derivatives + bias + min/max LOD (CONTROL0), filters 3–6, remap
  (A,R,G,B; `color_utils.hpp`), border texels (bit 3 = 0 ⇒ border, RPCS3 `has_border=!border_type`), gamma sRGB decode
  per texel before the filter (**hypothesis**; RPCS3 decodes after sampling).
- Capture: the first 8 flips and then every `ANYPS3_FRAME_STRIDE`-th (default 60), cap of 256 files.

## Tests

`tests/test_rsx.py` (draw with the game's original VP, immediate vertices, CC, textures, mipmaps/derivatives), logs `local/ratchet/tests-rsx-2xx.log`.
