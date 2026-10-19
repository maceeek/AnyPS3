# RSX draw: softwarový renderer do guest surface (sestavení 216–233)

Datum: 2026-10-14, Claude Opus 5.5. Cíl zůstává původní menu ACIT a navigace gamepadem.

## Ověřené (runtime)

- Po odstranění PPU/trophy stopů hra posílá skutečné RSX draw příkazy (FIFO put `0x17c096c`, bundle 216+).
- Bundle 220: první skutečný draw hry: QUAD 1280×720 (921 600 px), FP `MOV H0,{0,0,0,0}`.
- Bundle 228: draw #2 vykreslil 17 161 px z původní textury (960×704 LN UN A8R8G8B8 a další); snímek 5 obsahuje ikonu načítání
  (planeta) vlevo dole. Bundle 231: 256 flipů s animovanou ikonou (načítací obrazovka), stop jen na stropu zachytávání.
- Diagnostika (bundle 218/219): VP 14 instrukcí (2D převod pixel→clip: `c[6]=(640,360,640,360)`), stride 28, pozice float4,
  UV float2 (attr1), barva UB4 (attr9), viewport offset (640,360) scale (640,−360), surface A8R8G8B8 lineární pitch 5120.

## Implementace (`anyps3/rsx_draw.py`, `anyps3/rsx.py`)

- Guest paměť zůstává zdrojem pravdy (jako clear/scanout). Stínování v host binary32 (RSX není IEEE-přesná jednotka).
- `0x1808/0x1814/0x1824` begin/draw arrays/draw index array; registrové vertex atributy `SET_VERTEX_DATA{1F,2F,3F,4F,4UB}_M`
  a immediate-mode vertexy (dokončení ATTR0 uvnitř begin/end emituje vertex se snímkem registrů; RPCS3 `rsx_vertex_data.cpp`).
- `0x0b80–0x0bfc` upload VP podle RPCS3 `nv4097.cpp:186–241` (load += (count+index%4)/4).
- VP interpret: bitová pole RPCS3 `RSXVertexProgram.h`; ADD = src0+src2, skalární op čte src2; výstupy o[0..15].
  CC/indexované konstanty/větvení explicitně selžou.
- FP interpret: `RSXFragmentProgram.h`, halfword swap, inline konstanty, H/R aliasing (H(2k),H(2k+1) ⊂ R(k), packHalf2x16
  pořadí — **hypotéza**), CC0/CC1 (set_cond, podmíněný zápis, KIL), precision clamp s výjimkami RPCS3, lockstep 2×2 quad
  (hrubé derivace, DDX/DDY, pomocné pixely). Výstup H0 nebo R0 dle SHADER_CONTROL bit 0x40.
- Rasterizace: top-left pravidlo, středy pixelů, perspektivně korektní interpolace, flat shading, cull/front face,
  scissor, z-clip (clip min/max — **hypotéza**), alpha test, depth Z24S8 test/zápis, blend (GL faktory/rovnice), color mask,
  zápis A8R8G8B8 včetně tile adresace. Ořez proti w≤0 zatím explicitně selže.
- Textury: A8R8G8B8 (LN), DXT1/3/5; LN/UN, wrap/mirror/clamp/border, nearest/bilinear, mip úrovně (LN = stejný pitch,
  ostatní packed; RPCS3 `TextureUtils.cpp:626–704`), LOD z derivací + bias + min/max LOD (CONTROL0), filtry 3–6, remap
  (A,R,G,B; `color_utils.hpp`), border texely (bit 3 = 0 ⇒ okraj, RPCS3 `has_border=!border_type`), gamma sRGB dekód
  per texel před filtrem (**hypotéza**; RPCS3 dekóduje po vzorkování).
- Zachytávání: prvních 8 flipů a pak každý `ANYPS3_FRAME_STRIDE`-tý (výchozí 60), strop 256 souborů.

## Testy

`tests/test_rsx.py` (draw s původním VP hry, immediate vertexy, CC, textury, mipmapy/derivace), logy `local/ratchet/tests-rsx-2xx.log`.
