# SPU: ISA, kanály a kontrola sémantiky

Primární reference je [SPU ISA v1.2, 27 January 2007](../../local/reference/cell-docs/pdf/spu-isa-v1.2.pdf), IBM/Sony/Toshiba, 279 fyzických PDF stran. Dokument v patičce uvádí 278 stran; fyzická strana 279 je dotazník. Pro citace používej `PDF page N`, ne celkový počet z patičky.

## Ověřená fakta z manuálu

| Téma | Přesný význam | PDF strana |
| --- | --- | --- |
| Registry | 128 GPR, každý 128 bitů; floating point sémantika se musí číst po instrukcích | 25 |
| Preferred slot | první čtyři bajty registru pro skalární hodnoty/adresy; ne náhodně zvolený host lane | 28 |
| Load/store | aligned 16-byte quadwords, nejnižší 4 adresové bity se ignorují; LSLR dále maskuje efektivní adresu | 31 |
| `rdch` | čeká na data; neplatný readable channel vede k zastavení podle ISA | 248 |
| `rchcnt` | vrací capacity do preferred slot, ostatní bajty nula; neimplementovaný kanál má capacity nula | 249 |
| `wrch` | čeká na volnou kapacitu; 32bitový channel používá preferred slot | 250 |

Tyto závěry jsou **fakta veřejné ISA**, nikoli výsledky testů AnyPS3. [Prohledávatelný převod](../../local/reference/cell-docs/md/spu-isa-v1.2.md) zachovává text pseudokódu, nikoli přesné kreslené hranice bitů. Pro opcode a rozsahy polí otevři PDF. Zápis `2^32` může po extrakci ztratit superscript; text `232` není desetinné číslo 232.

## Referenční implementace

RPCS3 checkpoint v [SOURCES.md](SOURCES.md): `local/reference/rpcs3/rpcs3/Emu/Cell/SPUOpcodes.h` pro pole a opcode; `SPUInterpreter.cpp` pro jednotlivé operace; `SPUThread.cpp` pro kanály a MFC; `SPUThread.h:41` a `:61` pro SPU/MFC channel enumerace. Hledej konkrétní funkci přes `rg`, před použitím zaznamenej její aktuální řádek a commit. Chování referenčního emulátoru používej jako implementační srovnání, manuál jako architekturní zdroj.

## Co zapisovat při opravě

Vstupní instrukční word, dekódovaný mnemonic/pole, PC, zdrojové registry, požadovaný výstup a stav kanálu/DMA. Při aliasingu zdroj/cíl ověř, zda se původní operandy čtou dříve než zápis. U shuffle/shift/rotate zapiš byte order a hraniční počty posunu. U FP odděl SPU od PPU a PowerXCell; obecné host IEEE chování nemusí odpovídat cílové instrukci.

Aktivní runtime otázky patří do [CURRENT.md](CURRENT.md). Vznik tohoto přehledu nemění SPU runtime ani testy.
