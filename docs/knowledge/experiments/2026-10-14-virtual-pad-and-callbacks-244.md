# Callbacky loaderu a skutečný SDL virtuální vstup (244)

Datum: 2026-10-14. Původní menu ACIT a navigace gamepadem zatím neověřeny.

## Konečný ověřený běh 242

Session 31385 ukončena, wrapper exit 0, hra returncode 1. Nový stop
`pc=0x53a244`, `lr=0x57a7e0`, 274 257 993 PPU kroků. 355 čtení,
16 463 030 bajtů, SPU 0x100 755 183 648 instrukcí a čeká na událost,
12 232 flipů, 24 456 drawů, 28 zachycených snímků, prezentace do okna 0.
Vlastní `verification.json` 242 a všech šest artefaktových hashů znovu
ověřeny; oba původní PT_LOAD úseky souhlasí.

Loader dosáhl těla třetího typu po readiness metodě `0x962404`.
R31=8, r8=`0xc53140`; nejde ještě o dokončení všech šesti typů.
R9=`0xd0efb0`, r6/CTR=`0x53a244`, TOC=`0xd34a88`, r7=`0xf46db0`.
Původní `0x57a7a0..0x57a7dc` sestavuje argumenty count=`0x136`,
element-size=4, flags=`0x20` a volá callback přes toto původní OPD.

## Ověřené callbacky a profil 244

Původní inicializace `0x32c40c..0x32c464` načte sedm OPD z constant-pool
cells `0xc94440..0xc94458` a volá jednotlivé settery. Ty je instalují
do control blocku `0xf46da4` na offsety +0xc, +0x10, +0x14, +0x1c,
+0x24, +0x28, +0x2c. První odpovídá aktuálně dosaženému poli `0xf46db0`.
Nejde o odhad podle sousedních OPD: evidence obsahuje původní load, BL,
setter, TOC/bias a store pro každou položku.

```bash
PYTHONPATH=. python local/ratchet/tools/verify_loader_callbacks.py --apply > local/ratchet/target-244-evidence.json 2> local/ratchet/callbacks-244.log
```

Ověřen pinned původní ELF SHA-256
`6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`,
všech sedm OPD, TOC `0xd34a88` a executable kódové bajty. Profil obsahuje
novou `static_function_tables` položku s původem a skutečně pozorovaný
`observed_indirect_entries` záznam. Přidáno sedm seeds: `0x53a244`,
`0x53a644`, `0x53a520`, `0x53be88`, `0x94f3b4`, `0x4ec0f4`, `0x4ebecc`.
Další callbacky jsou ověřeny z registrace, nikoli vydávány za vykonané.
Herní bajty se nemění, nové host služby nebyly zavedeny.

243 bylo zahájeno před úspěšnou kontrolou profilu a přerušeno přes session
63268, exit 130. Chybný helper assert vyžadoval jen callee successor,
zatímco BL má callee i návratové pokračování. Selhal před úpravou profilu;
243 obsahuje starý profil a hra v něm neběžela. Artefakty jsou zachovány,
`native-bundle-243/TERMINATED.txt`. Opravená kontrola ověřuje oba cíle.

## Diagnostický virtuální gamepad

`local/ratchet/tools/virtual_pad.py` vytváří explicitně opt-in diagnostický
obal. Kopíruje původní přeložený C++ jako `program.base.cpp` bez změny
bajtů; přejmenuje pouze host main přes preprocesor a před jeho zavoláním
připojí SDL2 virtuální GameController. Žádný guest kód, paměť, menu nebo
asset nepatchuje ani nenahrazuje. Normální runtime SDL backend zůstává
zdrojem `cellPad` výsledků. `diagnostic-manifest.json` eviduje původ,
wrapper/base/binary hashe; původní build metadata jsou navíc zachována.

Control file: sedm desítkových polí `mask LX LY RX RY LT RT`; 15 SDL
buttons, šest int16 axes. Neutrální stav: `0 0 0 0 0 -32768 -32768`.
Atomický zápis zajistí subcommand `set`. Tlačítka mají jména down/up/left/right,
cross/circle/square/triangle, start/select, l1/r1/l3/r3. Neplatná data
explicitně zastaví diagnostiku; žádná implicitní neutralizace při chybě.

API zdroje: instalované `/usr/include/SDL2/SDL_joystick.h:132` (lockování),
`:370` (attach), `:467` (axis), `:485` (button); header verze 2.32.74
z `SDL_version.h:59`. [SDL2 LockJoysticks](https://wiki.libsdl.org/SDL2/SDL_LockJoysticks)
vyžaduje lock při přístupu z více vláken, [SetVirtualButton](https://wiki.libsdl.org/SDL2/SDL_JoystickSetVirtualButton)
publikuje nastavené hodnoty při dalším joystick update. Input worker používá
SDL lock a hodnoty spotřebovává původní `NativePad::poll`. Při cleanup
vyhledává zařízení podle stabilního instance ID, nikoli starého indexu.

```bash
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py self-test local/ratchet/virtual-pad-selftest-2
```

Ověřeno nativně: init/connected virtuálního zařízení, přesné guest big-endian
`cellPad` button words (down+cross), levá páčka +32767→255 a −32768→0,
neutrál 128, L2 pressure 255, délka 20, release a lifecycle. Log
`local/ratchet/virtual-pad-selftest-2.log`, zdroje/binárka/manifest ve stejnojmenném
adresáři. **Je to vstupní test, nikoli menu nebo fyzický gamepad.**
Předchozí selftest-1 také ověřil pět chybových případů (mask mimo rozsah,
chybějící axis, trailing token, chybějící opt-in, neznámé jméno bez přepsání
control file): `virtual-pad-selftest-1/error-checks.json`. Reader se od té
doby nezměnil; změnil se lock/index lifecycle a znovu prošel selftest-2.

## Nová sestava a probíhající skutečná hra

Runtime hash sada odpovídá úspěšné celé sadě 174 testů s povinným Vulkanem
z 239; další runtime změny nevznikly. Opakování této sady nebylo potřeba.
Nový profil zkouší skutečný následující běh.

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 5000000000 -o local/ratchet/native-bundle-244 > local/ratchet/build-244.log 2>&1
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-244 local/ratchet/native-virtual-244 > local/ratchet/build-virtual-244.log 2>&1
ANYPS3_VIRTUAL_PAD_CONTROL=/home/martink/AnyPS3/local/ratchet/native-virtual-244/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-244 5000000000 > local/ratchet/run-virtual-244.log 2>&1
```

`native-bundle-244` obsahuje původní nový build/snapshot, není samostatně
zkompilovaný ani spuštěný. Zkompilovaný a běžící program je v
`native-virtual-244`; compiler flags `-O2 -pthread -DANYPS3_VULKAN -lvulkan`.
Session 60472 potvrzena živá. V 16:42:15 UTC 212 čtení, 10 907 854 bajtů,
draw 3328, 10 snímků. Skutečná hra hlásí `ports=7 connected=1 backend=SDL2`.
Hashe wrapper/base/binary a shoda base s originálním `program.cpp` ověřeny.
Aktuální runtime zdroje a profil odpovídají 244; konečné ověření zatím chybí.
Překročení `0x53a244`, menu a navigace virtuálním ovladačem zatím neověřeny;
fyzický gamepad nebyl testován. Běh zatím vykresluje načítací ikonu.

Po dosažení menu lze změnit vstup bez patchování běhu:

```bash
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-244/control.txt --buttons down
python local/ratchet/tools/virtual_pad.py set local/ratchet/native-virtual-244/control.txt
```

Tyto příkazy zatím v herním běhu nebyly použity; jsou pouze ověřovací nástroj.
První další akce: pollovat **stejnou session 60472**, číst
`python local/ratchet/tools/observe_run.py native-virtual-244 --record`,
ověřit vlastní konečný `verification.json` spolu s diagnostickým manifestem
a řešit skutečný další stop. Starý `native-virtual-242` nikdy nebyl spuštěn,
má vlastní `NOT_RUN.txt` a chybí mu aktuální profil i lifecycle oprava.

V 16:46:23 UTC 262 čtení, 12 858 941 načtených bajtů, draw 10752,
17 snímků. `frame-000005.png` vizuálně potvrzuje původní načítací planetu.
Vlastní `live-identity.json` porovnává původní PT_LOAD, source/profile hashe,
base překlad s původním bundle 244 a hashe všech diagnostických artefaktů;
vše souhlasí. Tento živý důkaz neprokazuje menu/nav nebo konečný stop.

## Konečný skutečný výsledek 244 (16:59 UTC)

**Ověřené:** session 60472 skončila, hra returncode 1. Předchozí stop
`0x53a244` překročen; hra otevřela oba původní archivy
`packed/levels/front_end/level_cached.psarc` a `level_uncached.psarc`.
444 čtení, 17 897 130 bajtů; SPU 0x100 dokončilo 878 746 602 instrukcí.
15 259 flipů, 30 510 drawů, 33 snímků; host prezentace 0.
`frame-000033.png` (bezeztrátově převedený poslední PPM) vizuálně stále
ukazuje původní načítací planetu. Menu a navigace zůstávají neověřené.

Skutečný stop `pc=0x69f2b4`, `lr=0x69f060`, 308 205 078 PPU kroků:
`FP division by zero is unsupported`. Původní ELF slovo `ed8d3024`
dělí f13 (`0x4063400000000000`, 154,0) / f6 (`0`, +0,0) do f12,
jednoduchá přesnost; FPSCR `0x82004000`. Stop pochází z
`anyps3/floating.py:153`, nikoli z chybějícího statického cíle.
Další krok: ověřit PPU výsledky/příznaky/exception režim v původních
manuálech, implementovat s nativními regresemi, nová sestava a skutečný běh.

Vlastní `verification.json` a nezávislé `terminal-identity.json` ověřují
všech šest artefaktových hashů, tři hashe diagnostického manifestu,
byte-identický base C++ proti native-bundle-244 a všechny původní PT_LOAD
bajty proti EBOOT.analysis.elf. Ovladač zůstal neutrální; gamepad navigace
ani fyzický ovladač nejsou prokázány. Při tomto zápisu není živý herní handle.
