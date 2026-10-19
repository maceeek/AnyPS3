# Konstantní fragment shader během načítání ACIT

Datum: 2026-10-14. Cíl: původní menu ACIT a navigace gamepadem; zatím nesplněn.

## Ověřená překážka výkonu a změna

Běh 238 vykresluje střídavě QUAD s 921 600 pixely pozadí a QUAD s 17 161 pixely
načítací ikony (`native-bundle-238/stderr.log`, první šestnáct `native RSX draw`).
Jednoinstrukční původní konstantní MOV byl interpretován znovu pro každý quad,
včetně vytváření registrů a interpolace nepoužitých vstupů.

`anyps3/rsx_draw.py:709` rozpoznává výhradně shader s jedinou instrukcí MOV
z inline konstanty. Při prvním pokrytém quadu jej vyhodnotí stávajícím interpretem;
ten zachovává predikáty, masky, precision, H/R aliasing a explicitní chyby.
`rsx_draw.py:757` nevytváří pro tuto větev registry a interpolované vstupy pro každý
quad. Pokrytí, top-left pravidlo, culling, clipping, depth/alpha, blending, masky,
zápisy guest paměti a počty vykreslených pixelů zůstávají skutečně prováděné.
Jiné shadery pokračují obecnou cestou. Herní bajty se nemění.

Zdrojový hash změněného `rsx_draw.py` ze snapshotu sestavení 239:
`70ef9fff4439764ce625009fc90ce582fec28499ee0e3d1faf4b0c79fc825fec`.
Jde o jediný runtime soubor změněný proti 238; profil zůstal stejný.
Předchozí implementace pro diagnostické srovnání:
`local/ratchet/rsx_draw-before-239.py`.

## Ověřené testy a měření

- `tests/test_rsx.py:250`: nativní očekávání osmi přesných barevných pixelů,
  scissor, source-alpha blending, maskované modré složky, LESS depth, zachování
  stencil bajtu a odmítnutí dalších fragmentů přes depth/alpha. Stejný shader
  s úvodním NOP ověřuje obecnou cestu proti stejnému nezávislému očekávání.
- `python -m unittest tests.test_rsx.RsxDrawTests -q` → 6 testů, OK (10,481 s),
  `local/ratchet/tests-rsx-239.log`.
- `ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q` →
  **174 testů, OK (205,634 s), žádné skips**;
  `local/ratchet/tests-native-239.log`. Vulkan test tedy skutečně běžel.
- `PYTHONPATH=. python local/ratchet/tools/benchmark_constant_draw.py` →
  dvacet drawů 1024×512, 10 485 760 pixelů; stará větev 611 131 µs,
  nová 267 785 µs (~2,28× rychlejší). Oba programy ověřují přesný počet pixelů
  a každý výsledný bajt celé černé surface. Zdroje, binárky, logy a hashe:
  `local/ratchet/draw-benchmark-239/result.json`. Je to syntetické měření rendereru,
  nikoli běh hry nebo důkaz menu; rychlost celé hry z něj nelze odvodit.

## Nová sestava a další ověření

Nový `native-bundle-239` vznikl standardním `anyps3.build_game` s původním profilem,
firmwarem a `--max-steps 5000000000`, potom `c++ -std=c++17 -O2 -Wall -Wextra -Werror
-DANYPS3_VULKAN … -lvulkan`. Kompilace úspěšná v 201,298 s. Logy:
`local/ratchet/build-239.log`, `native-bundle-239/compile.log`.
Všechny zdrojové hashe a snapshot profilu odpovídají současným souborům.

Běh 238 zůstává srovnávací skutečný běh starého rendereru; 15:31:43 UTC měl
315 čtení, 14 947 223 načtených bajtů, draw 18 432 a 162 snímků, session `36487`
potvrzena živá. Překročil poslední čtení 235/236 (270 čtení, 13 187 843 bajtů).
**Ověřeno:** větší rozpočet posunul původní načítání za starý SPU limit.
Počty drawů a snímků nadále nejsou menu.

Původně plánovaný příkaz (neproveden):

```bash
ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-bundle-239 5000000000 > local/ratchet/run-239.log 2>&1
```

Řidší záznam snímků mění pouze diagnostický interval; guest draw ani flip se
nepřeskakují. Následně ověřit vlastní `verification.json`, PT_LOAD bajty,
hash artefaktů, stop a zachycené pixely. Toto herní ověření při zápisu ještě
neproběhlo. Virtuální ovladač ani fyzický gamepad a navigace menu nejsou ověřeny.

**Aktualizace po konci 238:** dosažen nový statický stop `0x962700`. Sestavení
239 nebylo spuštěno, má vlastní `NOT_RUN.txt`; bylo nahrazeno sestavením 240
se stejnou testovanou implementací a rozšířeným ověřeným profilem. První
skutečné herní ověření optimalizace tedy bude 240, viz
[záznam 240](2026-10-14-vtable-240.md).
