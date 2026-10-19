# RSX: FIFO, draw a rozdíl od prezentace

## Ověřené implementační mapování

Lokální RPCS3 a envytools obsahují navzájem porovnatelné metody. Tabulka níže dokládá pojmenování a adresu v těchto checkoutů; sama neprokazuje veškerou RSX sémantiku.

| Byte offset | RPCS3 | envytools |
| --- | --- | --- |
| `0x1808` | `NV4097_SET_BEGIN_END` | `VERTEX_BEGIN_END` |
| `0x1814` | `NV4097_DRAW_ARRAYS` | `VB_VERTEX_BATCH` |
| `0x1824` | `NV4097_DRAW_INDEX_ARRAY` | `VB_INDEX_BATCH` |

Zdroj: `local/reference/rpcs3/rpcs3/Emu/RSX/gcm_enums.h:674`, `:677`, `:681`; `local/reference/local/reference/envytools/rnndb/graph/nv30-40_3d.xml:471`, `:477`, `:487`, revize v [SOURCES.md](SOURCES.md). **Fakt (RPCS3):** jeho enum používá byte offset `>> 2`, například `gcm_enums.h:674`. Nesrovnávej tedy interní word index s envytools byte offsetem bez přepočtu.

## Zdroje pro konkrétní další krok

- `local/reference/PSL1GHT/ppu/librsx/commands_impl.h:35` a další funkce ukazují emission příkazů do bufferu; `:119` začíná konfigurace DMA surface. Veřejný producer je užitečný k porovnání packetů s guest FIFO.
- `local/reference/PSL1GHT/ppu/include/rsx/commands_inc.h` popisuje příkazy, `nv40.h` obsahuje symbolické offsety, `gcm_sys.h` veřejné GCM rozhraní.
- `local/reference/rpcs3/rpcs3/Emu/RSX/rsx_methods.cpp:1627` binduje begin/end, `:1629` draw arrays a `:1630` draw index array; tento dispatch není sám implementací shaderů/renderingu.
- `local/reference/local/reference/envytools/rnndb/graph/nv30-40_3d.xml` popisuje NV30/NV40 packet pole. Offsety, varianty a neznámá pole ověř; NVIDIA NV40/G70 dokumentace není automaticky kompletní RSX specifikace.
- [RPCS3 wiki Developer Information](../../local/reference/local/reference/rpcs3-wiki/Developer-Information.md) uvádí RSX FIFO wiki jako neúplný zdroj: veřejný [RSXFIFOCommands](https://www.psdevwiki.com/ps3/RSXFIFOCommands). Tato vzdálená stránka zde není vydávána za aktuálně ověřený lokální manuál.

## Diagnostika chybějící geometrie

Rozlišuj guest command production, FIFO decoding, draw submission, shader execution, surface obsah a flip/prezentaci. Zapiš počty guest begin/end a draw metod, rozsah vertex/index bufferu, typ/stride/offset/endianness, platnost programů a surface. Následně ověř viewport/scissor, depth/blend/clear a display buffer.

**Otevřená otázka ACIT:** dostává se skutečný guest až k draw, nebo stále vykonává inicializaci? Odpověď má vycházet z aktuálního logu, nikoli ze čtyř historických černých obrázků. Host okno, PPM soubor, clear a flip jsou samostatné výsledky. Původní menu vyžaduje skutečnou geometrii/obsah a následně ověřenou gamepad změnu stavu.

Veřejná CELL PDF popisují CPU/MFC, ne RSX shader ISA. Pro RSX použij výše uvedené veřejné implementace a zaznamenej odvození i jeho omezení.
