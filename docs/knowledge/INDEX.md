# Projektová báze znalostí AnyPS3

Toto je verzovatelný vstupní bod pro další model, nový běh i člověka. Stav cíle a runtime důkazy čti v [CURRENT.md](CURRENT.md), pravidla zápisu v [WORKFLOW.md](WORKFLOW.md), předání v [HANDOFF.md](HANDOFF.md). Záznam v dokumentaci sám o sobě neověřuje funkci emulátoru.

Nejnovější stav a předání: [CURRENT.md](CURRENT.md) a [HANDOFF.md](HANDOFF.md)
(2026-10-19: build380, původní úroveň `great_clock_a`15,02 FPS, pohyb virtuálním padem,
ukončeno pro předání na pokyn uživatele;60 FPS nedosaženo, fyzický gamepad neověřen).
[Výsledky379–380 a další postup](experiments/2026-10-19-performance-379.md),
[webová rešerše skutečných PS3 recomp projektů](experiments/2026-10-19-web-ps3-recomp.md).
**2026-10-19 odpoledne: vlna FAST (cíl 60 FPS v úrovni, režim hry 0 ověřen, výchozí 14,25 FPS):**
[záznam FAST](experiments/2026-10-19-fast-wave.md).

## Orientace

| Oblast | Přehled | Co hledat |
| --- | --- | --- |
| CELL a MFC | [CELL.md](CELL.md) | lokální vs hlavní paměť, DMA, synchronizace, veřejné manuály |
| SPU | [SPU.md](SPU.md) | ISA, kanály, mailbox, tag completion, instrukční reference |
| PPU | [PPU.md](PPU.md) | registry, ABI/OPD, PowerPC reference, HLE hranice |
| RSX | [RSX.md](RSX.md) | FIFO, metody, draw, shadery, surface/flip, NV40/RSX rozdíly |
| Původ zdrojů | [SOURCES.md](SOURCES.md) | skutečné cesty, revize, manifest manuálů, omezení |

## Rychlý začátek dalšího běhu

1. Přečti kořenový `AGENTS.md`, tuto stránku a CURRENT. Respektuj poslední instrukce uživatele a skutečný stav pracovního stromu.
2. Otevři [../menu-progress.md](../menu-progress.md) pro aktuální přehled ACIT a odkazované logy. Stárnoucí informaci ověř v posledním skutečném běhu; nepovažuj číslo běhu z jiného modelu za automaticky aktuální.
3. Vyber jednu konkrétní otevřenou otázku. Před změnou napiš hypotézu a důkaz, který ji potvrdí nebo vyvrátí.
4. Po ověření aktualizuj CURRENT a příslušnou tematickou stránku nebo samostatný záznam. Zapiš příkaz, zdrojový stav, profil a cesty artefaktů.

## Prohledávání

Z kořene projektu: `rg -n 'tag|mailbox|DMA' docs/knowledge local/reference/cell-docs/md`.
Pro přesnou instrukci použij `rg -n 'Shuffle Bytes|Read Channel' local/reference/cell-docs/md`.
MD manuálů má nadpis `PDF page N` pro každou fyzickou stránku; grafické tabulky a bitové diagramy ověř v PDF.

PDF a odvozené MD patří do lokální knihovny `local/reference/cell-docs/`; jejich existenci a verzi potvrzuje manifest a kontrola titulní strany. Lokální referenční klony nejsou automaticky součástí verzované báze znalostí.
