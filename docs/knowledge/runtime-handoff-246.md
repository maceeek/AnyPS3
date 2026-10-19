# Předání AnyPS3 po 246

**Pozor: pozdější změny sdíleného workspace.** Při závěrečné kontrole se po 17:52 UTC změnily `anyps3/build_game.py`, `anyps3/program.py` a profil (nové `static_opd_section`); objevily se i artefakty `native-virtual-247`. Tyto změny nejsou prací tohoto běhu a nejsou ověřené našimi 175 testy. Zachovány bez zásahu. Hashe/testy tohoto předání se vztahují ke snapshotu 246, současný worktree už není totožný. Přesná odlišnost: `local/ratchet/handoff-246-worktree-drift.json`. Číslo 247 již není volné. Nejprve ověřit pozdější změny a stav případných cizích procesů; naše session 6422 je ukončená.

2026-10-14, Codex. Uživatel zrušil aktivní automatický cíl a požádal o ukončení
práce a úplné předání jinému AI agentovi. **Původní menu Ratchet & Clank:
A Crack in Time ani navigace gamepadem nejsou dosažené.** Při případném
pokračování zachovat celý původní cíl, neoznačit loading obrazovku za menu.

## Stav, od kterého pokračovat

**Poslední skutečný sémantický stop:** `local/ratchet/native-virtual-245`,
`pc=0x69dd30`, `lr=0x69dabc`, CTR `0x69dd30`, 308 156 479 PPU kroků,
`target has no statically translated block`. Je to případ 13 původního
čtrnáctipoložkového přepínače. Registr r0=r27=13, r7=`0x69db38`,
r10=`0x34`, r3=r6=`0x1f8`, TOC r2=`0xd34a88`, r30=`0xcacf80`.
FPSCR `0x86002000` obsahuje sticky ZX: původní hra skutečně vykonala
podporované FP dělení nulou. Konkrétní předchozí FP PC není přímo trasované.
Vlastní `verification.json` a nezávislý `terminal-identity.json` ověřeny.

**Připravená oprava tohoto stopu:** profil 246 obsahuje přesně ověřenou tabulku
`0x69db38`, count 14, dispatch `0x69db34`, 12 různých cílových bloků a 12
nových seeds. Základ/TOC, unsigned-byte bound 13, signed big-endian int32
offsety i všechny původní executable cíle ověřeny. Případ 13 `0x69dd30`
je přeložený v novém C++, **jeho vykonání hrou 246 ale ještě neověřeno**.

**Nejnovější hra 246 byla přerušena na pokyn uživatele.** Session 6422 ukončena
Ctrl-C, wrapper exit 130. Přímý guest returncode nebyl zaznamenán, nelze
jej zaměnit za 130. Žádný `STOP pc=...`, žádný normální `verification.json`.
Poslední stav: 266 čtení, 13 048 193 bajtů, draw 11520, 17 captures;
poslední snímek `native-virtual-246/frame-000017.png` stále načítací planeta.
Ovladač po celou dobu neutrální, hra hlásila `connected=1 backend=SDL2`.

`native-virtual-246/interruption.json` nezávisle ověřuje původní PT_LOAD,
vstupní a diagnostické hashe, současné source/profile, byte-identický base C++,
zachované logy a všechny 17 PPM. Je to **ověření přerušeného běhu**, nikoli
normální terminální runtime nebo menu důkaz. `TERMINATED.txt` označuje přerušení.

**Žádný námi zadaný build/run handle už není živý.** 6422 exit 130;
65441/34840 kompilace/build 246 exit 0; 6682 hra 245 ukončena;
50860/10130 kompilace/testy 245 exit 0. Staré pozorovací exec cells jsou
dokončené; žádný subagent nebyl zadán. `get_goal` při předání vrátil `goal=null`.
Samostatný shell `ps` v tomto prostředí má vlastní PID namespace a není
spolehlivým důkazem stavu jiných tool sessions. Nepoužívat staré logs jako
náhradu živého handle. Git metadata jsou prázdná/nepoužitelná; žádný nový
repozitář ani commit nevznikl.

## Co bylo změněno od 237

### Runtime a testy

1. `anyps3/rsx_draw.py`: jednoinstrukční FP MOV z inline konstanty vyhodnotí
   jednou přes stávající interpreter. Zachová coverage/top-left, clipping,
   depth/alpha, blending, masky a skutečné guest zápisy; ostatní shadery
   obecnou cestou. Nativní regrese `tests/test_rsx.py:250` ověřuje nezávislé
   barvy a pixely i obecnou větev. Syntetický benchmark 20 drawů 1024×512:
   611 131 → 267 785 µs, ~2,28×. **Není to rychlost celé hry.**
2. `anyps3/floating.py`: division a add/sub podporují infinities, finite/zero
   nastaví ZX a XOR znaménko, 0/0 a Infinity/Infinity nastaví VXZDZ/VXIDI,
   protilehlé infinities při add/sub VXISI. Správné FR/FI/FPRF, sticky FX
   při přechodu konkrétního exception bitu 0→1. Původní finite algoritmus
   nezměněn; unsupported rounding/NI/enabled traps dál explicitně zastavují.
   **Další instrukce s infinities nejsou tímto obecně vyřešeny.**
3. `tests/test_floating.py:34`: literální IEEE/FPSCR vektory, oba precision
   režimy, znaménka, subnormal/zero, NaN precedence, invalid případy, sticky
   flags, record CR1, destination aliases, odmítnutí unsupported modes bez
   změny stavu. Přesná původní pětice FRSP/FDIVS/FSUB/FSEL/STFS z
   `0x69f2b0..0x69f2c0` správně uloží guest big-endian `0x3f400000` (0,75).
   Před opravou stejný test selhal na původním division-by-zero stopu.

Původní PPU/SPU bajty, ELF ani herní assety nebyly patchovány. Nevkládal se
náhradní menu ani cizí emulator. Renderer zůstává vlastní software RSX
implementací s vlastním Vulkan readback runtime.

### Profil a původ statických cílů

| Sestavení | Ověřený původ | Nové seeds |
| --- | --- | ---: |
| 240 | vtable `0xc53248`, 14 původních OPD, TOC `0xd34a88` | 14 |
| 241 | vtable `0xc53208`, 14 OPD | 11 |
| 242 | původní šestipoložkový ctor `0x57765c`, čtyři zbývající vtables | 42 |
| 244 | sedm původních registrací callbacků `0x32c40c..0x32c464` | 7 |
| 246 | interní byte-indexed switch `0x69db38`, 14 slots / 12 targets | 12 |

Celkem **86 jedinečných nových profile seeds proti snapshotu 237**. Počet
seeds není počet nově vykonaných funkcí ani důkaz dokončení celého loaderu.
Šest vtables: `0xc53248`, `0xc53208`, `0xc53140`, `0xc531c8`, `0xc53188`,
`0xc53100`; celkem 85 ověřených OPD slots z původního konstruktoru.
Sedm callbacků: `0x53a244`, `0x53a644`, `0x53a520`, `0x53be88`,
`0x94f3b4`, `0x4ec0f4`, `0x4ebecc`, instalované do loader control blocku
`0xf46da4`. Podrobné load/store/OPD/TOC/kódové evidence níže.

### Virtuální gamepad a lokální nástroje

`local/ratchet/tools/virtual_pad.py`: explicitní SDL2 diagnostický obal.
Kopíruje původní translation byteově do `program.base.cpp`, přes macro
přejmenuje host main a připojí skutečné SDL virtuální zařízení. Původní
`NativePad` a guest `cellPad` rozhraní zůstávají vstupní cestou. Thread SDL
lock, stable instance-ID cleanup, atomický control file; chyby explicitně
zastavují, nevymýšlí neutralizaci ani připojený fyzický ovladač.

Control formát `mask LX LY RX RY LT RT`, neutral
`0 0 0 0 0 -32768 -32768`. Názvy down/up/left/right, cross/circle/square/
triangle, start/select, l1/r1/l3/r3. Samostatné nativní testy ověřily down+cross,
stick ±32767/−32768 a neutral 128, L2 pressure 255, big-endian data délky 20,
release a lifecycle; pět chybných vstupních případů. **Menu navigace neověřena,
fyzický gamepad neověřen.** Běhy 244–246 pouze neutrální, v jejich menu zatím
nebyla provedena žádná sekvence tlačítek.

Další lokální nástroje/evidence: `observe_run.py` (čte log, sám neprokazuje
živý proces), `verify_loader_vtables.py`, `verify_loader_callbacks.py`,
`add_vtable.py`, `add_jump_table.py`, `benchmark_constant_draw.py`,
`ppm2png.py` a `rsx_vp_disasm.py`. Reference repozitáře nebyly změněny.

## Skutečné běhy a neúspěšné pokusy

| Běh | Výsledek | PPU kroky | Čtení / bajty | Snímky |
| --- | --- | ---: | --- | ---: |
| 238 | nepřeložené `0x962700`, LR `0x577e00`, game rc 1 | 274 293 424 | 355 / 16 463 030 | 211 |
| 239 | zkompilované, hra nespouštěna; `NOT_RUN.txt` | — | — | — |
| 240 | druhý typ `0x9626a4`, LR `0x577e00`, r31=4, rc 1 | 274 249 019 | 355 / 16 463 030 | 28 |
| 241 | třetí typ `0x962404`, LR `0x577e00`, r31=8, rc 1 | 274 258 927 | 355 / 16 463 030 | 28 |
| 242 | allocation callback `0x53a244`, LR `0x57a7e0`, rc 1 | 274 257 993 | 355 / 16 463 030 | 28 |
| 243 | předčasný build se starým profilem přerušen, wrapper 130; hra neběžela | — | — | — |
| 244 | unsupported FP zero-divide `0x69f2b4`, LR `0x69f060`, rc 1 | 308 205 078 | 444 / 17 897 130 | 33 |
| 245 | nový interní case `0x69dd30`, LR `0x69dabc`, rc 1 | 308 156 479 | 444 / 17 897 130 | 33 |
| 246 | uživatelské přerušení Ctrl-C, wrapper 130, guest rc neznámý | neznámé | 266 / 13 048 193 | 17 |

244/245 otevřely oba původní `packed/levels/front_end/level_{cached,uncached}.psarc`.
SPU 0x100 v obou 878 746 602 instrukcí, waiting event, bez sémantického SPU stopu.
245: 15 252 flipů / 30 496 drawů / prezentace do okna 0. Všechny vizuálně
ověřené poslední obrazy stále loading planeta. Captures/clear/flips nejsou menu.
239 nebylo herně spuštěno; testovaná optimalizace skutečně použita od 240 dál.
`native-virtual-242` zkompilované staré diagnostické zařízení, hra nikdy
nespuštěna, `NOT_RUN.txt`; nepoužívat místo aktuálního 246.

## Testy a zdrojová identita

| Ověření | Výsledek / log |
| --- | --- |
| RSX cílené 239 | 6 testů OK, 10,481 s; `tests-rsx-239.log` |
| Celá sada 239 | 174 OK, 205,634 s, Vulkan required, bez skips; `tests-native-239.log` |
| FP nový test před opravou | očekávané selhání původního stopu; `tests-fp-245-before.log` |
| FP finální 245 | 6 OK, 10,194 s; `tests-fp-245-final.log` |
| Celá sada 245 | **175 OK**, 202,918 s, Vulkan required, bez skips; `tests-native-245.log`, `tests-native-245-result.json` |
| Virtuální pad | `virtual-pad-selftest-2.log`, manifest/zdroje/binárka v `virtual-pad-selftest-2/` |
| Pad chyby | 5 případů; `virtual-pad-selftest-1/error-checks.json` |

Od úspěšné celé sady 245 žádná runtime sémantická změna; 246 má shodné source
hashe. Tests neopakovat bez změny nebo konkrétní pochybnosti. Pouhé dokončení
dokumentace nevyžaduje další build/test. Hash inventář všech anyps3 zdrojů,
testů a použitých helperů: `local/ratchet/handoff-246-source-hashes.json`.

| Soubor / obraz | SHA256 |
| --- | --- |
| Původní `EBOOT.analysis.elf` | `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1` |
| `profiles/ratchet-acit.json` | `ab6d1c4fe7067ae93608ede9b034f261334e8ad1c88ba36ba714b1a34bb81db7` |
| `anyps3/rsx_draw.py` | `70ef9fff4439764ce625009fc90ce582fec28499ee0e3d1faf4b0c79fc825fec` |
| `anyps3/floating.py` | `e295daf36fc5155ae1d357931e6102322c28a67b939eacbdc37542bf2597dcab` |

Normální `run_native.py` verification hash neobsahuje base C++; vždy navíc
ověřit `diagnostic-manifest.json` a base proti původnímu bundle translation.
Menu flags v helperu jsou pevně false; při skutečném menu nestačí přepnout
flag, je třeba doložit původní viditelný obsah a změny navigace přes `cellPad`.

## První další akce a příkazy pro nový nepoužitý N

1. Po novém uživatelském pověření přečíst AGENTS, INDEX, CURRENT, toto předání;
   zkontrolovat aktuální soubory/hashe/profil/handles. **Teď žádný živý běh není.**
2. V novém adresáři znovu vykonat původní hru s připraveným profilem 246.
   Číslo zvolit podle aktuálních artefaktů; 247 je již obsazené.
   Předchozí `frames/` se nepřepisují: helper vyžaduje nový adresář.
3. Potvrdit průchod case 13 a řešit další skutečný stop. Načítání v našich
   posledních bězích trvalo zhruba desítky minut; nepřerušovat po pouhém
   timeoutu pozorování. Rozpočet zůstává 5 mld. PPU/SPU, není opravou ISA.

N níže nahradit skutečně volným číslem až po kontrole pozdějších změn. Šablona nebyla provedena.

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 5000000000 -o local/ratchet/native-bundle-N > local/ratchet/build-N.log 2>&1
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-N local/ratchet/native-virtual-N > local/ratchet/build-virtual-N.log 2>&1
ANYPS3_VIRTUAL_PAD_CONTROL=/home/martink/AnyPS3/local/ratchet/native-virtual-N/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-N 5000000000 > local/ratchet/run-virtual-N.log 2>&1
python local/ratchet/tools/observe_run.py native-virtual-N --record
```

Wrapper kompiluje `c++ -std=c++17 -O2 -Wall -Wextra -Werror -pthread
-DANYPS3_VULKAN ... -lvulkan`, do `native-virtual-N/program`; původní
`native-bundle-N` obsahuje generované C++/ELF/profile/build, nemá vlastní
zkompilovaný program. `run_native.py` aktuálně nastaví skutečný disc
`game-complete`, cache, HDD, SPU budget, Lavapipe a Vulkan validation.
Jeho obsah znovu ověřit. Zachovat stdout/stderr/returncode/stop/captures.

Po skutečném menu lze tlačítka poslat přes SDL a pak uvolnit (příkazy zatím
nebyly v herním menu provedeny):

```bash
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-N/control.txt --buttons down
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-N/control.txt
```

Doložit změnu výběru v původním menu, návrat/aktivaci podle potřeby, původ
inputu a before/after snímky. Oddělit virtuální navigaci, fyzický gamepad
a prezentaci do okna. **Window presentation backend stále chybí**, nynější
Vulkan cesta je headless scanout/readback/capture. Zvukový backend také chybí.
Další omezení shaderů/textur/stencil/MRT jsou v starším RSX experimentu.

## Poslední rozpracovaná rešerše – nepoužít jako hotovou opravu

Read-only scan 64 přeložených původních BCTR našel další byte-shift patterns;
`local/ratchet/translated-byte-switch-candidates-246.json` je pouze katalog
**hypotéz/kandidátů**, bez změny profilu/generátoru. Není úplně prokázaný
TOC/register flow ani správný počet všech jeho tabulek.

| Dispatch / kandidátní table | Kandidátní count | Stav |
| --- | ---: | --- |
| `0x50c774` / `0x50c778` | 6 | cíle v 246 nepřeložené, další ověření nutné |
| `0x50d810` / `0x50d814` | 5 | totéž |
| `0x7d87a4` / `0x7d87a8` | 5 | totéž |
| `0x9b52fc` / `0x9b5300` | 10 | totéž |
| `0x5f99a8`, `0x6dc070`, `0x90acd8`, `0x91628c` | neprokázaný | regex/pattern sám nestačí |

Známé `0x69db38` a starší `0x90a294` už v profilu jsou. Automatický detector
zůstává strict canonical `rldic`; nebyl rozšířen na `rlwinm`. Tyto kandidáty
nepřidávat bez ověření původních guard/base/offset/count/targets a identity.

## Rozcestník důkazů a historie

- [238: původní skutečný stop a SPU](experiments/2026-10-14-loading-238.md)
- [239: renderer, benchmark, testy](experiments/2026-10-14-constant-fragment-239.md)
- [240: první vtable](experiments/2026-10-14-vtable-240.md)
- [241/242: šest vtables](experiments/2026-10-14-loader-vtables-241-242.md)
- [244: callbacky, virtual pad, skutečný FP stop](experiments/2026-10-14-virtual-pad-and-callbacks-244.md)
- [245: FP sémantika a regrese](experiments/2026-10-14-fp-zero-divide-245.md)
- [246: table a přerušení](experiments/2026-10-14-byte-switch-246.md)
- [Předchozí posun 198–237](runtime-handoff-237.md), [stručná historie](../menu-progress.md)
- [RSX sémantika a omezení](experiments/2026-10-14-rsx-software-draw.md),
  [PRX/trophy/PPU detaily](experiments/2026-10-14-prx-import-references.md)
- [Primární zdroje, přesné cesty a revize referencí](SOURCES.md)

Evidence soubory v `local/ratchet/`: `target-240-evidence.txt`,
`target-241-evidence.txt`, `loader-vtables-242-evidence.json`,
`target-244-evidence.json`, `target-245-evidence.json`, `target-246-evidence.json`.
Původní firmware 3.01 a herní obrazy musí zůstat stejné; žádné tiché záměny.
Primární PDF knihovna eviduje verze/hash/page mapping. Book I v2.02 byl
při FP rešerši dostupný přes web, lokální download selhal DNS a screenshoty
většinou cache miss; **neexistuje nový lokální Book I PDF hash**. Nepřebírat
novější Power ISA nebo PowerXCell detaily jako automaticky platné pro PS3.
