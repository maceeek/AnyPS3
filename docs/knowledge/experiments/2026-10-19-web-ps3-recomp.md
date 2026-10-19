# Veřejné PS3 → PC rekompilace a relevance pro AnyPS3

Datum rešerše: **2026-10-19**. Dokumentační úkol; žádný cizí port nebyl sestaven
ani spuštěn a žádná změna runtime/profilu AnyPS3 nebyla provedena.

**Výsledek: ano, existují veřejné PS3 statické rekompilace s vydanými balíčky
a doloženými autorskými zprávami o skutečné hře. Nejsilnější nález je
Drakengard 3 Recompiled; dalším je Soulcalibur II HD Online.** Není správné
zaměňovat je s porty Xbox 360. Hratelná část a vydaná alpha však nejsou
důkaz bezchybného průchodu celé hry.

## Jak číst důkazy

- **Ověřené zde:** existující veřejný repozitář, konkrétní zdrojový soubor,
  licence, pevný Git SHA a metadata release z GitHub API.
- **Tvrzení autora:** dosažená herní scéna, FPS a rozsah testů publikovaný
  v primárním README, technickém záznamu nebo release notes. Tento průzkum
  jejich běh nezávisle nereprodukoval.
- **Odvozené / hypotéza:** možné využití v AnyPS3. Žádná níže uvedená
  optimalizace není důkazem zrychlení Ratcheta.

Hledání pokrylo `PS3Recomp`, `CellRecomp`, PlayStation 3 static recompilation,
konkrétní hry a navazující repozitáře. Katalogy a sociální výsledky sloužily
jen k nalezení primárních zdrojů; závěry níže stojí na repozitářích a jejich
releases. Samostatný ověřitelný projekt `CellRecomp` s hotovým PS3 portem
se v tomto rozsahu nenašel. To není důkaz, že žádný další projekt neexistuje.

## Konkrétní porty

Všechny řádky v této tabulce vycházejí z **PS3 verze**, nikoli z verze Xbox.
PPU = PowerPC CPU, SPU = Cell lokální programy, RSX = původní grafická cesta.
Uvedené herní výsledky jsou **tvrzení autora**, veřejnost kódu a revize byly ověřeny.

| Projekt a původní hra | Skutečně popsaný stav a omezení | Architektura / host | Veřejný kód a licence |
| --- | --- | --- | --- |
| [Drakengard 3 Recompiled](https://github.com/soyjxck/dod3-recomp), **BLUS31197 + update 1.01** | Vydaná **v0.2.5-alpha** pro Windows, v0.2.4-alpha pro Mac. Autor uvádí první kapitolu, boj a hub, 60 FPS ve 4K na RTX4090/i9-13900K a 60 FPS/1080p na M1 Pro. Pozdější kapitoly nové generace překladu nejsou prošlé. Windows10 oprava ověřena simulací přísnějších copy pravidel na Windows11, nikoli přímo na Windows10. | PPU+SPU → C++; HLE knihovny; RSX → D3D12 / Metal. Vulkan experimentální a pomalejší. Windows x64/AVX2, macOS arm64. | **MIT**, generátor/runtime i nativní části veřejné, vlastní hra nutná. [README v pevném SHA](https://github.com/soyjxck/dod3-recomp/blob/96df4c364a20c306724a48886a43f8f14ce5bedd/README.md), [release](https://github.com/soyjxck/dod3-recomp/releases/tag/v0.2.5-alpha). |
| [Soulcalibur II HD Online](https://github.com/n0madic/Soulcalibur2HD-recomp), **NPUB31366**, 01.00/01.01 | Vydaný launcher **v0.1.0**; autor popisuje běžný boj na Macu a na Windows/Arc140V po sestavení shaderů 60 FPS. Linux pouze softwarový Vulkan (~10 FPS), skutečný Linux GPU neověřen. Online nefunguje; občasný chybný přechodový snímek, A/V a dekódovací omezení. | PPU+5 SPU programů → C/C++ až na uživatelově PC; ps3recomp; Metal / Vulkan, SDL. Windows, macOS arm64, omezený Linux. | Zdroj **GPL-2.0-or-later**, celek binárky **GPL-3.0-or-later** kvůli kombinaci s Apache2 částmi; přesně rozlišeno v README. Kód/patches/launcher veřejné, nikoli vygenerovaná hra. [Pevná revize README](https://github.com/n0madic/Soulcalibur2HD-recomp/blob/be2dd0640bc0e7486c4a3a47d027bd643fd0cd90/README.md), [release](https://github.com/n0madic/Soulcalibur2HD-recomp/releases/tag/v0.1.0). |
| [The Simpsons Arcade Game PS3](https://github.com/sp00nznet/simpsonsarcade-ps3), **NPUB30563** | Autor uvádí intro, menu, ukládání, Stage1 dál, pad a online kooperaci; 35–57 FPS ve hře, 60 v menu. Zdrojový build, neověřen průchod celé hry; při propadu FPS mezery ve zvuku. | Staticky přeložen PS3 PPU/SPU obal; RSX → D3D12, Windows/clang-cl. **Uvnitř jde o rekompilovaný PS3 arcade emulator:** původní 6809 arkádový kód se nadále interpretuje z ROM. | **MIT** ověřena v LICENSE. Konfigurace, runner a relift tooling veřejné; potřebuje vlastní PSN data a zachycení dynamicky skládaných SPU jobů. [Pevné README](https://github.com/sp00nznet/simpsonsarcade-ps3/blob/5371f2575b9dc9d2a7d2c3c615e4072bb85a9800/README.md). |
| [Yakuza: Dead Souls Recompiled](https://github.com/canersaka/Yakuza-Dead-Souls-Recompiled), **BLUS30826 v1.00** | Výslovně WIP. Autor uvádí úvod → New Game → gameplay, kancelářový tutorial 30 FPS, zalidněný venkovní začátek ~20 FPS. Hotový bezchybný port ani binární release tím doložen není. | PPU, shader PRX i SPU → C++; vlastní HLE/SPURS; RSX VP/FP → HLSL/D3D12, Windows x64. | **MIT**, veřejný fork runtime, liftery i herní runner. Herní C++ se generuje z vlastní kopie. [Pevné README](https://github.com/canersaka/Yakuza-Dead-Souls-Recompiled/blob/95135736b2e20ecf19e67457bbabc2926c3b355f/README.md). |
| [God of War II HD](https://github.com/andrebrumdev/gow2-recomp), **NPUA80491** | Autor uvádí Rhodes 45–60 FPS, boj s Kolosem hratelný s chybou kostní matice; zvuk má časovací omezení. Uvedené další mobilní platformy jsou experiment. | PPU+7 SPU programů → C/C++, ps3recomp fork; Metal/MSL, VideoToolbox, CoreAudio; Apple Silicon. | **MIT**, veřejný setup kit, hranice funkcí, patche, testovací skripty; bez herních bajtů. [Pevné README](https://github.com/andrebrumdev/gow2-recomp/blob/a787912c7b2e2f61bb387c9c22722a93e6c5ee3c/README.md). |
| [Tornado Outbreak](https://github.com/sp00nznet/tornado), **BLUS30371** | Úvod → New Game/save → první úroveň `jb_intro`, pohyb tornáda, 10–16 FPS. **Po pár sekundách bílý přechod nezmizí.** Doložený herní milník, nikoli doporučení hotového portu. | PPU+malý raw-SPU memcpy program a Wwise SPURS joby; ps3recomp, D3D12/Windows. | **MIT**, runner a build/relift nástroje veřejné. [Pevné README](https://github.com/sp00nznet/tornado/blob/2cc6c0fb660b62aab235e06ac8e0fa64106ec265/README.md). |

**Ověřené:** GitHub API skutečně vrací assets Drakengard3 a Soulcalibur2HD;
nejde jen o odkaz na budoucí release. Drakengard3 v0.2.5-alpha publikován
2026-10-16, SHA `96df4c364a20c306724a48886a43f8f14ce5bedd`, asset
`dod3-recomp-v0.2.5-alpha-win64.zip`. Autorem uvedený SHA256 zipu:
`62fad10015b95a482b5f720263cb67cef4066439ae1432a91485a2b53f55f535`.
Binárka nebyla stažena ani ověřena. Soulcalibur v0.1.0 publikován
2026-10-16: Windows x86_64, Linux x86_64 a macOS arm64 balíčky.

Další relevantní, ale slabší důkazy: [inFamous autorův PR287](https://github.com/sp00nznet/ps3recomp/pull/287)
popisuje gameplay na úplné LLE větvi; [Killzone3 autorův technický issue214](https://github.com/sp00nznet/ps3recomp/issues/214)
popisuje první úroveň a desítky obecných oprav (BCUS98234, PPU+SPU/Havok,
RSX deferred, Windows build pod Protonem). Nejde o ověřenou dostupnost hotových
portů. Naopak [juans-collab/infamous-2-recomp](https://github.com/juans-collab/infamous-2-recomp)
sám výslovně říká **Not playable**; pouhý název portu není důkaz.

## Společný základ a pevné revize

**Ověřené:** [sp00nznet/ps3recomp](https://github.com/sp00nznet/ps3recomp)
je veřejný **MIT** toolkit s PPU/SPU liftery, memory/LV2/HLE runtime a vlastní
RSX implementací. **v0.15.0** vyšla 2026-10-16; čtený upstream strom:
`39a36296511c445707adabcd312f0b740cfbd694`. [Release notes](https://github.com/sp00nznet/ps3recomp/releases/tag/v0.15.0)
oddělují sjednocené opravy od odložené změny memory domains #296, která
rozbíjela boot VF5. Release SDK není release každé hry.

Drakengard3 používá jiný [soyjxck/ps3recomp fork](https://github.com/soyjxck/ps3recomp),
gitlink přesně **`bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb`**. Nezaměňovat jeho
`ppu_vm_fast.h` či `rsx_vulkan_engine.c` s dnešním upstreamem, který má
`rsx_vulkan_backend.c` a jinou strukturu. Údaje v různých dokumentech mohou
pocházet z různých stadií: například starší části `PPU_RECOMP.md` stále
popisují počáteční Uncharted boot, zatímco sekce o lokálních registrech
už měří Drakengard3. Rozhodující je konkrétní implementace a revize.

## Pět oblastí pro navazující práci v AnyPS3

Pořadí vychází z lokálního [měření379+](2026-10-19-performance-379.md):
PPU memory ~35 % CPU, group4 single FP ~28 %, CPU část RSX ~40 ms.
PIPE2 ukázal producenta engine counteru přes SPU PUTLLUC; samotná podobnost
s cizím čekáním na RSX fence z něj RSX problém nedělá.

### 1. PPU page fast path a slučování paměťových kontrol

**Ověřený kód:** [fork/runtime/ppu/ppu_vm_fast.h](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/runtime/ppu/ppu_vm_fast.h)
ř.1–48, 83–151 a 167–248; [ppu_vm_page.h](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/runtime/ppu/ppu_vm_page.h).
64KiB stránkové příznaky rozhodují o krátké cestě; rezervace, MMIO, sledování
a diagnostika mají pomalou cestu. Stack/prolog a souvislý běh přístupů sdílí kontrolu.
**Tvrzení autora v komentáři:** varianta s několika větvemi byla o9–11 % CPU
horší než volání helperu, přestože měla méně instrukcí.
**Hypotéza:** velmi relevantní pro PPUMEM2, nejprve porovnat jeho epochy s touto
stránkovou reprezentací. **Nepřebírat přímo:** flat mapping, volatile přístupy
a host race/lifetime model nejsou důkaz bezpečnosti pro AnyPS3. Nutné zachovat
unmap, dirty tracking a PPU/SPU reservations i jejich souběh.

### 2. Registry v lokálech a přesné dirty/liveness analýzy překladače

**Ověřený kód:** [fork/tools/ppu_nv_dirty.py](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/tools/ppu_nv_dirty.py)
ř.1–89, [PPU_RECOMP.md sekce91–183](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/docs/PPU_RECOMP.md#registers-in-locals---nonvolatile-locals).
PPU GPR/FPR/VMX a oddělené CR/XER lokály se ukládají při volání jen pokud
jsou změněné a potřebné; call graph má explicitní ABI hranice a ověřovací režim.
**Tvrzení autora:** vydání0.2.4 snížilo CPU dvou hlavních vláken na Windows
asi o třetinu; není to izolovaný výsledek jediné transformace.
**Hypotéza:** adaptovat do `anyps3` generátoru až po kontrole jeho současných
bloků. Pozor na scheduler preemption, HLE callbacky, mid-function vstupy a
uložení stavu před stopem; pravidla AnyPS3 mohou vyžadovat více writebacků.

### 3. Přesné SPU FP rychlé cesty a nezávislý oracle

**Ověřený kód:** [upstream/runtime/spu/spu_helpers.h](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/runtime/spu/spu_helpers.h)
ř.570–624 a následující NEON část; [spu_float_referee.py](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/runtime/spu/tests/spu_float_referee.py).
Normální operandy používají double mezivýsledek a kontrolovanou truncation,
nepříznivé lane přesný fallback. [PR287](https://github.com/sp00nznet/ps3recomp/pull/287)
byl sloučen 2026-10-16, merge `41c19c8`; testy interpretu/lifteru porovnávají
s RPCS3 a případné float rozdíly adjudikuje exact arithmetic.
**Hypotéza:** oracle a hraniční případy pomohou SPUFP2. Hlásené1,6–4× se
týká **ARM NEON**, nelze slibovat na našem x86. MXCSR/extended-range/zero
semantiku je třeba dál prokázat proti manuálu a našim testům.

### 4. Write-watch cache a dávkování RSX práce

**Ověřený kód:** [fork/runtime/platform/vm_watch.c](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/runtime/platform/vm_watch.c)
a [fork/libs/video/rsx_vulkan_engine.c](https://github.com/soyjxck/ps3recomp/blob/bbcf9a53c7efef9fe7ca84c058f0292ed998d0cb/libs/video/rsx_vulkan_engine.c)
ř.1–33: sběr draw records, staging chunks, společný rendering pass, timeline
semaphore. [Technický záznam Drakengard3](https://github.com/soyjxck/dod3-recomp/blob/96df4c364a20c306724a48886a43f8f14ce5bedd/docs/TECHNICAL.md#rendering)
popisuje validaci texture/vertex cache pomocí zápisů a subalokace.
**Hypotéza:** zmenšit práci na CPU RSX threadu, ne zaměnit ji s GPU časem.
Fork Vulkan je zde **Windows-only**; upstream Linux
[rsx_vulkan_backend.c](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/libs/video/rsx_vulkan_backend.c)
je přímější POSIX reference. Nejdřív audit existující AnyPS3 cache, guest DMA,
readback a fence pořadí; přenos celého backendu nedává smysl.

### 5. SPURS admission, persistent workery a wake-up invarianty

**Ověřený kód:** [upstream/libs/spurs/cellSpurs.c](https://github.com/sp00nznet/ps3recomp/blob/39a36296511c445707adabcd312f0b740cfbd694/libs/spurs/cellSpurs.c)
ř.1660–1767; [PR291](https://github.com/sp00nznet/ps3recomp/pull/291).
Koordinátor přiděluje práci persistent workerům s limitem počtu SPU a
workload contention, odděluje kontexty DMA a rezervace.
**Hypotéza:** použitelné jako kontrola producent/consumer hladovění a
ztraceného probuzení. U Ratcheta nejprve určit guest job a kritickou cestu
PUTLLUC counteru. Náš skutečný LLE SPU job manager není jejich HLE scheduler;
jeho nahrazení není obecná optimalizace. Zvýšení počtu workerů bez měření
ani odstranění polling smyčky není podložená oprava.

## Co nepřenášet a co nebyl PS3 výsledek

**Ověřené:** Drakengard3 kromě obecného runtime obsahuje game-specific hooks,
upravený limiter, náhrady vybraných SPU úloh, přepsané menu/Unreal bytecode
a asset overlay ([TECHNICAL.md](https://github.com/soyjxck/dod3-recomp/blob/96df4c364a20c306724a48886a43f8f14ce5bedd/docs/TECHNICAL.md#the-settings-menu)).
Jeho celkové FPS nelze připisovat pouze statickému překladu a tyto náhrady
nejsou vhodnou cestou ke splnění podmínek AnyPS3. Relevantní jsou obecné
algoritmy a testovací metodika výše.

[UnleashedRecomp](https://github.com/hedge-dev/UnleashedRecomp) opravdu portuje
**Xbox360** Sonic Unleashed: Xenon PowerPC přes
[XenonRecomp](https://github.com/hedge-dev/XenonRecomp), Xenos shadery přes
XenosRecomp; GPL3. Neřeší Cell SPU ani RSX. N64Recomp zase řeší MIPS N64.
Tyto úspěchy nejsou důkaz úspěšné PS3 rekompilace. [RPCS3](https://github.com/RPCS3/rpcs3)
je PS3 emulator s JIT/AOT a nejvyspělejší behaviorální reference; není
samostatný statický herní port. Licence RPCS3 je kombinace GPL2-only a dalších
licencí podle souboru, nikoli MIT. Tvrzení upstreamu o clean-room původu
neodstraňuje potřebu ověřit licenci konkrétní přebírané revize; PR287 sám
zmiňuje odstranění staršího GPL-derived float kódu.

## Artefakty a další první akce

Čtené veřejné soubory a GitHub API odpovědi jsou v
`local/ratchet/codex/web-ps3-recomp/`, inventář `manifest.json` zahrnuje SHA256,
URL a revize; `validation.json` kontroluje inventář a odkazy lokálních artefaktů.
Žádné herní soubory, releasové binárky ani firmware se nestahovaly. Žádné
testy, instalace, buildy ani herní běhy nebyly pro tuto rešerši zapotřebí.

**První další akce v rámci této rešerše:** porovnat oddělený PPUMEM2
kandidát s `ppu_vm_fast.h`/`ppu_vm_page.h` a navrhnout jediný izolovaný
prototyp minimalizace per-access kontrol se zachováním současných souběžných
invariantů. Změřit stejnou `great_clock_a` scénu; syntetický ns/access údaj
sám o sobě nestačí. SPUFP2 oracle je další přímá cesta. **Pořadí skutečné
implementace určuje hlavní [HANDOFF](../HANDOFF.md): nejprve malý SPUFP2
kandidát; PPUMEM2 odděleně kvůli jeho hot-tracked regresi.** Rešerše samotná
nezměnila skutečný stav cíle 60 FPS ani nerozhodla o integraci kandidátů.
