# Předání runtime po sestavení 237

Datum: 2026-10-14, Claude Opus 5.5. Práce přerušena na pokyn uživatele kvůli předání jinému AI agentovi.
**Cíl (původní menu ACIT + navigace gamepadem) není splněn.** Hra je ve fázi načítací obrazovky.

## Ověřený posun proti sestavení 198

1. Stop `0x1b8` odstraněn: linkování PRX import ref tabulek (atribut `0x2000`), `anyps3/prx.py`, `build_game.py`.
2. Nativní VSH trophy služba (NPTR `f`,`8`,`o`,`1`,`9`,`h`,`i`,`term`), instalace TRP na HDD, VSH události 299 přes
   sysutil frontu s odloženým doručením (`anyps3/sysutil_transport.py`, `events.py`).
3. SPU běží během `sys_timer_usleep` (3200 instr./µs) a během vsync čekání ve flipu (`services.py`, `rsx.py`).
4. PPU: `fsqrt`, single-precision aritmetika z plných double operandů (hypotéza implementace), `stfs` = Power ISA SINGLE,
   `lwbrx/lhbrx/stwbrx/sthbrx`, `sys_lwmutex_destroy`.
5. Statické cíle: vtables `0xc3c428`, `0xc3c378`, statusCb `0xcd3b38`, trophy handlery 299/310, jump tables; automatická
   ověřená detekce switch tabulek (`anyps3/jump_tables.py`, `build.json` → `verified_jump_tables`). Rozpočet překladu 1 000 000.
6. **RSX software renderer** (`anyps3/rsx_draw.py`): VP/FP interpret, textury (A8R8G8B8, DXT1/3/5, mipmapy, gamma,
   border texely), quad-lockstep derivace, depth/alpha/blend. Detail: [experiments/2026-10-14-rsx-software-draw.md](experiments/2026-10-14-rsx-software-draw.md).
7. První guest-renderovaný obraz: `local/ratchet/native-bundle-228/frame-000005.png` (ikona načítání). Běh 231/235:
   tisíce flipů s animovanou ikonou.

## Poslední skutečné běhy

| Sestavení | Výsledek |
| --- | --- |
| 235 (`-O2`) | stop `SPU execution step limit reached` (SPU 0x100, 500 M), 211 272 704 PPU kroků, flips=5998, draws=11988, 107 snímků |
| 236 (+SPU při vsync) | stejný stop ve stejném bodě (211 272 448 kroků, flips=5999) |
| 237 (+rozpočty 5 mld.) | **přerušen na pokyn uživatele** (SIGKILL, returncode −9) po ~4 min: načítání pokračovalo (221 čtení, draw 4608) |
| 227, 232 | kompilace selhala (varování `misleading-indentation`), ponecháno jako neúspěšné artefakty |
| 233, 234 | záměrně ukončeny, překonány rychlejšími sestaveními (viz `TERMINATED.txt`) |

## První další akce

Sestavit nové číslo (238+) a nechat doběhnout s rozpočty 5 mld. (`local/ratchet/tools/cycle.sh N`, kompiluje `-O2`).
Načítání je dlouhé (~SPU dekomprese, souborové čtení po ~45 KB); sledovat další skutečný stop (pravděpodobně nové RSX
metody/formáty, video/zvukové moduly nebo menu). Po dosažení menu ověřit vstup přes `cellPad` (hra již volá
`cellPadGetData`/`cellPadGetInfo2`).

## Reprodukce

```bash
local/ratchet/tools/cycle.sh 238          # build + c++ -O2 + run_native.py s rozpočty 5e9
python3 local/ratchet/tools/ppm2png.py local/ratchet/native-bundle-238/frames/frame-000005.ppm out.png
python3 local/ratchet/tools/rsx_vp_disasm.py local/ratchet/native-bundle-218/stderr.log   # disasm VP z diagnostiky
PYTHONPATH=. python local/ratchet/tools/add_vtable.py VTABLE SITE BUNDLE   # ověřená vtable do profilu
PYTHONPATH=. python local/ratchet/tools/add_jump_table.py BASE COUNT DISPATCH "evidence"
```

Při nepodporované RSX metodě stderr obsahuje snímek stavu (`RSX state`, `RSX vp[]`, `RSX vc[]`, `RSX fp[]`).
SIGTERM program ignoruje; běh ukončovat přes PID `kill -9` (pozor: `pkill -f` zabije i vlastní shell).

## Testy

Cílené sady prošly průběžně (logy `local/ratchet/tests-*-199…237.log`). Celá regresní sada s povinným Vulkanem:
`local/ratchet/tests-native-237.log`: **173 testů, 188,96 s, OK** (`ANYPS3_REQUIRE_VULKAN=1`, spuštěno po ukončení všech běhů).

## Omezení a hypotézy

Viz experiment záznamy: H/R aliasing pořadí, z-clip proti clip min/max, gamma před filtrem, single-precision operandy,
ořez w≤0 nepodporován, swizzled nekomprimované textury, stencil, MRT, zpass reporty (počítání pixelů zatím není).
Zvukový backend chybí. Profil `profiles/ratchet-acit.json` byl rozšířen (vtables, callbacks, jump tables, firmware seeds).
Git repozitář není k dispozici; žádný commit nevznikl.
