# Původ a dostupnost zdrojů

Ověřeno lokálně 2026-10-14. Cesty jsou relativní ke kořenu AnyPS3. Referenční zdroje během tohoto dokumentačního úkolu nebyly změněny.

## Referenční checkouty

| Zdroj | Skutečná cesta | HEAD | Veřejný původ |
| --- | --- | --- | --- |
| RPCS3 | `local/reference/rpcs3` | `ffba2bf09ef3980638f9327b25240d81414034ab` | [upstream](https://github.com/RPCS3/rpcs3) |
| PSL1GHT | `local/reference/PSL1GHT` | `f649a08fd536a9e27c08c7db2d93a2d7ee4c3bbe` | [upstream](https://github.com/ps3dev/PSL1GHT) |
| QEMU | `local/reference/qemu` | `8ee6c77176cfee03be0ec7665b6652b2f31f1531` | [upstream](https://gitlab.com/qemu-project/qemu) |
| envytools | `local/reference/local/reference/envytools` | `f102b82381f3f11cee113d16374c87091db039d9` | [upstream](https://github.com/envytools/envytools) |
| RPCS3 wiki | `local/reference/local/reference/rpcs3-wiki` | `5436d3de21410586daf23e4736208b3b43aa1ba6` | [upstream](https://github.com/RPCS3/rpcs3/wiki/Developer-Information) |

Jediný nalezený referenční `AGENTS.md` je QEMU; přečten spolu s jeho `docs/devel/code-provenance.rst`. Tyto zdroje používáme pro rešerši rozhraní, algoritmů a srovnání sémantiky. Aktuální řádky vždy znovu vyhledej při změně HEAD.

## Lokální originály a převody

Binární PDF byla stažena přes autorizovaný IDE terminal po selhání DNS v běžném sandbox shellu. Každé má `%PDF` signature, validní `pdfinfo`, zkontrolovanou titulní stranu/verzi/datum a SHA256. Plný původní URL, effective URL, čas a chybné pokusy: [manifest.json](../../local/reference/cell-docs/manifest.json). Metoda a měřená kvalita: [quality.json](../../local/reference/cell-docs/quality.json).

| Skutečný manuál a datum | Fyzické PDF strany | Originál | Prohledávatelný text |
| --- | ---: | --- | --- |
| Synergistic Processor Unit Instruction Set Architecture v1.2 (January 27, 2007) | 279 | [PDF](../../local/reference/cell-docs/pdf/spu-isa-v1.2.pdf) | [MD](../../local/reference/cell-docs/md/spu-isa-v1.2.md) |
| Cell Broadband Engine Architecture v1.02 (October 11, 2007) | 358 | [PDF](../../local/reference/cell-docs/pdf/cell-architecture-v1.02.pdf) | [MD](../../local/reference/cell-docs/md/cell-architecture-v1.02.md) |
| Cell Broadband Engine Registers v1.5 (April 2, 2007) | 357 | [PDF](../../local/reference/cell-docs/pdf/cell-registers-v1.5.pdf) | [MD](../../local/reference/cell-docs/md/cell-registers-v1.5.md) |
| Cell Broadband Engine Programming Handbook v1.1 (April 24, 2007) | 877 | [PDF](../../local/reference/cell-docs/pdf/cell-handbook-v1.1.pdf) | [MD](../../local/reference/cell-docs/md/cell-handbook-v1.1.md) |
| Cell Broadband Engine Programming Handbook Including the PowerXCell 8i Processor v1.11 (May 12, 2008) | 884 | [PDF](../../local/reference/cell-docs/pdf/cell-handbook-powerxcell-v1.11.pdf) | [MD](../../local/reference/cell-docs/md/cell-handbook-powerxcell-v1.11.md) |

Originály mají zachovaný copyright IBM/Sony/Toshiba. Jde o původní veřejné manuály na univerzitních/archivních mirrorech, nikoli aktuální vendor download server.

**Omezení:** Registers v1.5.1 je ve wiki uvedena bez URL; vyhledávání našlo index s titulkem Version 1.51 September 18, 2007, ale žádný originální soubor nebyl získán. V knihovně je výslovně v1.5 April 2, 2007, bez tvrzení, že jde o nejnovější vydání. Handbook v2.0 odkaz v RPCS3 wiki vede na soubor nazvaný `CBE_Tutorial_v2.0`; nestahovali jsme jej jako handbook. Skutečný Handbook v1.1 je titulní stranou potvrzený dokument April 24, 2007. PowerXCell v1.11 je doplněk, jeho odlišnosti nepřenášej automaticky na PS3.

SPU PDF má 279 fyzických stran, z nichž poslední je dotazník; patička uvádí 278. Převody označují fyzické strany od 1. Součet knihovny: 2755 fyzických stran. Všechny extrakce mají shodný počet stran, žádné prázdné stránky, NUL ani U+FFFD; krátké titulní/přechodové strany jsou zdokumentované. Ručně porovnány titulní strany, obsahy, vzorky instrukcí/registrů a konec dokumentů; renderované kontrolní vzorky SPU248, Registers98 a Handbook446 potvrzují čitelnost textu a limity diagramů/tabulek.

## Obnovení a hledání

`python3 local/reference/cell-docs/download.py` stáhne chybějící PDF a zachová existující; potřebuje síť a `pdfinfo`. `python3 local/reference/cell-docs/convert.py` převádí stažené dokumenty pomocí `pdftotext -layout -enc UTF-8`, ověří page count a hash originálu. Nový převod označí manuální review jako pending: znovu zkontroluj vzorky. Originály nemaž.

Dokumentační ověření nenahrazuje spuštění hry. Další runtime předání: [HANDOFF.md](HANDOFF.md).
