# Origin and availability of sources

Verified locally 2026-10-14. Paths are relative to the AnyPS3 root. The reference sources were not modified during this documentation task.

## Reference checkouts

| Source | Actual path | HEAD | Public origin |
| --- | --- | --- | --- |
| RPCS3 | `local/reference/rpcs3` | `ffba2bf09ef3980638f9327b25240d81414034ab` | [upstream](https://github.com/RPCS3/rpcs3) |
| PSL1GHT | `local/reference/PSL1GHT` | `f649a08fd536a9e27c08c7db2d93a2d7ee4c3bbe` | [upstream](https://github.com/ps3dev/PSL1GHT) |
| QEMU | `local/reference/qemu` | `8ee6c77176cfee03be0ec7665b6652b2f31f1531` | [upstream](https://gitlab.com/qemu-project/qemu) |
| envytools | `local/reference/local/reference/envytools` | `f102b82381f3f11cee113d16374c87091db039d9` | [upstream](https://github.com/envytools/envytools) |
| RPCS3 wiki | `local/reference/local/reference/rpcs3-wiki` | `5436d3de21410586daf23e4736208b3b43aa1ba6` | [upstream](https://github.com/RPCS3/rpcs3/wiki/Developer-Information) |

The only reference `AGENTS.md` found is QEMU's; it was read together with its `docs/devel/code-provenance.rst`. We use these sources for researching interfaces and algorithms and for comparing semantics. Always look up the current lines again when HEAD changes.

## Local originals and conversions

The binary PDFs were downloaded through the authorized IDE terminal after DNS failed in the regular sandbox shell. Each has a `%PDF` signature, a valid `pdfinfo`, a checked title page/version/date and a SHA256. The full original URL, effective URL, time and failed attempts: [manifest.json](../../local/reference/cell-docs/manifest.json). The method and measured quality: [quality.json](../../local/reference/cell-docs/quality.json).

| Actual manual and date | Physical PDF pages | Original | Searchable text |
| --- | ---: | --- | --- |
| Synergistic Processor Unit Instruction Set Architecture v1.2 (January 27, 2007) | 279 | [PDF](../../local/reference/cell-docs/pdf/spu-isa-v1.2.pdf) | [MD](../../local/reference/cell-docs/md/spu-isa-v1.2.md) |
| Cell Broadband Engine Architecture v1.02 (October 11, 2007) | 358 | [PDF](../../local/reference/cell-docs/pdf/cell-architecture-v1.02.pdf) | [MD](../../local/reference/cell-docs/md/cell-architecture-v1.02.md) |
| Cell Broadband Engine Registers v1.5 (April 2, 2007) | 357 | [PDF](../../local/reference/cell-docs/pdf/cell-registers-v1.5.pdf) | [MD](../../local/reference/cell-docs/md/cell-registers-v1.5.md) |
| Cell Broadband Engine Programming Handbook v1.1 (April 24, 2007) | 877 | [PDF](../../local/reference/cell-docs/pdf/cell-handbook-v1.1.pdf) | [MD](../../local/reference/cell-docs/md/cell-handbook-v1.1.md) |
| Cell Broadband Engine Programming Handbook Including the PowerXCell 8i Processor v1.11 (May 12, 2008) | 884 | [PDF](../../local/reference/cell-docs/pdf/cell-handbook-powerxcell-v1.11.pdf) | [MD](../../local/reference/cell-docs/md/cell-handbook-powerxcell-v1.11.md) |

The originals retain the IBM/Sony/Toshiba copyright. These are the original public manuals on university/archive mirrors, not the current vendor download server.

**Limitation:** Registers v1.5.1 is listed in the wiki without a URL; a search found an index with the title Version 1.51 September 18, 2007, but no original file was obtained. The library explicitly holds v1.5 April 2, 2007, without claiming that it is the latest edition. The Handbook v2.0 link in the RPCS3 wiki leads to a file named `CBE_Tutorial_v2.0`; we did not download it as a handbook. The actual Handbook v1.1 is a document confirmed by its title page as April 24, 2007. PowerXCell v1.11 is a supplement; do not automatically carry its differences over to the PS3.

The SPU PDF has 279 physical pages, the last of which is a questionnaire; the footer states 278. The conversions number physical pages from 1. Library total: 2755 physical pages. All extractions have a matching page count, no empty pages, NUL or U+FFFD; short title/transition pages are documented. Title pages, tables of contents, samples of instructions/registers and the ends of the documents were compared manually; rendered check samples SPU248, Registers98 and Handbook446 confirm the legibility of the text and the limits of the diagrams/tables.

## Restoring and searching

`python3 local/reference/cell-docs/download.py` downloads the missing PDFs and keeps the existing ones; it needs network access and `pdfinfo`. `python3 local/reference/cell-docs/convert.py` converts the downloaded documents using `pdftotext -layout -enc UTF-8`, verifies the page count and the hash of the original. A new conversion marks the manual review as pending: check the samples again. Do not delete the originals.

A documentation check does not replace running the game. The next runtime handoff: [HANDOFF.md](HANDOFF.md).
