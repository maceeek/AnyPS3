# Milník: nativní načítání a SPU dekomprese

Ověřeno 2026-10-14, Ratchet & Clank: A Crack in Time, BCES00511 01.00,
sestavení `native-bundle-190`. **Menu ani herní geometrie zatím nejsou
vykreslené.** Dokončený celek je cesta od skutečného čtení archivu přes
původní SPU dekompresi až po DMA výsledek a oznámení hlavnímu procesoru.

## Ověřený výsledek

Původní FIOS otevře `packed/game/game.psarc`, načte index a data a vytvoří
skutečné soubory systémové cache. Staticky přeložený původní SPU program
rozbalí všechny tři bloky `game.toc` a přes DMA je předá do hlavní paměti.
Mailbox události na portu 17 umožní PPU převzít výsledky. Hra následně
otevře také `global_cached.psarc` a `global_uncached.psarc`.

Diagnostický pozorovatel po třetí dokončené SPU události zkopíroval
136 620 bajtů z guest adresy `0x409acb40`. Nezměnil herní paměť ani kód.
Výstup se **bajt po bajtu shoduje** s nezávislým rozbalením PSARC přes zlib:

```text
SHA-256 game.toc:
b540c6930178ed93d1850c7210069dd612df73a17a5cde49f42ad9693630c08d
```

Sestavení 190 také překročilo dřívější chybu DMA na rozsahu
`0x409fee80 + 0x4000`, který protíná hranici sousedních mapování.
Dosáhlo 110 610 176 PPU kroků a skončilo na diagnostickém limitu
10 milionů instrukcí prvního SPU. Limit zatím chrání experimentální běh;
pro další načítání bude nutné jej upravit a pokračovat v diagnostice.

Od předchozího commitu přibylo také dokončení inicializace herních dat,
ukončení a join PPU vlákna, lehké podmínkové proměnné, vlastní Vulkan
clear dlaždicových ARGB8/Z24S8 ploch a headless výměny bufferů. Čtyři
zachycené snímky 1280 × 720 jsou černé; počty clearů a flipů nejsou důkazem
menu. Statický překlad doplňuje ověřené původní callbacky a SPU overlaye,
nikoli patche herního kódu nebo emulační backend RPCS3.

## Ochrany a rozsah

- DMA předem ověří celý rozsah, oprávnění a zákaz zápisu do kódu.
  Díra či nepovolený sousední blok nezpůsobí částečný zápis. Úspěšné zápisy
  aktualizují rezervace i přes aliasy paměti.
- Plná fronta událostí pozastaví SPU a zachová zprávu pro opakování.
  Odeslání s odpovědí vrací skutečný stav portu/fronty; události se nevymýšlejí.
- Filesystém čte a zapisuje skutečné soubory. Symlinky a `..` se odmítají.
  `ANYPS3_DISC_CASEFOLD=1` je explicitní adaptace velkých ISO9660 názvů
  našeho extraktu; netýká se HDD/cache a odmítá nejednoznačné názvy.
- PPU/SPU vykonávají předem přeložený kód. SPU ověřuje přesné instrukční
  slovo známého obrazu/overlaye. Neznámé instrukce a služby stále selhávají.
- Přerušení SPU jsou dosud vypnutá; jejich povolení a izolované skupiny
  nejsou implementované. Vulkan zatím nekreslí herní shadery ani geometrii.
  SDL2 vstup je samostatně testovaný; navigace skutečným menu ověřena není.

## Reprodukce

Kompletní testy bez herních dat:

```bash
ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q
```

Pro herní běh jsou potřeba původní lokální vstupy: analytický EBOOT,
ověřený firmware 3.01, lokální klíčový soubor a kompletní extrakt disku.
Tyto soubory nejsou součástí Gitu. Výstupní adresář musí být nový:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json \
  --profile profiles/ratchet-acit.json --max-steps 200000000 \
  -o local/ratchet/loading-milestone
c++ -std=c++17 -Og -Wall -Wextra -Werror -DANYPS3_VULKAN \
  local/ratchet/loading-milestone/program.cpp -lvulkan \
  -o local/ratchet/loading-milestone/program
mkdir -p local/ratchet/loading-milestone/frames local/ratchet/cache local/ratchet/hdd0
ANYPS3_DISC_ROOT=local/ratchet/game-complete \
ANYPS3_DISC_CASEFOLD=1 ANYPS3_CACHE_ROOT=local/ratchet/cache \
ANYPS3_HDD_ROOT=local/ratchet/hdd0 \
ANYPS3_FRAME_DIR=local/ratchet/loading-milestone/frames \
  local/ratchet/loading-milestone/program \
  local/ratchet/loading-milestone/program.elf
```

Ověřovací běh používal Lavapipe a Khronos validation včetně synchronizace.
Podrobný SPU trace lze zapnout `ANYPS3_SPU_TRACE=1`; výchozí běh loguje
DMA, události a stop bez každé jednotlivé instrukce.

## Lokální důkazy

- `local/ratchet/native-bundle-190/`: sestavení, snapshot profilu, logy,
  čtyři snímky, `verification.json` a hashe zdrojů při sestavení.
- `decompression-verification.json` ve stejném adresáři: shoda celého
  `game.toc`, hash a identita diagnostického programu.
- `diagnostic.cpp` ve stejném adresáři: čtecí pozorovatel přidaný pouze
  pro porovnání dat. Běžný runtime žádný předem rozbalený výstup nedostává.
- `local/ratchet/tests-native-190.log`: kompletní regresní sada.
- [Podrobný postup](menu-progress.md) a [Vulkan ověření](vulkan-rendering.md).

Další práce začíná rozpočtem běhu SPU při čtení globálních archivů.
Potom je potřeba sledovat skutečné požadavky hry na další služby a RSX
příkazy, doplnit kreslení a teprve následně ověřit menu i gamepad.
