# Documentation and local library validation

Date: 2026-10-14. Scope: only the documentation library and the knowledge base; no runtime change and no new game run by this documentation agent.

## Result

Five actual original PDFs are preserved in `local/reference/cell-docs/pdf/`; five conversions in `md/`. SPU ISA v1.2 has 279, Architecture v1.02 358, Registers v1.5 357, Handbook v1.1 877 and PowerXCell Handbook v1.11 884 physical pages: 2755 in total.

- For all five files, the `%PDF` signature, a valid `pdfinfo`, the title/version/date from the cover page, and the original copyright were verified.
- The SHA256 of the original and of the conversion was recomputed and compared with the [manifest](../../local/reference/cell-docs/manifest.json).
- The `pdftotext` page count matches `pdfinfo`; the MD contains the exact uninterrupted sequence `PDF page 1` through `PDF page N`.
- No empty extracted pages, NUL or U+FFFD; short transitional/title pages are recorded in [quality.json](../../local/reference/cell-docs/quality.json).
- Cover pages, tables of contents/navigation, representative instruction/register/chapter pages and document endings were checked manually. SPU248, Registers98 and Handbook446 were compared graphically; bit diagrams and tables must be interpreted from the PDF, and superscripts may be flattened during extraction.
- The syntax of the downloader and converter was verified with the Python AST parser. The conversion actually ran; it was not merely prepared.
- Topic links and line references were verified in the current reference checkouts; the revisions are in [SOURCES.md](SOURCES.md).

## Explicit limitations

Registers v1.5.1 was not obtained; the library keeps the explicitly labelled v1.5. The wiki link to tutorial v2.0 is not passed off as the downloaded Handbook v2.0. The PowerXCell supplement is kept separate from PS3 CELL. The text conversion does not reproduce images, diagrams or the exact table structure. The original copyright is preserved.

CURRENT uses the locally read verification.json and the test log of build 198; the deeper TOC diagnosis was handed over by the coordinator and is marked as derived. The menu and gamepad navigation are still unverified. The runtime handoff is maintained by the coordinator. The final check of all local links passed, including its existing runtime-handoff-198.md; all ten PDF/MD hashes again match the manifest.
