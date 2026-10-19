> **Překonáno** předáním [runtime-handoff-237.md](runtime-handoff-237.md); stop `0x1b8` byl vyřešen linkováním PRX import refs. Zachováno jako historický záznam; těžké artefakty běhu 198 zůstaly.

# Předání runtime po sestavení 198

Datum: 2026-10-14. Původní cíl: původní menu Ratchet & Clank: A Crack in Time a jeho navigace gamepadem. **Cíl není dokončen.** Uživatel povolil skončit po zásadním posunu a dokončení vedlejších dokumentačních úkolů, aby práci převzal další AI agent.

## Ověřený posun

- SPU již nezastavuje původní pevný limit 10 milionů instrukcí. `ANYPS3_SPU_MAX_STEPS` umožňuje explicitní kladný uint64 rozpočet; výchozí limit zůstává 10 milionů. Nativní test kontroluje překročení staré hranice a zastavení před další instrukcí za novým limitem.
- PPU aritmetika zachovává NaN payload, znaménko a PowerPC prioritu operandů, tiší signaling NaN a aktualizuje FPSCR. Odčítání nemění znaménko NaN. Přesné původní FSUBS v `0x3acfc0` již běží.
- Subnormální operandy a výsledky v binary32/binary64 používají celočíselnou aritmetiku a jediné závěrečné zaokrouhlení. Exact subnormal nehlásí UX, tiny inexact hlásí UX/XX. Racionální testy ověřují hraniční hodnoty, midpointy, znaménka a přechod do normálního rozsahu. Původní FSUBS `0 - 2^-142` v `0x3af508` již běží.
- Další původní callback přes OPD `0xd0af48` (`0x4eb05c`, TOC `0xd34a88`), jeho desetipoložková jump table a čtrnáct metod vtable `0xc38280` jsou ověřené a přeložené předem. Původní herní bajty zůstávají stejné.
- Sysutil dotaz na počet audio zařízení `get 0x4c05`, délka 8, vrací skutečnou konfiguraci: zatím žádný zvukový backend. `set 0x4c09`, délka 28, vrací explicitní chybu NOT_IMPLEMENTED, nikoli úspěšnou konfiguraci. Původní hra tuto chybu přijala a pokračovala.
- Původní `TrophyInitThread` (thread 10, OPD `0xd08f60`, entry `0x4c59b8`) již skutečně vykonává instrukce. Dříve byl pouze vytvořen.

## Poslední autoritativní běh

Adresář: `local/ratchet/native-bundle-198/`.

- `verification.json`: kontrola původních herních PT_LOAD bajtů, SHA-256 zdrojů/artefaktů, stop a výsledné snímky.
- `build.json`, `profile.json`, `program.elf`, `program.cpp`, `program`: přesné reprodukovatelné vstupy a AOT výstupy.
- `stderr.log`, `stdout.log`, `frames/`: skutečný výstup běhu.
- `local/ratchet/run-198-summary.log`: stručný strojový souhrn.

PPU dokončilo **118 208 831 kroků**. SPU `0x100` dokončilo **30 465 692 instrukcí**; ostatní kontexty vykonávají původní job manager a čekají na události. RSX dokončilo 1629 FIFO paketů a 4 cleary. Čtyři zachycené snímky 1280 × 720 jsou stále zcela černé; prezentace do okna je nula. **Žádná herní geometrie, menu ani navigace gamepadem nejsou tímto během ověřeny.**

Kód `anyps3/*.py` a `profiles/ratchet-acit.json` byl po běhu porovnán se snapshotem sestavení 198: hashe všech zdrojů a přesné bajty profilu souhlasí. Originální game ELF má SHA-256 `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`. Profil sestavení má SHA-256 `abba22a03a9377653b820a1a9f7833fcf335f742e374a74cd97eff836502d016`.

PPU počty mezi běhy nejsou deterministický časový měřič: plánování a vblank používají skutečný monotónní čas. Novější běh může mít menší počet kroků při dřívějším naplánování jiného vlákna. Posuzuj dosaženou větev a důkazy, ne pouze vyšší číslo.

## Aktuální stop a první další akce

Stop: PC **`0x2011056c`**, LR **`0x20221a98`**, čtení 4 bajtů z nemapované guest adresy **`0x1b8`**. Aktivní je skutečné `TrophyInitThread`, uvnitř původního firmwaru.

**Ověřená stopa:**

1. `libsysutil_np_trophy` kód v `0x20221a68` načte `r0` ze svého TOC slotu (`r2 - 0x7eec`) a v `0x20221a78` uloží hodnotu do command context `+0x6c`.
2. Modulový TOC podle relocation reportu je `0x20238360`; slot `0x20230474` v původním relokovaném ELF obsahuje `0x1b8`. Pro tento slot nebyl nalezen záznam v původní PRX relocation tabulce. Sousední sloty relokace mají.
3. `libsysutil` kód v `0x20110520` načte context `+0x6c` do `r10`, převede jej na `r9` a v `0x2011056c` jej dereferencuje jako OPD před nepřímým voláním.

**Dosud neprokázáno:** proč se konstanta `0x1b8` dostala do pole, které callee používá jako OPD. Kandidáti jsou TOC/ABI při volání napříč moduly, nedokončená inicializace contextu nebo nesprávné předání argumentů. Není to dosud prokázaná chyba PRX relokátoru.

**První další krok:** sledovat skutečný `r2` a argumenty před `0x20221a68` a při vazbě na `libsysutil`, a porovnat originální import/export ABI i inicializaci command contextu. Nepřičítat k `0x1b8` modulový base a nemapovat nulovou stránku jako opravu bez tohoto důkazu. Nevyřazovat trophy vlákno z plánovače.

Pro tento účel vzniká samostatný čtecí `diagnostic.cpp` v adresáři 198, který pouze loguje instrukce a vybrané registry threadu 10. Běžné `program.cpp` zůstává beze změny. Stav a výsledek diagnostiky doplňuje poslední oddíl této zprávy; samotná existence zdroje neprokazuje dokončený proces.

## Reprodukce

Z kořene projektu a do dosud nepoužitého adresáře:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json \
  --max-steps 500000000 -o local/ratchet/native-bundle-199
c++ -std=c++17 -Og -Wall -Wextra -Werror -DANYPS3_VULKAN \
  local/ratchet/native-bundle-199/program.cpp -lvulkan \
  -o local/ratchet/native-bundle-199/program
PYTHONPATH=. python local/ratchet/run_native.py native-bundle-199 500000000
```

Helper nastavuje skutečný disc/cache/HDD root, ASCII case-fold pouze pro ISO9660 extrakt, Vulkan Lavapipe, validation layer, nový adresář snímků a SPU rozpočet. Kontroluje identitu původních herních segmentů a píše verification report; menu flag je výslovně false, automatická interpretace obsahu menu není implementována. Jeho úspěšný exit může znamenat řádně zdokumentovaný guest stop: čti `returncode` v reportu.

## Testy a omezení

`ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q` prošlo: **156 testů**, 97.714 s. Log: `local/ratchet/tests-native-198.log`, jeho hash je ve verification reportu. Vulkan byl povinný. Samostatné testy NaN/subnormal i sysutil wire audio chyb jsou rovněž uložené v `local/ratchet/tests-floating-195.log` a `tests-sysutil-197.log`.

Infinita mimo podporovanou NaN propagaci, overflow, alternativní rounding modes a povolené FP traps nadále explicitně selhávají. Zvukový backend chybí; neznámé služby nejsou simulované úspěšným návratem. SDL2 vstup je ověřený s virtuálním zařízením, nikoli v původním menu. Geometrie/shadery a oknová prezentace stále chybí.

Git metadatový adresář v aktuálním prostředí je prázdný/omezený; `git status` hlásil, že nejde o repozitář. Nebyl vytvořen commit ani nový Git repozitář. Dostupné zdrojové hashe a snapshoty nahrazují chybějící commitovou referenci pro toto předání.


## Dokončená čtecí diagnostika a stav při předání

`diagnostic.cpp` a `diagnostic` byly sestaveny a spuštěny; běh skončil
nenulovým návratovým kódem 1 na **stejném** čtení `0x1b8` na PC
`0x2011056c`, při 118 209 228 PPU krocích. Výstupy:
`diagnostic-stderr.log`, `diagnostic-stdout.log`, `diagnostic-frames/`
a `diagnostic-verification.json` v adresáři sestavení 198. Poslední soubor
obsahuje hashe samostatné diagnostiky a logů.

**Nový přímý důkaz:** trace před `0x20221a68` uvádí skutečný `r2=0x20238360`;
po této instrukci je `r0=0x1b8` a před `0x20221a78` je tatáž hodnota
předána do contextu. Nesprávný TOC registr přímo v místě tohoto loadu
je tedy vyvrácen. Příčina chybného allocator OPD stále není prokázána:
zkontrolovat původní inicializaci tohoto TOC slotu a command contextu,
modulový životní cyklus a jeho ABI. Chyba nesmí být maskována.

Při předání jsou všechny vytvořené build/run/test/diagnostic procesy
terminální a dokumentační subagent GPT-6.1 Sol dokončil svůj úkol.
Žádný běh nečeká na další polling. Dokumentace má pět ověřených originálů
PDF a pět stránkových MD převodů (2755 fyzických stran), platné lokální
odkazy a manifest SHA-256. Kořenový AGENTS.md je hlavní workflow.
Práce se pozastavuje pro předání na přání uživatele; původní cíl zůstává
nedokončený.
