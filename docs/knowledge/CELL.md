# CELL: orientace a paměťové hranice

## Ověřené veřejné zdroje

[Cell Architecture v1.02](../../local/reference/cell-docs/pdf/cell-architecture-v1.02.pdf), fyzické PDF strany 27–31, popisuje PPE, SPE, lokální storage a MFC. [Handbook v1.1](../../local/reference/cell-docs/pdf/cell-handbook-v1.1.pdf), strany 63–64, popisuje konkrétní SPE jako SPU + MFC a lokální paměť 256 KB. Odděluj obecnou ISA od implementační velikosti LS; SPU ISA dovoluje jiné implementace.

**Fakt (veřejná architektura):** SPU load/store pracuje v lokální paměti. Přenos do/z hlavní paměti zprostředkovává MFC. Jeho SPU channel interface není totéž co proxy MMIO interface PPE. Architecture v1.02, PDF strany 29–31; [textový převod](../../local/reference/cell-docs/md/cell-architecture-v1.02.md#pdf-page-29).

**Fakt (veřejná architektura):** `get` kopíruje z hlavní paměti do LS; `put` z LS do hlavní paměti. Architecture v1.02, PDF strany 64–66, sekce 7.5/7.6. Fence/barrier ani dokončení tag group nenahrazuj automatickým dokončením každého přenosu: pravidla pořadí čti v sekci 7.9, zvlášť PDF strany 73–76.

**Fakt (Handbook v1.1):** parametry MFC se zapisují před command opcode; zápis opcode enqueueuje úplný příkaz. PDF strana 452, sekce 17.2.1. Při ladění zaznamenej LSA, EA, size, tag, command, queue capacity a důkaz dokončení, nejen změněné bajty.

## Praktická mapa zdrojů

- SPU instrukce: [SPU.md](SPU.md), ISA v1.2.
- Channel ID a stav: `local/reference/rpcs3/rpcs3/Emu/Cell/SPUThread.h:61`, viz referenční commit v [SOURCES.md](SOURCES.md).
- MMIO registry: [Registers v1.5](../../local/reference/cell-docs/pdf/cell-registers-v1.5.pdf), hlavně rozcestník strany 3–10; význam a bity ověř v originálu.
- Veřejné systémové rozhraní PSL1GHT: `local/reference/PSL1GHT/ppu/include/lv2/spu.h` a `ppu/include/spurs/`.

## Otázky pro AnyPS3

Zatím zde není nově provedený runtime test. Pro konkrétní čekající SPU ověř PC, channel, počet položek, MFC tag mask/update/status, LS a guest scheduler. Hypotézu „SPU visí“ odliš od správného blokujícího read channel a od neimplementovaného wakeupu. PS3 přístup k dostupným SPE, LV2 syscallům a SPURS ověřuj zvlášť; veřejný Cell manuál nepopisuje všechny služby PS3 firmware.

PowerXCell Handbook v1.11 ponechán jako doplněk; změněné double precision instrukce a DDR2 specifika nejsou automaticky vlastnostmi PS3 CELL.
