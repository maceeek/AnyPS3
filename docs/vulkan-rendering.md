# První obrazová operace přes Vulkan

AnyPS3 používá jeden grafický backend: Vulkan. Vlastní softwarový rasterizér
nevyvíjíme. Bez dostupného GPU lze stejný kód vykonat ovladačem Mesa Lavapipe
na CPU. To nemění statickou rekompilaci PPU/SPU a nepřipojuje backend RPCS3.

## První ověřený rozsah (sestavení 123)

2026-10-14 proběhl skutečný Vulkan render pass přes Lavapipe, hlášené zařízení
`llvmpipe (LLVM 23.1.1, 256 bits)`, Mesa 26.2.4. Syntetický proud RSX FIFO
provedl pět příkazů `NV4097_CLEAR_SURFACE`. Backend nahrál původní obsah VRAM
do obrazu RGBA8, vykonal `vkCmdClearAttachments`, počkal na fence a přečetl
výsledek zpět do big-endian ARGB8 ve VRAM.

Diagnostika porovnala **všech 57 600 pixelů** plochy 320 × 180 včetně alfa
kanálu. Ověřila také ořez obdélníku na pravém a dolním okraji, zachování
paddingu řádků a paměti mimo plochu, nulové/prázdné čištění a odmítnutí
nepodporovaných příkazů bez posunu GET či změny pixelů. Validační vrstva
Khronos se zapnutou kontrolou synchronizace nehlásila chyby ani varování.

Výstup obsahuje tři barevné obdélníky a malý oříznutý obdélník na tmavém
pozadí. Jde o **diagnostický obrazec, nikoli snímek Ratcheta**. Není to důkaz
vykreslení trojúhelníku, překladu RSX shaderů ani prezentace do okna.
PPM vzniká z přečtené VRAM, nikoli z referenčního výpočtu očekávaných pixelů.

Artefakty aktuálního ověření jsou v ignorovaném `local/vulkan-milestone/`:
`rsx-vulkan-validated.ppm`, `rsx-vulkan.png`, `validated.stdout.log`,
`validated.stderr.log`, `tests-targeted.log` a `tests-full.log`.
Celá sada prošla: **128 testů**, včetně Vulkan testu vyžadovaného přes
`ANYPS3_REQUIRE_VULKAN=1` (bez přeskočených testů).

Také skutečný Ratchet byl znovu sestaven s `-DANYPS3_VULKAN -lvulkan`
(`local/ratchet/native-bundle-123`). Běh dosáhl stejných 102 817 041 kroků PPU,
24 360 dokončených SPU instrukcí a 331 RSX paketů jako sestavení 121; zastavil
se na `GDAT initGameData`. Hlásí `rendered clears=0 presented_frames=0`.
Původní herní load segmenty zůstaly beze změny. `verification.json` obsahuje
kontrolu segmentů a hashe zdrojů, sestaveného programu, ELF a logů.
Vulkan zařízení se v tomto běhu ještě nevytvořilo; cesta pixelů je ověřena
samostatným diagnostickým FIFO výše.

## Reprodukce bez herních dat

Na CachyOS/Arch:

```bash
sudo pacman -Syu vulkan-headers vulkan-validation-layers vulkan-tools vulkan-swrast
mkdir -p build
python -m anyps3.render_demo -o build/rsx-vulkan.cpp
c++ -std=c++17 -O1 -Wall -Wextra -Werror -DANYPS3_VULKAN \
  build/rsx-vulkan.cpp -lvulkan -o build/rsx-vulkan
VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.json \
VK_INSTANCE_LAYERS=VK_LAYER_KHRONOS_validation VK_LAYER_VALIDATE_SYNC=1 \
  build/rsx-vulkan build/rsx-vulkan.ppm
```

Zdroj i výsledný PPM vyžadují nový název; existující snímek se nepřepisuje.
PNG v místních artefaktech je bezeztrátový převod tohoto PPM.
Bez `VK_DRIVER_FILES` backend preferuje dostupné diskrétní, následně
integrované GPU. GPU musí být dostupné procesu a musí mít funkční Vulkan ICD.
Aktuální pracovní sandbox nevidí `/dev/dri`; hardwarová cesta zde ověřena není.
Backend nevyžaduje X11, Wayland ani rozšíření pro swapchain.

Samotný `vulkaninfo` se v tomto sandboxu pokoušel připojit k nedostupnému
displeji. Pro headless diagnostiku zde funguje:

```bash
env -u DISPLAY -u WAYLAND_DISPLAY \
  VK_DRIVER_FILES=/usr/share/vulkan/icd.d/lvp_icd.json vulkaninfo --summary
ANYPS3_REQUIRE_VULKAN=1 python -m unittest tests.test_vulkan -v
```

Bez vývojových hlaviček nebo použitelného zařízení se integrační test
výslovně přeskočí; při `ANYPS3_REQUIRE_VULKAN=1` je chybějící zařízení chyba.
Test nepřeskakuje selhání kompilace ani nesprávný obrazový výstup.

## Rozšíření pro skutečný herní clear (sestavení 131)

Hra již provedla vlastní čištění plochy 960 × 704 přes Vulkan. Barevná
a hloubková plocha používají dlaždicové adresování a dva horizontální
vzorky. Podporujeme úplné masky `0xf0`, `0x03` a `0xf3`; Z24S8 se při
čištění přenáší jako přesná 32bitová hodnota přes RGBA8, zatím bez
hloubkového testování. Adresování respektuje pitch a banku. Plná sada
v tomto bodě prošla se 133 testy; rozšířený Vulkan test kontroluje také
Z24S8, oba vzorky, scissor a nezměněné okolní bajty.

Pro podporované komprimovatelné dlaždice uchováváme nekomprimované
hodnoty; nejde o dekodér komprimovaných dat. Pozdější čtení, blity
a vykreslování musí tuto reprezentaci respektovat. Přehled dalšího
postupu je v [postupu směrem k menu](menu-progress.md).

Sestavení 138 dále dokončilo čtyři skutečné herní flipy. Headless sink
vybírá buffer určený příkazem hry, převádí jeho dlaždicové X8R8G8B8 pixely,
provede Vulkan upload/readback a uloží nový PPM pod `ANYPS3_FRAME_DIR`.
Teprve po úspěšném fence a zápisu aktualizuje stav flipu a pošle povolenou
událost původnímu firmwaru. Snímky jsou zatím celé černé. Nevzniklo okno
ani swapchain; čítač `captured_frames` je oddělen od `presented_frames`.
Test porovnává také celý zachycený snímek se skutečně vykreslenými pixely
diagnostiky a ověřuje, že selhání výstupu neuvolní čekajícího konzumenta.

## Omezení backendu

Podporované čištění používá ARGB8 buffer A a/nebo Z24S8 v lokální VRAM,
jeden nebo dva vzorky a nulový počátek surface clip. Scissor, pitch a
offset se respektují. Částečné masky kanálů či stencil masky, jiné formáty,
MRT a main-memory povrchy explicitně selhávají. Stejně tak kreslení,
upload shaderů a blity. Podpora dvou vzorků při čištění není podpora MSAA
při kreslení geometrie.

Vulkan je volitelný při sestavení generovaného programu přes
`-DANYPS3_VULKAN` a `-lvulkan`. Bez něj pokračují dosavadní CPU/IPC testy,
ale neprázdné čištění skončí explicitní chybou. Vytvoření Vulkan zařízení
je odloženo na první podporované neprázdné čištění.

Každé čištění nyní alokuje přechodné prostředky a synchronně kopíruje
povrch tam i zpět. Je to ověřitelný základ správnosti, nikoli výkonný renderer.
Další práce: trvalé GPU povrchy a jejich koherence s VRAM, převod RSX
shaderů na SPIR-V, draw calls a swapchain pro zobrazení v okně.

Technické podklady: místní RPCS3 `gcm_enums.h` a `rsx_decode.h` pro rozložení
registrů; [Vulkan v ArchWiki](https://wiki.archlinux.org/title/Vulkan),
[ovladače Mesa](https://docs.mesa3d.org/systems.html) a hlavičky Vulkan.
