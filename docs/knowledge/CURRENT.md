# Aktuální stav a cíl

**2026-10-19 odpoledne — nové kolo (Claude Opus 5.5), goal uživatele: hra na 60 FPS s rezervou.**
Ověřeno: v úrovni `great_clock_a` hra běží v režimu 0 (VSYNC bez stropu, `G+84=0`, `G+24=0`) → cíl 59,94 FPS;
~15 FPS je výkonové plató, ne herní strop. Výchozí měření `measure_level.sh 380 380base`: **14,25 FPS** (medián 14,97),
hlavní PPU 43 ms/snímek (2,06 ns/instr, 21 M instr/snímek), RSX worker 38,7 ms/snímek (~39 µs/draw), SPU ~10 ns/instr.
**Konec session 2026-10-19 ~22:00: nejlepší ověřený build `native-virtual-460` (= 386 + RSX2) — 22,59 FPS**
(medián 23,0) v původní úrovni, obraz správný, bez STOP; 386 = 21,35 FPS. Úzké hrdlo RSX worker (~28 ms/snímek),
dále GPU ~30 ms/snímek při nízkých taktech. Sada nad 386: 757 testů, 751 OK (3 = jedna SPU regrese opravená ve
stromu SPUFAST, 3 = timeouty kompilace). 60 FPS nedosaženo. Žádný agent ani build neběží.
**Živý strom = snímek 460** (povýšeno 2026-10-19 ~22:40 na pokyn uživatele; zdrojové hashe = build 460) a je to
první commit gitu (`origin` https://github.com/maceeek/AnyPS3.git, větev `main`). Známé: 3 testy `test_par1/2`
(chybějící kontrola „unknown native SPU image“, oprava v `local/ratchet/work/spufast/tree`).
Vlna FAST (subagenti v `local/ratchet/work/`, uzavřeno); plán, nástroje, důkazy a první další akce:
[záznam FAST](experiments/2026-10-19-fast-wave.md). Stav níže (předání 380) zůstává platný jako výchozí bod.

Aktualizováno 2026-10-19, finální předání (Codex + GPT-6.1-sol High subagenti).
**Na pokyn uživatele se toto kolo ukončuje a pokračování se předává jinému AI prostředí. Aktivní goal byl
uživatelem zrušen; projektový požadavek původní hry s ovládáním a rezervou pro 60 FPS tím není splněn.**

**Poslední ověřený build: `native-virtual-380`, zdroje `source-snapshot-380`.**
380 = 377 + HLEFAST1 + oprava scheduler deadline + opravené očekávání atexit testu.
Živý runtime/testy/profil odpovídají snímku380;117 source hashů a runtime artefakty ověřeny.
Skutečný běh `native-virtual-380load`: původní menu → LOAD → `great_clock_a`,
**15,02 FPS v 67 intervalech za134,067s**, min13,5/max16,0. Clank se prokazatelně pohybuje
virtuálním padem (`evidence-input-montage.png`). Po měření a vstupu řízené ukončení SIGKILL;
`verification.json`: returncode−9, stop null, oba původní PT_LOAD shodné,34 captures.
Build379 ve stejné scéně14,64 FPS/120,08s. Rozdíl~2,6% z jednoho běhu na variantu není průkazný výkonový zisk.
Menu, skutečná úroveň a virtuální vstup jsou ověřeny; **60 FPS ani fyzický gamepad nejsou ověřeny**.
Uinput přes SDL byl ověřen dříve v363; toto kolo jej neopakovalo.

**Konkrétní překážka:** výkon původní úrovně. PIPE2 doložil SPU jako producenta engine completion counterů
group2 a4 přes MFC PUTLLUC. Během group4 čekání SPU vykonával původní mikroprogram0x27
(EA0xbbf480, LS0x26c00, všech5264B shodných s EBOOT), FP normalizační kód.
V této skupině `spu_single_vector`+klony27,95% CPU; úplná vazba na dřívější27ms čekání stále není prokázaná.
Původní tvrzení „RSX fence je příčina čekání“ není podložené.

**Připravené, neintegrované:** SPUFP2 (27 cílených testů PASS, syntetický AOT1,31–1,37×),
PPUMEM2 (dokončená validace a sanitizéry, untracked AOT~2×, ale hot-tracked+46,6% času).
Ani jeden kandidát není součástí380 a nemá herní FPS. **Celá regresní sada sloučeného380 nebyla spuštěna.**

**První další akce v novém prostředí:** převzít [HANDOFF.md](HANDOFF.md), ověřit aktuální procesy a SHA,
potom posoudit malou SPUFP2 dodávku proti snímku380; po případné integraci nová celá sada a skutečný běh
stejné úrovně. Další možnosti přináší [webová rešerše PS3 recomp projektů](experiments/2026-10-19-web-ps3-recomp.md).
Přesné výsledky, příkazy, artefakty a omezení: [záznam379–380](experiments/2026-10-19-performance-379.md).
Host audit 2026-10-19 09:59 UTC: žádná vlastní hra, kompilace, debugger ani běžící AnyPS3 scope;
`local/ratchet/handoff380-process-audit.json`. Stav při novém převzetí znovu ověřit.

**Absolutní pravidlo stroje (uživatel, 2026-10-17): z tohoto PC nikdy žádný slyšitelný zvuk (v místnosti spí miminko).**
Zvuk jen přes SDL `dummy`/`disk`; `native_audio.py` bez `ANYPS3_AUDIO_AUDIBLE=1` skutečné zařízení odmítne (nikdy
nenastavovat). Sinky PipeWire jsou ztlumené. Platí pro hlavního agenta i všechny subagenty.

## Kde hra je

Původní hra projde bootem, videi (přeskočitelné STARTem), načítáním `front_end`, titulní obrazovkou a dojde do
**původního hlavního menu** (NEW GAME / LOAD / COMMUNITY / OPTIONS / DELETE) za ~2 min od startu; menu reaguje na
virtuální pad (šipka dolů → LOAD). Rendering: vlastní Vulkan RSX (NVIDIA RTX 4070 SUPER), PPU/SPU staticky přeložené.
Historický výkon menu339 byl medián59,94 FPS; tento údaj není výkon úrovně380, která má15,02 FPS.

## Rozdělená kompilace (od 256)

`build_game` zapisuje `units.json` + `anyps3.hpp`, `program.cpp` (runtime, main),
96 `ppu-*.cpp` a 96 `spu-*.cpp`; `virtual_pad.py build -j 24` kompiluje paralelně za **~4 min**
(dříve jediná 458MB TU, LTO 7–15+ min). Viz [záznam 256](experiments/2026-10-15-split-units-256.md).

## Historie skutečných stop (256–339; současný stav je nahoře)

- **256**: `SPU code differs from static image/overlays` (SPU 0x200, LS 0x7270): job CollBroad
  `0xb9bd80` chyběl v profilu, CollNarrow ořezán. Oprava profilu 257 (138 jobů z původních datových odkazů,
  `local/ratchet/tools/spu_job_overlays.py`).
- **257**: `pc=0x202a9a4c` `lwarx r9,0,r31` (r31 `0x202b0c2c`), 541 769 959 kroků: rezervační granule
  přesahuje konec datového segmentu firmware modulu (`0x202b0000+0xc38`) → `unmapped or split guest memory
  access size=128`. Oprava 258: PPU rezervace vyžaduje mapování jen čtených bajtů, granule sdílená
  s jinou oblastí = explicitní stop; test `tests/test_memory.py::PartialGranuleReservationTests`.
- **258**: `sys_config_open` (syscall 516) z nativního libio — hra volá `cellKbInit/cellMouseInit`
  a `cellPadSetActDirect` mimo HLE pad. Oprava 260: `anyps3/native_hid.py` (Kb/Mouse knihovní stav, 0 zařízení),
  `cellPadSetActDirect` bez aktuátoru → `UNSUPPORTED_GAMEPAD`; testy `tests/test_native_hid.py`.
  Současně codex: `_sys_*printf` a bitově shodné zrychlení RSX draw (`local/ratchet/codex/*-result.md`).
- **260**: 1 214 M kroků za ~11 min; stop SPU FP → codex 42 instrukcí + frest/frsqest LUT + rchcnt (261).
- **261–267**: DMA seznamy, prázdný seznam, okno SPU skupiny `0xF0000000`, tabulka SPU modulů `0xde3600`
  (LS báze = hypotéza), `NTCL getstate` (netctl odpojeno), paralelní RSX (codex). Od 263 běh ~24 M kroků/s
  (884 M za 36 s). Detaily: [záznam 256–267](experiments/2026-10-15-split-units-256.md).
- **268–280** (paralelní dávka, viz záznam): heap, NPMG offline, OPD seedování firmware, switch tabulky
  s TOC modulu a `lwax`, `lswi/stswi`, `tw/twi`, NPDR drmrequest (chybějící EDAT → `ENOENT`), atexit callbacky
  jako guest volání (`ATEXIT_RETURN` trampolína), 5+2 paralelních codex skupin (FS/SYNC/SPU/RSXNET/SAVE,
  INTEGRATE, IMPORTS3) přes `anyps3/lv2_*_ext.py`.
- **281–284**: NPTR trofeje, konfigurovatelný strop spánku, `get_priority` → ESRCH pro ukončené detached
  vlákno (hra tím detekuje konec „Check Thread“), NV4097 semafory 0x1d70/0x1d74.
- **284**: 1 034 431 488 kroků, `nonempty RSX image transfer requires a native blitter` → codex RSXSWEEP
  (NV0039/NV3089/NV308A, 272 metod) a VSHUI (MSGD/savedata dialogy řízené padem).
- **285–286**: regrese na 150 M — nový stop Z-cull statistik (`GET_REPORT` typ 2). 287: sémantika typů 2–5
  převzata z RPCS3 `RSXZCULL.cpp:337-373` (**odvozené**, aproximace čítače; zde 0).
- **287–288**: `map::at` → 0x1d6c (`SET_SEMAPHORE_OFFSET`) hra nikdy nezapíše. Od 289 mají nezapsané RSX
  registry reset hodnotu 0 (RPCS3 `rsx_methods.cpp:137`; ostatní nenulové výchozí hodnoty tam jsou příkazy
  `cellGcmInit`, které zde vykonává nativní libgcm). Testy v `tests/test_rsx.py`.
- **289** (poslední): 1 042 889 216 kroků, `RSX fragment depth export is unsupported`. Hra už načítá
  **`packed/levels/front_end/level_{cached,uncached}.psarc`** (menu úroveň), kontroluje PARAM.SFO jiných
  Ratchet her a začala renderovat do 960×704 MSAA (samples=2). Snímky dosud ukazují načítací planetu.
- Po 289: PPU `vadd{u,s}{b,h,w}s`, `vsub{u,s}{b,h}s`, `vsubsws` (test `test_ppu_string_trap.py`).
  Běží codex **RSXGAPS** (MSAA, formáty cílů, primitivy, clipping, úplné FP/VP opcode vč. depth exportu,
  textury) a **SPUGAPS** (stop kódy 0x0/0x10/0x203/0x3fff, instrukce 0x51cef5e8, statický sken SPU).
  Základ zdrojů před agenty: `local/ratchet/source-baseline-290/`.
- **290–293**: clear swizzled 128×128 vyřešen (RSXCLEAR/RSXSWZ/RSXBLIT2 hotovy); 293: 1 048 M kroků, stop remap textur → běží codex RSXTEX. Dříve:
  RSXCLEAR, RSXSWZ, RSXBLIT2 (viz záznam 256–291); RSXGAPS a SPUGAPS hotovy.
- **294–296**: 296 normální 1 121 M kroků (stop split RSX čtení `0x49ac8c00`); **296-diag (RSX skip, není důkaz)
  ukázal původní obrazovku „Would you like to turn subtitles on? YES/NO“**. Virtuální pad tam hra ignorovala →
  příčina: chybějící `CELL_PAD_CAPABILITY_PS3_CONFORMITY` (opraveno, zatím nesestaveno). Běží RSXTEX3, RSXMEM.
- **297-diag (0 RSX přeskoků)**: virtuální pad naviguje původní obrazovku titulků (right → NO, cross → „initial
  setup… CONTINUE“); po CONTINUE stop nepřeložený switch v libsysutil → detektor rozšířen (298). Běh 298 normální
  s `ANYPS3_PAD_TRACE=1` běží. Hlavní menu zatím nedosaženo.
- **298 (normální běh) — ověřená navigace virtuálním padem na původní obrazovce titulků** (right → NO, left → YES,
  cross → „initial setup“ → cross → stop NPCU `cmTerm`). Hlavní menu zatím nedosaženo; FPS ~0,5 flipu/s.
- **Výkon**: ~99 % CPU v SW rendereru, 0,5 flipu/s. Priorita: **GPU (Vulkan, RTX 4070 SUPER) backend** —
  codex RSXVK; CPU optimalizace RSXPERF. 299 (NPCU cmTerm) běží.
- **300**: stop `updateGameData` (instalace herních dat „initial setup“) → codex GAMEDATA; GPU backend funguje
  bitově správně na RTX 4070 SUPER, ale zatím pomalu → codex RSXVK2 (surface cache).
- **303–306 (2026-10-16)**: plánovač emuluje **2 HW vlákna PPU** (hlavní vlákno už nehladoví); SPU joby „Bink“,
  „PostProcess“, „Composite“, „SyncJobs“ přidány do profilu; nulový clip = no-op; dithering bez ditheru.
  **GPU renderer (Vulkan graphics, RTX 4070 SUPER)** zapnutý: `ANYPS3_RSX_VK_BUILD=1` při kompilaci,
  `ANYPS3_VK_DEVICE=gpu ANYPS3_RSX_BACKEND=gpu VK_INSTANCE_LAYERS= ANYPS3_FRAME_STRIDE=600` při běhu; načítání až
  ~46 flipů/s, zobrazeno původní upozornění o epilepsii (305). Zbývající brzda: GPU fallback na CPU u stavů
  logic op/fog/stipple/shader packer/two-side/window → codex RSXVK5 (parita). Readback přes HOST_CACHED paměť.
- **309 (2026-10-16): původní hlavní menu dosaženo a navigováno virtuálním padem** (NEW GAME → LOAD šipkou dolů;
  START na titulní obrazovce). Menu ale běží ~0,7 FPS (cíl 60) → profil + codex RSXVK7.
- **311 (2026-10-16): OOM** — systemd-oomd v 07:55 zabil terminálovou scope (session, běh 311, codex RSXVK8).
  Příčina **ověřená** během 311m/311h/311f: na 12. snímku načítání `vkCreateGraphicsPipelines` NVIDIA kompiluje
  fragment shader 125 kB GLSL (169 FP instrukcí, emulace 4 lanes quadu na invokaci) → ≥ 20 GB RAM; `spirv-opt -O`
  na něm hlásí ID overflow. 309 tyto FP posílal na CPU fallback. Alias draw (barva = hloubka) je čistě hloubkový
  (`color_mask=0`). Cílové FPS: volič `0x661c50` (mode 0 = VSYNC ≤ 59,94; mode 2 = 29,97; …), viz záznam.
  RSXVK8 hotov (LRU cache, perzistentní SPIR-V/pipeline cache, 474/474 testů); běží **codex RSXVK9** (přímý GLSL
  FP jako RPCS3, VkImage textury, neutrální barevný alias). Diagnostické buildy 311d/f/i ze snapshotu
  `local/ratchet/source-snapshot-311d` (nejsou důkaz), základ integrace `local/ratchet/source-snapshot-312`.
  Hra i agenti nyní běží v `systemd-run --user --scope -p MemoryMax=…`.
- **313 (2026-10-16 14:45, snímek `source-snapshot-313` = RSXVK8+RSXVK9+PPUBLOCK):** normální běh (bez diag.
  přeskoků, CPU fallback vypnut) prochází načítáním přes dřívější FS 27 do 2 min, RSS ~1,1 GB, 27 pipelines.
  `PERF`: ~2 flipy/s, ~125 draw/s, PPU ~1,7 M instr./s; host ~1,7 ms + flush ~2,3 ms na draw. Profil 313p:
  `decode_texture` 34 % (CPU dekódování textur každý draw, `rsx_draw.py` ~ř. 1207) → **codex RSXVK10**.
  Celá sada nad snímkem 313: 481 testů, 479 OK; `test_anyps3` OK po doplnění `profiles/` do kopie,
  `test_rsx_scalar_fp` překročil časový limit kompilace pod zátěží (3,3 s vs 0,9 s bez zátěže) — zopakovat v klidu.
- **316c (2026-10-16 16:05–16:30, snímek `source-snapshot-316`, normální GPU běh):** opravený `usleep` + RSXVK10 →
  **titulní obrazovka za ~15 min a hlavní menu za ~20 min** (309: ~45 min), videa přeskočena virtuálním STARTem,
  menu snímek `native-virtual-316c/frames/frame-000022.ppm`, virtuální šipka dolů přečtena (`digital1=0040`).
  **Menu ~1,7 FPS** (~78 draw/flip). Profil menu `local/ratchet/codex/rsxvk/game-profiles/menu-316c.*`:
  `GuestMemory::range` 26 %, `ioctl` 10 %, texel/texture_image/lroundf ~18 %; čítače: texture miss 436 k / hit 74 k,
  `invalidate_gpu_write` 173 k, flush unprotectable 222 s, host_read 148 s → render-to-texture přes CPU.
  Běží **codex RSXVK11** (RTT na GPU, unprotectable flush, GPU VP, exact FS) a **SPUBLOCK**.
- **317 (RSXVK11+SPUBLOCK):** titulní obrazovka a menu ~5,9 FPS (4× proti 316c). **Cílové FPS = 59,94 ověřeno za
  běhu** (`G+84=0` VSYNC bez stropu). Chybí ~10×: RSX host ~45 %, SPU FP/DMA ~23 %, vše na jednom host vlákně.
  Běží codex **SPUPERF**; další: RSXVK12 (ustálená cena draw v menu, capture menu přes `ANYPS3_RSX_CAPTURE_TRIGGER`),
  pak paralelizace SPU/RSX na více host vláken.
- **318:** menu 6,1–6,4 FPS. **319** (RSXVK12+SPUPERF+PAR0): menu **9,75–9,99 FPS**, titulka po ~4 min.
  **320** (zarovnané `GuestBacking`, allocator-edge readbacky pryč): menu **11,0–11,3 FPS**, DOWN → LOAD ověřeno.
  `PERF WALL` na snímek: RSX CPU ~37 ms, GPU wait ~36 ms, SPU 8,5 ms, PPU 7,6 ms. **GPU běží v P8 na 210 MHz**
  (ovladač bursty zátěž považuje za idle); s GPU vytaženým na P0 (souběžný vkcube) menu ~15 FPS, gpu_wait 5,6× menší.
  Na START na titulce držet ~2 s; po ~60 s nečinnosti titulka spouští attract video (START → zpět).
  Program ukončovat `kill -KILL <pid programu>` (SIGINT/TERM zachytí SDL) → wrapper zapíše `verification.json`.
- **322** (RSXVK13 + PAR1, `-march=native`): menu **15,66 FPS** za ~2 min od startu; RSX CPU 21,9 ms, GPU wait
  25,1 ms (`label_reader`), SPU 9,1, PPU 7,9 ms/snímek. **323** (oprava relaunch SPU workeru): s
  `ANYPS3_SPU_WORKER=1` jen 9,99 FPS (zámky PAR1 paměti) → PAR2. Do menu: `local/ratchet/tools/to_menu.sh DIR`.
- **325s** (PAR2 + RSXVK14 + opravy poolů a wakeupu, sériově, čisté měření): menu **21,94 FPS**, PPU 7,3 / SPU 5,3 /
  RSX 16,2 / GPU wait 16,0 ms na snímek; DOWN → LOAD. `ANYPS3_SPU_THREADS=1` jde do menu, ale ~10 FPS (lanes aktivně
  pollují reservation event, false sharing labelů s FIFO) → PAR3/RSXVK15 a navazující vlna.
- **326** (PAR3 + RSXVK15): sériově menu **24,42 FPS** (PPU 6,8 / SPU 4,8 / RSX 17,0 / GPU wait 9,6 / vblank 3,0 ms);
  `SPU_THREADS=1` 10,75 FPS (polling LR eventu). Běží PAR4 (SPU wait, false sharing, RSX worker) a RSXVK16 (≤ 20 µs/draw).
- **327** (PAR4 + RSXVK16): sériově 21,83 FPS (regrese PPU 7,46 → 9,39 ns/instr s aktivním observerem);
  `SPU_THREADS=1` 15,18 FPS (lanes parkují; `sched_yield` 33 % kvůli legacy RawPermit). Nejlepší ověřené: **326
  sériově 24,42 FPS**. Měření: `local/ratchet/tools/measure_menu.sh`. Běží PAR5 + RSXVK17.
- **328** (PAR5 + RSXVK17, sada 564/564): **sériově menu 24,59 FPS** — nejlepší ověřený stav (PPU 6,6 / SPU 5,4 /
  RSX 16,5 / GPU wait 9,5 / vblank 3,1 ms). `SPU_THREADS=1` 15,78 FPS. **329** (PAR6 RSX worker): výchozí 22,34 FPS;
  `RSX_WORKER=1` uvízne na načítání (každá HLE služba → MapBarrier → přestavba Session). **330** (PAR7+RSXVK18,
  576/576): sériově 23,98 FPS; `RSX_WORKER=1` do menu 14,98 FPS (paměťový protokol ~55 % CPU). Běží PAR8 + RSXVK19.
- Fyzický gamepad: k počítači není připojen (`/dev/input/js0` = ASRock LED Controller) → neověřitelné.
- **331–337** (PAR8–PAR11, RSXVK19–21): menu 38–40 → **57,5–59,5 FPS**, navigace 10/10 — viz [záznam 332+](experiments/2026-10-17-menu-fps-332.md).
- **338** (PAR12 inline skalární load/store): nelze přeložit — TU > 10,7 GB GCC; OOM zabil session (17:07) i scope 338r.
  **339** = 338 + `runtime.py` (rychlá cesta out-of-line, `load()/store()` bez `always_inline`): TU 1,0 GB / 24 s,
  build 8 min / 16,9 GB (−j16), PAR12 6/6 + PAR10 memory 10/10; menu medián 59,94 FPS, PPU 11,1 ms/snímek.
- **337game (2026-10-17 18:23)**: menu → NEW GAME → výběr obtížnosti → jas → `Save Thread` →
  `STOP pc=0x201142e8: SAVE missing attribute: elmSearchType` = `openSaveList` (jediný builder; `getDirList` posílá
  `elmSearchType=0` vždy, `0x2011cfa0`; `openSaveList` jen v jedné větvi `0x2011f514`). → codex SAVEFULL
  (`local/ratchet/codex/batch/task-savefull.md`); diagnostický 339d (dump atributů, není oprava).

## První další akce

1. Ověřit předané soubory a procesy;379load i380load jsou ukončené, staré PID nerecyklovat.
2. Přečíst `codex/batch/spufp2-result.md` a `codex/spufp2/verification.json` pod `local/ratchet/`.
   Kandidát mění pouze `spu.py` a přidává test. Případnou integraci dělat v novém snímku, baseline SHA ověřit.
3. Po integraci celá regresní sada neměnného snímku včetně skutečného Vulkanu, nový herní build,
   stejná scéna a ustálené měření. Žádný slib FPS z mikrobenchmarku.
4. PPUMEM2 před integrací vyhodnotit podle hot-tracked regrese a reálné četnosti fallbacků;
   přenášet jen dodávku a minimální program.py hook. Hotovou izolovanou validaci bez důvodu neopakovat.
5. Pro další práci PIPE2 chybí úplná wall časová vazba konkrétního jobu na čekání. GDB snapshot ani
   globální profil nedokazují, že event polling této skupiny dominuje. Rešerše nabízí další obecné mechanismy.

## Příkazy

```bash
# Snímek zdrojů + build (vždy nové N), překlad s host ISA:
# Ze snímku: PYTHONPATH=local/ratchet/source-snapshot-N python -P … (bez -P bere `python -m` živý strom z cwd!)
python -P -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 1000000000000 -o local/ratchet/native-bundle-N
ANYPS3_EXTRA_CXXFLAGS='-march=native -ffp-contract=off' ANYPS3_RSX_VK_BUILD=1 PYTHONPATH=. \
  python local/ratchet/tools/virtual_pad.py build -j 16 local/ratchet/native-bundle-N local/ratchet/native-virtual-N
# Běh do menu + měření + DOWN + SIGKILL (zapíše verification.json); stejná binárka v kopii adresáře pro jiné režimy:
bash local/ratchet/tools/measure_menu.sh N 0-5,12-17 ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1
python local/ratchet/tools/menu_stats.py N 10
# Celá sada (4 procesy, skutečný Vulkan):
LP_NUM_THREADS=4 python local/ratchet/codex/rsxvk/rsxvk12/run_suite.py local/ratchet/suite-N
```

Pravidla měření: jen `ANYPS3_PERF_REPORT=1` (`ANYPS3_SPU_PROFILE` zkresluje SPU i hodiny); hra na CPU 0–5,12–17,
agenti/sada na 6–11,18–23 (druhý CCD). Program ukončovat `kill -KILL` (SIGINT/TERM zachytí SDL). Každý běh
`systemd-run --user --scope -p MemoryMax=24G`. Agenti: `codex exec … -m gpt-6.1-sol` ve scope s `MemoryMax`.
`pgrep/pkill -f` se vzorem z vlastního příkazu zasáhne i vlastní shell — zabíjet podle PID s kontrolou cmdline.
