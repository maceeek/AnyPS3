# AnyPS3: workflow pro agenty

## Cíl a hranice projektu

Aktuální cíl je **dostat původní Ratchet & Clank: A Crack in Time do menu
a umožnit jeho navigaci gamepadem**. Zachovej tento cíl i při předání
mezi modely a běhy. Počet instrukcí, dokončený clear, černý flip nebo
syntetický test nejsou dosažené menu.

Projekt používá vlastní statický překlad PPU/SPU a vlastní Vulkan runtime.
Referenční projekty slouží k rešerši rozhraní a algoritmů. Původní herní
bajty se nepatchují. Nevkládej náhradní menu, předem rozbalené assety ani
jiný emulator jako cestu k domnělému splnění cíle.

## Na začátku každého běhu

1. Přečti tento soubor, `docs/knowledge/INDEX.md`,
   `docs/knowledge/CURRENT.md` a `docs/knowledge/HANDOFF.md`.
   Pro historii použij `docs/menu-progress.md`; nečti všechny staré logy.
2. Zkontroluj aktuální soubory, profil, poslední ověřené sestavení a jeho
   `verification.json`. Znalostní báze je rozcestník, ne důkaz současného
   runtime stavu. Existenci a relevantní obsah odkazů ověř.
3. Zjisti skutečný stav běžících procesů a dostupných tool/session handles.
   Živý build nebo run nejprve sleduj; po pouhém timeoutu pozorování ho
   nerestartuj. Samotný starý log nebo stavový soubor neprokazuje živý proces.
4. Pokud je k dispozici Git, přečti status a diff, zachovej cizí změny.
   Prázdná či nepřístupná `.git` není oprávnění inicializovat nový repozitář.
5. Formuluj další konkrétní překážku z posledního skutečného stopu.
   Upřednostni její odstranění před nesouvisejícím refaktoringem.

## Cyklus implementace a důkazů

1. **Reprodukce:** otevři poslední stderr/stdout, stav registrů, relevantní
   původní instrukce a původní ELF/PRX/SPU obraz. Identifikuj skutečné
   argumenty, adresy, verze a očekávaný výsledek.
2. **Rešerše:** hledej přes `rg` v lokálních referencích a manuálech.
   Pro bitové layouty a tabulky kontroluj původní PDF; převod do Markdownu
   je navigační pomůcka. Přesně rozlišuj firmware 3.01, PowerPC, PS3 Cell,
   SPU a PowerXCell 8i. NVIDIA NV40/G70 není automaticky totožná s RSX.
3. **Implementace:** uprav generátor nebo runtime v `anyps3/`.
   Nepodporované chování nikdy nepřeskakuj. Úspěšný návrat smí potvrzovat
   pouze provedenou operaci; známá služba může vrátit skutečnou chybu.
   Neznámý protokol nebo instrukce mají explicitně zastavit běh.
4. **Statické cíle:** novou nepřímou funkci nebo overlay přidej teprve
   po ověření původního OPD/kódových bajtů, TOC, rozsahu a identity obrazu.
   U jump table ověř základ, znaménko/šířku offsetů, počet i všechny cíle.
   Evidence a původ patří do profilu nebo znalostního záznamu.
5. **Test:** u změny sémantiky přidej nativní regresní test s nezávislým
   očekáváním. Ověř endianitu, aliasy, hraniční hodnoty, chyby a stavové
   příznaky podle relevance. Test nemá pouze kopírovat algoritmus změny.
6. **Nová sestava:** vždy nový adresář, snapshot profilu a hash zdrojů.
   Předchozí artefakty nepřepisuj. Jednorázová změna v generovaném C++
   není trvalá oprava; samostatná diagnostika musí být tak označena.
7. **Skutečný běh:** vykonej původní hru se skutečnými daty a souborovým
   úložištěm. Zachovej stderr/stdout, návratový kód, stop a zachycené snímky.
   Po úpravě nedokazuj postup jen úspěšným sestavením.
8. **Ověření:** porovnej původní PT_LOAD bajty, hashe artefaktů, skutečný
   výstup a rozsah testů. Počty clearů/flipů odděl od geometrie, zachycených
   snímků, prezentace do okna a dosažení menu.
9. **Zápis:** aktualizuj `CURRENT.md`, vhodný tematický záznam a stručnou
   historii v `docs/menu-progress.md`. Potom řeš další skutečný stop.

## Standardní příkazy

Z kořene projektu, s dostupnými lokálními herními vstupy:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json \
  --profile profiles/ratchet-acit.json --max-steps 500000000 \
  -o local/ratchet/native-bundle-N

# Překlad je rozdělen do jednotek (units.json: anyps3.hpp, program.cpp
# s runtime a main, ppu-*.cpp, spu-*.cpp); kompilují se paralelně.
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build -j 24 \
  local/ratchet/native-bundle-N local/ratchet/native-virtual-N
# bez obalu virtuálního padu: python -m anyps3.native_build --vulkan local/ratchet/native-bundle-N

ANYPS3_AUDIO_SINK=null ANYPS3_FRAME_STRIDE=600 \
  ANYPS3_VIRTUAL_PAD_CONTROL=$PWD/local/ratchet/native-virtual-N/control.txt \
  PYTHONPATH=. python local/ratchet/run_native.py native-virtual-N 500000000

ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q
```

`N` nahraď dosud nepoužitým číslem. Build/compile/test/run výstupy ukládej
do vlastních logů. Čtecí ověřovací helper `local/ratchet/run_native.py`
nastavuje skutečný disk, cache, HDD, frame directory a SPU rozpočet.
Před použitím ověř jeho aktuální obsah. Standardní SPU limit je 10 milionů
instrukcí na vlákno; `ANYPS3_SPU_MAX_STEPS` explicitně nastavuje jiný
kladný uint64 rozpočet. Změna limitu není oprava sémantiky ani důkaz menu.

Spusť cílené testy před drahým herním sestavením. Celou regresní sadu
spusť po dokončené skupině runtime změn; při změnách RSX/Vulkanu musí
Vulkan test skutečně běžet. Již úspěšné testy neopakuj bez nové změny,
selhání nebo konkrétní pochybnosti. Pouhá editace dokumentace nevyžaduje
kompilaci hry; ověř odkazy, manifesty a dostupnost artefaktů.

## Znalostní báze a dokumentace

- `docs/knowledge/INDEX.md`: rozcestník a pořadí čtení.
- `docs/knowledge/CURRENT.md`: současný ověřený stav a první další krok.
- `docs/knowledge/HANDOFF.md`: předání mezi modely/běhy.
- `docs/knowledge/WORKFLOW.md`: pravidla znalostních záznamů a dokumentů.
- `local/reference/cell-docs/`: původní PDF, převody, downloader a manifest.
- `local/reference/`: skutečně dostupné kopie RPCS3, QEMU, PSL1GHT,
  envytools a RPCS3 wiki. Jejich přesné cesty eviduje znalostní báze;
  nepředpokládej, že jsou všechny ve stejné hloubce.

Každý důležitý poznatek uveď jako **ověřené**, **odvozené** nebo
**hypotéza**. Připoj přesnou verzi zdroje, soubor a řádek nebo PDF stranu,
konkrétní runtime/test důkaz, omezení a důsledek pro další práci. Ulož
i vyvrácené hypotézy, pokud zabrání opakování stejné slepé uličky.
Nenechávej klíčová zjištění jen v chatu či paměti konkrétního modelu.

U PDF eviduj původní URL, název/verzi z titulní strany, SHA-256, počet
stran, datum získání a metodu převodu. Zachovej mapování na PDF strany.
Neoznačuj tutorial jako handbook a nenahrazuj nedostupnou verzi novější
či starší potichu. Převod textu není přesná rekonstrukce diagramů/tabulek.

## Subagenti a komunikace

Izolované dokumentační rešerše lze delegovat subagentovi GPT-6.1 Sol,
pokud je dostupný. Předej mu konkrétní výstupy, soubory, pravidla a
vlastnictví cest. Runtime a profil spravuje hlavní agent; dokumentační
agent nemá současně editovat stejné soubory. Před předáním práce počkej
na všechny zadané vedlejší úkoly a jejich výsledky ověř.

Referenční repozitáře neměň bez zvláštního důvodu a čti jejich místní
`AGENTS.md`, pokud se v nich pohybuješ. Nepřenášej pravidla pro jejich
upstream contribution na změny vlastního AnyPS3 projektu.

Komunikuj česky, stručně a průběžně. Uváděj zjištěnou překážku, důkaz
posunu a to, co řeší další krok. Rutinní vratné kroky v autorizovaném
rozsahu nevyžadují opakované potvrzení.

## Dokončení a předání

Celý cíl je hotový pouze při prokazatelném původním menu hry a
prokazatelné navigaci gamepadem přes herní vstupní cestu. Samostatný
test SDL ovladače neprokazuje navigaci menu. Uveď zvlášť virtuální
ovladač, fyzický gamepad a případné neprovedené ověření.

Když uživatel dovolí skončit po zásadním posunu, dokonči vedlejší úkoly,
ověř nový běh a před odchodem sepiš: změny, poslední skutečný stop,
přesné příkazy a artefakty, testy, přetrvávající omezení, živé procesy
a první další akci. Neschovávej nehotové menu za dílčí milník.
Nevytvářej z původního cíle užší náhradní cíl. Pozastavení pro předání
není dokončení; pokračuj či zastav podle posledního pokynu uživatele.
