# Ověření dokumentace a lokální knihovny

Datum: 2026-10-14. Rozsah: pouze dokumentační knihovna a báze znalostí; žádná runtime změna ani nové spuštění hry tímto dokumentačním agentem.

## Výsledek

Pět skutečných originálních PDF zachováno v `local/reference/cell-docs/pdf/`; pět převodů v `md/`. SPU ISA v1.2 má 279, Architecture v1.02 358, Registers v1.5 357, Handbook v1.1 877 a PowerXCell Handbook v1.11 884 fyzických stran: celkem 2755.

- U všech pěti souborů ověřena `%PDF` signature, validní `pdfinfo`, titul/verze/datum z titulní strany a původní copyright.
- SHA256 originálu i převodu znovu spočítán a porovnán s [manifestem](../../local/reference/cell-docs/manifest.json).
- Počet stran `pdftotext` odpovídá `pdfinfo`; MD obsahuje přesnou nepřerušenou posloupnost `PDF page 1` až `PDF page N`.
- Žádné prázdné extrahované stránky, NUL nebo U+FFFD; krátké přechodové/titulní strany zaznamenány v [quality.json](../../local/reference/cell-docs/quality.json).
- Ručně kontrolovány titulní strany, obsahy/navigace, reprezentativní instrukční/register/chapter stránky a konce dokumentů. Graficky porovnány SPU248, Registers98 a Handbook446; bitové diagramy a tabulky se musí interpretovat z PDF, superscripty se mohou při extrakci zploštit.
- Syntax downloaderu a converteru ověřena Python AST parserem. Konverze skutečně proběhla, nikoli pouze připravena.
- Tematické odkazy a line reference ověřeny v aktuálních referenčních checkoutech; revize jsou v [SOURCES.md](SOURCES.md).

## Výslovná omezení

Registers v1.5.1 nebyla získána; knihovna uchovává explicitně označenou v1.5. Odkaz wiki na tutorial v2.0 není vydáván za stažený Handbook v2.0. PowerXCell supplement je oddělen od PS3 CELL. Textový převod nereprodukuje obrázky, diagramy ani přesnou strukturu tabulek. Původní copyright zůstává zachován.

CURRENT používá lokálně přečtený verification.json a test log sestavy198; hlubší diagnózu TOC předal koordinátor a je označena odvozeně. Menu a navigace gamepadem jsou stále neověřené. Runtime předání spravuje koordinátor. Konečná kontrola všech lokálních odkazů prošla včetně jeho existujícího runtime-handoff-198.md; všech deset PDF/MD hashů znovu odpovídá manifestu.
