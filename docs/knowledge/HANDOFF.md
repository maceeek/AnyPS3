# Předání další instanci

Předávající doplní CURRENT a odkazy na ověření. Přebírající zkontroluje pracovní strom, otevře poslední log a ověří cesty i profil. Předání není požadavek na nový plán: pokračuj od nejbližší konkrétní otázky.

## Předání 2026-10-19 večer — vlna FAST (Claude Opus 5.5, goal: 60 FPS s rezervou)

Stav a důkazy: [záznam FAST](experiments/2026-10-19-fast-wave.md) — sekce „Stav na konci session“ obsahuje
**první další akce**. **Nejlepší ověřený build: `native-virtual-460`** (snímek 460 = 386 + RSX2; 386 = 380 + PPUFAST +
RSXFAST 443 + SPUFAST 421), úroveň **22,59 FPS** (380: 14,25). Úzké hrdlo RSX worker (~28 ms/snímek), pak GPU.
60 FPS nedosaženo. Session ukončena na pokyn uživatele 2026-10-19 ~22:00; žádný agent, build, sada ani hra neběží.
**Živý strom (`anyps3/`, `tests/`, `profiles/`) = snímek 460** (povýšeno na pokyn uživatele, ověřeno proti
`native-virtual-460/build.json` source_sha256) a je v gitu (`main`, origin `maceeek/AnyPS3`). Další integraci skládat
přes `integrate.py` ze snímku 460 (nebo novějšího) a výsledek po ověření promítnout do živého stromu a commitnout.
Neintegrováno a hrou neověřeno: `work/spufast/tree` po 422 (oprava par1/par2 + par_worker), `work/rsx2/dev-after-460.patch`.

- **Nástroje:** `local/ratchet/tools/measure_level.sh SRC NAME` (menu → LOAD → úroveň, okno 60 s, `level-summary.json`,
  zámek `.run.lock`, výchozí `GAP=8`), `make_worktree.sh`, `agent_build.sh` (zámek `.compile.lock`), `integrate.py` +
  `integrate_build.sh` (integrační snímky; **nikdy nepředávat seznam souborů přes zsh proměnnou** — 381/382 neplatné),
  `run_suite.py` (celá sada z cwd), `watch_fast.sh` (monitor).
- **Agenti:** pracovní stromy `local/ratchet/work/<úkol>/{base,tree}`, zadání `TASK.md`, stav `STATUS.md`, výsledek
  `RESULT.md`, pravidla `work/COMMON-FAST.md`. PPUFAST a RSXFAST uzavřeny (RESULT.md), SPUFAST a RSX2 uzavřeny (RESULT.md). Uživatel: dál preferovat Sonnet subagenty (limit API).
- **Další integrace:** nový snímek z 386 + `work/rsx2/tree` + novější SPUFAST snímek (42x) přes `integrate.py`.

## Předání 2026-10-19 — na výslovný pokyn uživatele po tomto kole

**Cíl zůstává původní hratelná úroveň ACIT s funkčním gamepadem a s rezervou 60 FPS. Není splněn.**
Uživatel požádal ukončit toto kolo, zapsat výsledky a pokračovat v jiném AI prostředí.
Aktivní goal následně zrušil; žádná automatická další kola nezahajovat.
Žádné náhradní menu/emulátor ani patch herních bajtů; zvuk vždy `SDL_AUDIODRIVER=dummy`
a `ANYPS3_AUDIO_SINK=null`, nikdy povolit AUDIBLE.

- **Strom:** `source-snapshot-380` =377+HLEFAST1+oprava scheduler deadline; živé Python soubory/profil
  při kontrole přesně shodné. `build380-handoff-audit.json` ověřuje117 source hashů a binárky.
  Buildy379 i380 `rc=0`, zdroje shodné se svými snímky. Git metadata zde nedostupná, neinicializovat.
- **379load ověřeno:** původní menu → LOAD → `great_clock_a`, Clank pohyb virtuálním padem,
  `evidence-play-montage.png`. Dvouminutové ustálené okno60 intervalů: **14,64 FPS**, min13/max16,5;
  `measurement-steady.json`, profiler `profile-379load.txt`, `sample-profile-379load-T1/-T2`.
  Po měření řízeně SIGKILL; verification−9, stop null, oba původní PT_LOAD shodné;49 captures.
  Ruční vizuální/input posouzení v `acceptance.json` odděleně od generických false příznaků verifieru.
- **380load ověřeno a ukončeno:** původní menu → LOAD → `great_clock_a`, Clank se pohybuje virtuálním padem
  (`evidence-input-montage.png`, `acceptance.json`). Ustálené okno67 intervalů/134,067s:
  **15,0154 FPS**, medián15,0, min13,5/max16,0. `measurement-steady.json`, `profile-380load.txt`.
  Bez guest stopu, po měření a vstupu SIGKILL ověřeného PID120207; verification−9/stop null,
  oba původní PT_LOAD shodné,34 captures. Rozdíl+2,6% proti379 není z těchto dvou běhů průkazný zisk.
- **Integrovaná oprava:** `anyps3/threads.py` počítá nejbližší deadline i při aktivním guest audiu;
  dřívější100µs spánek nesmí překrýt30µs PPU timer. Není to přeskočení guest čekání.
  `tests/test_scheduler_idle_deadline.py`:11 deterministických native scénářů PASS, starý runtime RED.
  Opraven také zastaralý atexit test, který chybně žádal stop místo správného wait_kind13.
  Cílená37922/23PASS, opravený atexit modul2/2PASS. **Celá sada sloučeného380 dosud neprovedena.**
- **Nový hlavní důkaz PIPE2:** engine counters group2 i4 dokončuje SPU přes MFC PUTLLUC,
  nikoli potvrzený RSX label. Group2 SPU0x02000200/frontier0xfc7d00,
  group4 SPU0x04000200/frontier0xfc7e00, společný originpc0x3f9e8.
  Issue counter zapisuje PPU1 stw0x696524. Capture JSON/logy a původní bajty:
  `local/ratchet/codex/pipe2/`, report `codex/batch/pipe2-result.md`.
  Ve380 group4 právě vykonávala původní mikroprogram0x27: EA0xbbf480, živé LS0x26c00,
  všech5264B shodných s původním EBOOT (`job380-proof.json`); FP normalizační instrukce.
  Lidský název nadřazeného jobu a jeho celý begin→end interval nejsou doložené.
  Vazba konkrétního counteru na dominantní27ms burst není samotným GDB časováním prokázaná.
- **PPUMEM2, neintegrované:** hotový report `codex/batch/ppumem2-result.md`.
  Zbytek sady58 modulů/219 testů PASS, finální5TSan+2UBSan PASS;10 společných HLE epoch scénářů
  a2 AOT/worker kontroly PASS. Epochová cesta má~2× zrychlení běžného scalar/AOT případu,
  ale hot-tracked AOT **5,297→7,768ns/instr (+46,6% času)**. Neoznačit kandidáta za plošně rychlejší.
  Žádný herní build/FPS kandidáta není ověřen. Dodávka a manifesty `codex/ppumem1/ppumem2-*`;
  před integrací číst instrukce, přenést minimální `program.py` hook, nikoli celý starší soubor.
- **SPUFP2, neintegrované:** dokončený kandidát v `codex/spufp2/`; report
  `codex/batch/spufp2-result.md`,27 cílených testů PASS (Fraction/exact oracle, generic/native/UBSan,
  MXCSR obnova, aliasy, FPSCR, čítače). Patch `owned-changes.patch` mění pouze `anyps3/spu.py`
  a přidává `tests/test_spufp2.py`. Helpery~1,5×, syntetický AOT1,31–1,37×; nejde o herní FPS.
  All-exact scénář měl jednu pomalejší dvojici0,897×, není záruka absence každé regrese.
  Přesné SIMD masky bez odstranění
  MXCSR guardu nebo exact fallbacku. V kritické group4 tvoří single_vector+clones~27,95% CPU,
  exact round2,06%; event polling není doložený dominantní problém této skupiny.
- **Ovladače:** v tomto kole přímo ověřen virtuální pad379 i380; uinput ve hře doloženo starším363,
  fyzický gamepad v tomto kole neověřen. 60 FPS není dosaženo.

Podrobné změny, příkazy a odkazy: [záznam379+](experiments/2026-10-19-performance-379.md).
Všechny výše zkrácené cesty `codex/`, `profile-*`, `source-snapshot-*` jsou pod `local/ratchet/`.

**Ověření předání:** `local/ratchet/handoff380-final-audit.json` znovu porovnal117 source hashů,
runtime artefakty, živé Python/JSON proti snímku380,44 PIPE2 manifest položek a19 SPUFP2 artefaktů:
vše bez rozdílu. PPUMEM2 sedm dodaných souborů bylo samostatně porovnáno s manifestem.
Generický verifier má `menu_reached=false` jako výchozí pole; ruční obrazové a vstupní posouzení
je proto zvlášť v `native-virtual-{379,380}load/acceptance.json`, bez přepisování původního verifieru.
Soupis41 hlavních dokumentů a důkazů se SHA256: `local/ratchet/handoff380-index.json`;
ověřeno62 místních Markdown odkazů bez chybějícího cíle. Velké herní/snapshot artefakty zůstávají
na původních cestách a jejich vlastní manifesty se zachovaly.

**Procesy:** host audit 2026-10-19 09:59:29 UTC v `local/ratchet/handoff380-process-audit.json`
potvrdil nulové vlastní herní/build/debugger procesy a nulové běžící `anyps3-*` scopes.
PID104347 i120207 už neexistují. Všechny runtime/test tool sessions skončily; při převzetí ověřit znovu.

**Prostředí:** tato relace měla skutečně `workspace-write`, omezenou síť a zápis jen do projektu a/tmp,
přestože uživatel dříve volil Full access. Host procesy a GUI proto vyžadovaly eskalaci.
Jde o oprávnění aktivní relace, nikoli chybu hry; konfigurace oprávnění se zde neměnila.

**První další akce:** ověřit tento stav a zvolit malý kandidát SPUFP2 pro nový izolovaný snímek
(nejprve baseline SHA `spu.py`, pak jeden `owned-changes.patch`). Po integraci celá regresní sada
včetně skutečného Vulkanu, nový build a stejné měření úrovně. PPUMEM2 zvažovat zvlášť kvůli hot-tracked
regresi; přenášet minimální program hook, nikdy celý starý `program.py`. Ani jeden kandidát neintegrovat
pouze podle syntetického násobku. PIPE2 další otázka: časově spojit úplný job s čekáním bez GDB perturbace.

**Výslovně vyžádaná webová rešerše:**
[skutečné PS3 recomp projekty, revize, licence a použitelné mechanismy](experiments/2026-10-19-web-ps3-recomp.md).
Agent našel6 konkrétních PS3 portů. Nejsilnější jsou veřejné alfa sestavy
[Drakengard3](https://github.com/soyjxck/dod3-recomp/releases/tag/v0.2.5-alpha)
a launcher [Soulcalibur II HD Online](https://github.com/n0madic/Soulcalibur2HD-recomp).
Existence kódu/release je ověřená; uváděná hratelnost/FPS jsou tvrzení autorů, zde se porty nespouštěly.
34 veřejných podkladů se SHA/URL/revizí: `local/ratchet/codex/web-ps3-recomp/manifest.json`,
validace v `validation.json`; main inventář znovu přepočítal.

Pět relevantních oblastí: PPU stránkové příznaky pro rychlé přístupy; registry v lokálech a analýza
změněného/živého stavu; přesné SPU FP a nezávislé orákulum; RSX write-watch cache a dávkování;
SPURS přidělování práce a invarianty probuzení. Přesné cesty a pevné revize obsahuje rešerše.
**Přínos pro AnyPS3 je hypotéza.** Drakengard3/MIT fork navíc mění konkrétní hru a některé SPU úlohy;
takové byte patches či náhradní logika nejsou slučitelné se zdejšími pravidly. Soulcalibur zdroj je
GPL2-or-later, distribuovaný celek GPL3-or-later. Rešerše nic neintegrovala a pořadí pokračování
výše nemění. Všichni zadaní pracovníci včetně rešerše dokončili výstupy; nezůstal otevřený vedlejší úkol.

## Předání 2026-10-18 ~22:25 — konec relace (Claude Opus 5.5)

Čti [CURRENT.md](CURRENT.md) a [záznam 338+](experiments/2026-10-17-ingame-338.md) od sekce „374load".

- **ABSOLUTNÍ PRAVIDLO:** z PC nikdy slyšitelný zvuk — jen `SDL_AUDIODRIVER=dummy`, `ANYPS3_AUDIO_SINK=null`;
  nikdy `ANYPS3_AUDIO_AUDIBLE=1`. Platí i pro codex agenty (`local/ratchet/codex/batch/COMMON.md`).
- **Poslední ověřený build:** `local/ratchet/native-virtual-377` (snímek `source-snapshot-377` = 375 + oprava
  souběžného `sys_ppu_thread_exit` + SPUDYN2 v2). Běh `native-virtual-377load`: menu → LOAD → úroveň great_clock_a,
  **~14,8 FPS stabilně** (120 s: 13,0–16,5), žádný stop; gamepad ve hře ověřen dříve (363, virtuální i uinput).
  Cíl 59,94 FPS nesplněn.
- **Řetěz dnes:** 373 (9,16 FPS) → 374 PROF1 sampler + PAR15 (~10) → 375 RSXVK22 v24 (stop atexit při bootu) →
  377 atexit + SPUDYN2 (~14,8). 376 diag neprošel překladem, 378diag = časová osa PIPE1 (jen diagnostika).
- **Živý strom ≠ poslední build:** = 377 + **HLEFAST1** (`services.py`, `lightweight_sync.py`, `program.py` hunks +
  `hle_perf_report`, `tests/test_hlefast1.py`); cílené testy OK (38 + `test_services` 3/3 izolovaně), **herní build
  ještě neproveden** → první akce: `bash local/ratchet/tools/snapshot_build.sh 379 377 "HLEFAST1 …" anyps3/services.py
  anyps3/lightweight_sync.py anyps3/program.py tests/test_hlefast1.py`, pak běh jako 377load.
- **Běh do úrovně (vzor):** `(bash local/ratchet/tools/run_to_menu_ptrace.sh N NAME 0-5,12-17 SDL_AUDIODRIVER=dummy
  ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1 ANYPS3_PRESENT=sdl SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS=1
  ANYPS3_SAMPLE_PROFILE=$PWD/local/ratchet/sample-profile-NAME && bash local/ratchet/tools/to_load.sh
  local/ratchet/native-virtual-NAME 330) > local/ratchet/drive-NAME.log 2>&1 &`; hru ukončit `kill -KILL` po kontrole
  `comm == program`. Profil okna: `local/ratchet/profile-374load/sample_window.py T1 T2 PROGRAM BUILD` (kopie adresáře
  sampleru v čase T1/T2), po vláknech `sample_threads.py`. Časová osa: `ANYPS3_FRAME_TIMELINE` jen v diag buildu.
- **`snapshot_build.sh`** nově umí `SRC=<pracovní strom>` (diagnostické buildy bez zásahu do živého stromu).
- **Agenti (codex gpt-6.1-sol), výsledky v `local/ratchet/codex/batch/<úkol>-result.md`:** PROF1, PAR15, RSXVK22,
  HLEFAST1 hotovo a integrováno; SPUDYN2 integrováno (jeho sada přerušena, 380/733 bez selhání); **PIPE1** (fáze B:
  publikace labelů/fence vs. čekání hry 0x696c10, latence `usleep(30)` ~151 µs) a **PPUMEM1** (epochová rychlá cesta
  PPU paměti, ~35 % CPU PPU) dostaly pokyn k ukončení se zápisem stavu — jejich dodávky jsou neúplné/neintegrované.
  Pokračování konverzace agenta: `codex exec resume --skip-git-repo-check -c 'sandbox_mode="workspace-write"'
  -c 'model="gpt-6.1-sol"' <session id z batch/run-<úkol>.log> "<pokyn>"`.
- **Obecnost:** uživatel chce budoucí kompatibilitu s dalšími hrami — preferovat obecné mechanismy (obsahové
  přiřazení, cache, JIT) před per-game daty v profilu (COMMON.md).

## Předání 2026-10-18 ~10:55 (Claude Opus 5.5 → codex gpt-6-astra, ~1 h)

Čti [CURRENT.md](CURRENT.md), [záznam 338+](experiments/2026-10-17-ingame-338.md) (sekce 359load → 360/361) a
zadání `local/ratchet/codex/batch/task-astra-main.md`. Hra je v úrovni na ~1,5 FPS; hlavní práce = výkon RSX
(RSXVK22) + chyby renderingu (RSXFIX1) + důkaz gamepadu ve hře. Nástroje: `local/ratchet/tools/snapshot_build.sh`
(snímek+build s cache objektů), `to_load.sh`, `to_play.sh` (`FT=` frame trigger), `stack_sample.py`.

## Nejnovější předání 2026-10-17 večer (Claude Opus 5.5)

Čti [CURRENT.md](CURRENT.md) a [záznam 338+](experiments/2026-10-17-ingame-338.md).

- **ABSOLUTNÍ PRAVIDLO:** z tohoto PC nikdy žádný slyšitelný zvuk (spí tu miminko) — jen SDL `dummy`/`disk`, pojistka
  v `anyps3/native_audio.py`, sinky PipeWire ztlumené. Platí i pro všechny subagenty (`codex/batch/COMMON.md`).
- **Build:** vždy ze snímku `PYTHONPATH=local/ratchet/source-snapshot-N python -P …` a každá kompilace/sada/běh ve vlastní
  `systemd-run --user --scope -p MemoryMax=… -p MemorySwapMax=0` (OOM v 17:07 shodil session). Když agent mění živý
  strom, snímek skládej z izolované kopie (snímek N-1 + vlastní soubory).
- **Poslední ověřený build:** `native-virtual-347` (snímek 347 = 345 + SPUPERF2 + diagnostika). Poslední stop:
  347serial `SPU code differs` (SPU 0x3000200 pc 0x5680, kód z EBOOT 0xdd3220 na LS 0x5600); paralelní SPU režim
  uvízne dřív (ztracené LR probuzení, hypotéza). Nástroje: `run_to_menu_ptrace.sh` (`STRIDE=`, `PAD=uinput`),
  `to_game.sh`, `uinput_pad.py`, `ANYPS3_DIAG_WATCH`, `ANYPS3_SPU_LS_DUMP_DIR`, SIGUSR1 = dump PPU+SPU (+LS).
  Cesta LOAD: v menu se savem stojí kurzor na LOAD → CROSS → systémový seznam → CROSS.
- **Agenti:** SPUDYN (`task-spudyn.md`, základ 346) a AUDIOFULL2 (`task-audiofull.md`, jen tiše) — stav viz konec
  předání/CURRENT; výsledky v `local/ratchet/codex/batch/{spudyn,audiofull}-result.md`.
- **Živé procesy při předání (2026-10-17 ~22:35):** jen codex agenti **SPUDYN** (scope `anyps3-spudyn`) a
  **AUDIOFULL2** (scope `anyps3-audiofull2`) dobíhají závěrečnou sadu; uživatel je nechal doběhnout. Žádná hra, build ani
  gdb neběží. **Živý strom proto obsahuje jejich změny navíc proti snímku 347** (`spu*.py`, audio soubory, testy) —
  příští integrace: ověřit `owned-files.json`, skládat snímek 348 z izolované kopie 347 + jejich dodávek.
- **Úklid:** `local/ratchet/_trash-2026-10-17/` (karanténa ~197 GiB, `MANIFEST.txt`), smaže uživatel; archiv starých
  logů `local/ratchet/archive/`. Velké logy běhů jsou `*.log.xz`.
- **První akce:** viz CURRENT „První další akce“ (SPUDYN → LR wakeup → AUDIOFULL2 → skutečná hra a FPS).

## Nejnovější předání 2026-10-17 odpoledne (Claude Opus 5.5)

Čti [CURRENT.md](CURRENT.md) a [záznam 332+](experiments/2026-10-17-menu-fps-332.md).

- **Poslední ověřený stav:** build `local/ratchet/native-virtual-337` (snímek `source-snapshot-337`, sada 614/614),
  `ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1` → původní menu medián 58,96 FPS (2 min, `native-virtual-337long`),
  navigace virtuálním padem 10/10 (`native-virtual-337tap`). Běží/proběhl codex PAR12 (`task-par12.md`).
- **Build ze snímku:** `PYTHONPATH=local/ratchet/source-snapshot-N python -P -m anyps3.build_game …` a
  `python -P local/ratchet/tools/virtual_pad.py build …`; po buildu porovnat `build.json:source_sha256` se snímkem
  (bez `-P` se bere živý strom, kde pracují agenti — viz záznam).
- **Nástroje:** `local/ratchet/tools/{profile_menu.sh,stack_sample.py,stall_probe.sh,hang_probe.sh,nav_check.sh,
  nav_strip.py,menu_table.py,run_to_menu_ptrace.sh}`, `tools/ptracer/libptracer.so` (gdb/eu-stack při Yama 1).
- **Fyzický gamepad:** běh bez `ANYPS3_VIRTUAL_PAD_CONTROL` použije SDL GameController (hotplug v `NativePad::poll`);
  nepřipojen → neověřen.

## Předání 2026-10-17 dopoledne (Claude Opus 5.5)

Čti [CURRENT.md](CURRENT.md) a [záznam 311–331](experiments/2026-10-16-oom-alias-fps-311.md) (sekce od „PAR8 + RSXVK19
→ 331“ zpět). Cíl: původní menu na 59,94 FPS s gamepadem, pak skutečná hra.

- **Poslední ověřený stav:** build `local/ratchet/native-virtual-331` (snímek `source-snapshot-331`, sada 583/583),
  `ANYPS3_RSX_WORKER=1 ANYPS3_SPU_THREADS=1` → menu **38–40 FPS** (`native-virtual-331tw`), sériově 25,8 FPS.
  Žádný guest stop; běhy ukončeny SIGKILL po měření (verification −9, PT_LOAD shodné).
- **Výstupy agentů:** `local/ratchet/codex/batch/{par0..par8,rsxvk12..rsxvk19}-result.md`, jejich `DESIGN.md`/
  `INTEGRATION.md` v `local/ratchet/codex/par*/` a `local/ratchet/codex/rsxvk/rsxvk*/`. Herní profily
  `local/ratchet/codex/rsxvk/game-profiles/menu-3*.{prof,txt}`; captures `local/ratchet/codex/rsxvk/capture-3{19,22,24}-menu`.
- **Nástroje:** `local/ratchet/tools/{measure_menu.sh,to_menu.sh,menu_stats.py,gperf_report.py}`,
  `virtual_pad.py` (`ANYPS3_EXTRA_CXXFLAGS`).
- **Živé procesy při předání:** žádné (agenti, buildy, běhy i memwatch ukončeny).
- **První akce:** profil 331tw (hlavní + RSX vlákno saturované), pak další vlna (CPU rendereru na worker cestě,
  koordinátor/PPU aktivní čekání, PPU2). Fyzický gamepad ověřit po připojení.

## Předání 2026-10-14 večer (Claude Opus 5.5)

Čti [CURRENT.md](CURRENT.md) a
[záznam 247–255](experiments/2026-10-14-full-opd-coverage-247-250.md). Starší předání (odkazy níže) jsou překonaná
(247 už použito, 246 profil ověřen během 247). Výstupy codex subagentů: `local/ratchet/codex/*-result.md`,
`switches-250.md`, rešerše importů `experiments/2026-10-14-import-research-250.md`.
První akce: rozdělit generovaný C++ do více TU (kompilace je hlavní brzda vývoje), pak běh s profilem 255.

## Šablona experimentu nebo předání

```text
Datum a autor/model:
Cíl a kritérium dokončení:
Zdrojový stav: commit + necommitované soubory/hashes
Vstup: profil, identifikace hry, relevantní konfigurace
Ověřený fakt: co přesně bylo pozorováno
Zdroje: soubor:řádek @ commit / titul-verze PDF page N + SHA256
Runtime: přesný příkaz, log, exit/timeout, artefakty a čítače
Změny: co je hotové a kde
Hypotéza: důvod, alternativa, rozlišující test
Vyvráceno: pokus a důvod
Otevřeno: co zatím nevíme
Další krok: konkrétní první akce
Omezení: co test nepokrývá
```

## Kontrola před převzetím

- Cesty skutečně existují; referenční checkout má zaznamenanou revizi.
- Fakt vychází z konkrétního zdroje nebo runtime artefaktu.
- Výsledek odpovídá kritériu původního menu a gamepadu; diagnostická prezentace není zaměněna za guest rendering.
- Neúspěšné pokusy a hypotézy jsou viditelně oddělené od potvrzeného chování.
- Další krok řeší skutečný poslední blokátor, nikoli dávno odstraněnou chybu.


## Starší předání

Předání 238–246 (překonaná): [runtime-handoff-246.md](runtime-handoff-246.md),
[runtime-handoff-237.md](runtime-handoff-237.md), [runtime-handoff-198.md](runtime-handoff-198.md).
Plné předchozí znění tohoto souboru: `local/ratchet/HANDOFF-before-cleanup-2026-10-14.md`.
