# PPU: PowerPC, ABI a HLE hranice

**Fakt (veřejná architektura):** CELL PPE je 64bitová PowerPC implementace s 32bitovým i 64bitovým mode. V 32bitovém mode se některé interpretace adres a branch testů mění; neznamená to zúžení všech GPR na 32 bitů. [Cell Architecture v1.02](../../local/reference/cell-docs/pdf/cell-architecture-v1.02.pdf), fyzická PDF strana 47, sekce 4. Dokument odkazuje na PowerPC Books I–III, které tato knihovna zatím nestáhla.

**Fakt (referenční implementace):** RPCS3 `local/reference/rpcs3/rpcs3/Emu/Cell/PPUThread.h:168` eviduje 32 GPR, 32 FPR a 32 vector registrů. Jde o stav RPCS3 při revizi v [SOURCES.md](SOURCES.md), nikoli úplnou normativní specifikaci ISA.

## Mapa lokálních zdrojů

| Zdroj | Účel a hranice |
| --- | --- |
| [Handbook v1.1](../../local/reference/cell-docs/pdf/cell-handbook-v1.1.pdf), kapitola 2, PDF od strany 49 | PPE přehled; Appendix A nabízí instrukční přehled |
| `local/reference/rpcs3/rpcs3/Emu/Cell/PPUOpcodes.h` | dekódování instrukcí |
| `local/reference/rpcs3/rpcs3/Emu/Cell/PPUInterpreter.cpp` | reference sémantiky konkrétních instrukcí |
| `local/reference/rpcs3/rpcs3/Emu/Cell/PPUThread.h` | registrový a vláknový stav |
| `local/reference/qemu/target/ppc/translate.c`, `int_helper.c` | doplňková PowerPC implementační reference; porovnej konkrétní cílový CPU/mode |
| `local/reference/PSL1GHT/ppu/include/lv2/thread.h`, `sys/thread.h` | veřejné PS3 thread rozhraní |
| `local/reference/PSL1GHT/ppu/include/io/pad.h` | veřejné gamepad rozhraní; pro menu nutný guest runtime důkaz |

QEMU čti v souladu s jeho `AGENTS.md` a `docs/devel/code-provenance.rst`: zde probíhá výzkum rozhraní/algoritmů, bez upstream příspěvků a bez editace reference.

## OPD, callback a skutečný stop

Před přidáním statického callback cíle ověř původní OPD bajty, entry, TOC a identitu ELF/PRX obrazu. Adresa descriptoru a adresa kódu jsou různé údaje; přebírej je z konkrétního obrazu, nikoli z jiné verze firmware. V záznamu uveď zdroj adres, rozsah segmentu, disassembly a runtime caller. Toto je pracovní pravidlo AnyPS3, ne tvrzení, že byl daný callback otestován.

HLE návrat a guest reakce eviduj odděleně: známé `NOT_IMPLEMENTED`/error může být guestem zpracované, ale samo nepotvrzuje dokončení služby. Při `unmapped read` zaznamenej fault address, PC, LR, GPR a call chain; odliš null + offset od poškozeného pointeru podle skutečných registrů. Příčinu neurčuj pouze z nízké fault adresy.

Přesný aktuální stop ACIT spravuje [CURRENT.md](CURRENT.md) s odkazem na skutečný log.

## PPU floating divide a následný herní clamp

**Ověřené nativními testy 245:** disabled-trap FP divide a add/subtract
zpracují infinities, ZX a invalid VXZDZ/VXIDI/VXISI se správnou klasifikací
a sticky FPSCR. Původní posloupnost `0x69f2b0..0x69f2c0` po 154/+0
vytvoří +Infinity, původním FSEL omezí hodnotu na 0,75 a uloží ji přes guest
big-endian STFS. Změna je v runtime, původní herní instrukce zachovány.
Je to test ISA posloupnosti, nikoli menu důkaz; skutečný 245 při zápisu
ještě načítá. Podrobné zdroje, rozsah a omezení stažení původního Book I,
regrese a runtime: [FP experiment 245](experiments/2026-10-14-fp-zero-divide-245.md).
