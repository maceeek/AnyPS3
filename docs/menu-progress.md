# Postup směrem k menu

Cíl: původní menu Ratchet & Clank: A Crack in Time a navigace gamepadem.
Menu zatím dosaženo není. PPU/SPU nadále běží výhradně ze statického překladu;
Vulkan backend je vlastní, RPCS3 slouží pouze jako místní technická reference.

## Herní data a životní cyklus vlákna

Sestavení 124–127 ze dne 2026-10-14 překročila původní blokaci
`GDAT initGameData`. Původní firmware postupně:

1. Otevřel relaci služby s existujícím paměťovým kontejnerem. Nativní služba
   rezervuje skutečných 64 KiB pracovního prostoru z jeho rozpočtu; kontejner
   po dobu relace nelze zrušit.
2. Vyžádal `getGameDataStat` pro `BCES00511`. Služba čte skutečnou existenci
   metadat a volné místo pod explicitním `ANYPS3_HDD_ROOT`. Pro nové herní
   úložiště vrací odpovídající stav a cesty. Interní wire struktura má 0x1450
   bajtů, veřejný callback 0x1448; převod provádí původní firmware.
3. Zavolal původní herní callback `0x66dc78` přes OPD `0xd18230`. Tento
   callback byl předem zahrnut do statického překladu, pouze po kontrole
   identity ELF a skutečného deskriptoru. Callback převezme cesty a volné
   místo a sám vrátí `OK_CANCEL`; služba nepředstírá vytvoření dat.
4. Uzavřel relaci přes `endGameData` a uvolnil její rozpočet.
5. Dokončil `SysUtilThread()` přes `sys_ppu_thread_exit`. Runtime uvolní
   vlastněný zásobník/TLS a zachová návratový stav pro `sys_ppu_thread_join`.
   Hlavní vlákno skutečně provedlo join a pokračovalo v grafické inicializaci.

Dosud je podporovaný dotaz na nové úložiště bez PARAM.SFO, bez výpočtu
velikosti a bez chybového dialogu. Existující metadata, jejich aktualizace
a další režimy stále explicitně selhávají. Adresářové symlinky a průchod
mimo připojený kořen se odmítají. Žádná podpora neznámých požadavků se
nesimuluje úspěšným návratem.

Pro další reprodukci je kromě disku a cache potřeba:

```bash
mkdir -p local/ratchet/hdd0
export ANYPS3_HDD_ROOT=local/ratchet/hdd0
```

## Grafické příkazy

Sestavení 128 překročilo 348 paketů RSX. Konstanty vertex shaderu se
ukládají bitově přesně do 512 čtyřsložkových registrů, s respektováním
`TRANSFORM_CONSTANT_LOAD` i neinkrementujících FIFO zápisů. Následující
invalidace vertex cache uchovává generaci invalidace; zatím neexistuje
cache vykreslených vertexů.

Sestavení 131 provedlo první skutečný herní `CLEAR_SURFACE` přes Vulkan:
960 × 704, dva horizontální vzorky, ARGB8 a Z24S8, maska `0xf3`.
Dlaždicové adresování obou ploch respektuje pitch a banku. Clear ukládá
plnou hodnotu Z24S8 bitově přesně přes RGBA8 attachment; nejde zatím
o podporu hloubkového testování při kreslení. Komprimovatelné plochy
udržujeme v nekomprimované reprezentaci, komprimovaná data nedekódujeme.

Převod adres dlaždic byl porovnán s místní referencí pro 20 kombinací
pitch/banky. Testy ověřují také dva vzorky, scissor, Z24S8 a zachování
okolní paměti. Celá sada v tomto bodě: 133 testů, všechny prošly, Vulkan
byl povinný; obrazový test prošel i s kontrolou synchronizace Khronos.

Běh 131 se následně zastavil na `GET_REPORT` (`0x1800`, hodnota
`0x02020a00`) po 408 paketech RSX a 102 838 528 krocích PPU.
`rendered clears=1 presented_frames=0`: skutečná GPU operace, dosud žádná
herní geometrie, prezentovaný snímek ani menu. Samostatný
[Vulkan obrazový test](vulkan-rendering.md) zůstává diagnostikou.

## Reporty, fronta a časování výstupu

Následující běhy doplnily GPU reporty v lokální reportové oblasti, VRAM
a mapované hlavní paměti. Zapisují skutečný monotónní čas a hodnoty čítačů;
po resetu a bez podporovaných draw calls jsou čítače nulové.

`DRIVER_QUEUE` zpracuje skutečný display buffer a pošle pouze povolené
události původnímu firmwaru. Nepotvrzuje tím dokončený flip. Vblank vychází
ze skutečného monotónního času, při 60000/1001 Hz podle video konfigurace.
Původní herní callback `0x661730` (OPD `0xd17890`) je přeložen předem.
Celá sada po těchto změnách prošla: 136 testů včetně povinného Vulkan testu.

Sestavení 136 ukázalo čekání na zařízení `device+0x30`. Místní reference
v `NV47/HW/nv406e.cpp` tento registr označuje jako synchronizaci display
queue a bezpodmínečně přepisuje release nuly na jedničku. Naše implementace
potvrzení váže na dosud nepotvrzenou generaci skutečně zpracované fronty.
Jde o model protokolu odvozený z FIFO a reference, zatím neověřený na PS3
hardwaru; nejde o zápis do samostatného semaforu dokončení flipu.

## První skutečné výměny bufferů

Sestavení 138 dokončilo čtyři herní flipy střídající buffery 1 a 2.
Výstupní sink načetl jejich skutečný obsah 1280 × 720 přes Vulkan,
počkal na fence, zapsal PPM a teprve poté oznámil dokončení firmwaru.
Všechny čtyři snímky jsou černé (všech 2 764 800 RGB bajtů každého
snímku je nulových). Nejde o menu ani důkaz kreslení geometrie.

Reprodukce používá navíc `ANYPS3_FRAME_DIR`, ukazující na existující prázdný
adresář. Soubory `frame-000001.ppm` atd. se nikdy nepřepisují; diagnostický
limit je 256 snímků. Chybějící výstup, chyba Vulkanu nebo zápisu nepotvrdí
flip a neuvolní jeho semafor. Jde o headless výstup, nikoli swapchain do
okna: diagnostika odděluje `captured_frames` od `presented_frames=0`.

Běh 139 překročil původní virtuální metodu `0x32bb74` a zastavil se při
volání `cellSysmoduleInitialize`. Export byl ověřen přímo v původním
firmwaru 3.01: funkce na offsetu `0x312c` pouze vrací nulu. Obsluha tohoto
exportu proto nepředstírá načtení dalších modulů; ty nadále vyžadují
skutečné obrazy a jejich statický překlad.

## Inicializace správce souborů

Běhy 142–145 načetly skutečný `libio` (ID 23), spustily jeho původní
`module_start` a vytvořily herní `TrophyInitThread`. Dosažené vtable
správce stavu a alokátoru byly ověřeny proti původním OPD v EBOOT a
přidány mezi vstupy statického překladu.

`lightweight_sync.py` poskytuje vytvoření, zrušení, signal, broadcast
a nekonečné čekání pro lehké podmínkové proměnné. Čekání uvolní vlastněný
mutex, po signálu jej musí znovu získat a obnoví původní hloubku rekurze.
Blokované vlákno nesmí dokončit wait, dokud jiný vlastník mutex neuvolní.
Testy pokrývají tento souběh, FIFO/priority výběr, nepersistentní signál
a odmítnutí zrušení používané podmínky. Timeouty zůstávají explicitně
nepodporované.

Sestavení 145 dosáhlo původního FILE-output callbacku `0x9098e8` při
102 888 256 krocích PPU. Stále jsou zachyceny čtyři černé snímky.
`local/ratchet/native-bundle-145/verification.json` ověřuje zachování
všech původních PT_LOAD bajtů, hashe programu a snímků. Novější sestavení
uchovávají i přesný použitý `profile.json` a hashe zdrojů při generování.
Celá sada zde prošla se 139 testy a povinným Vulkan backendem.
Následující běh 146 už vypsal původní hlášku `FIOS 1.3.1 initialized`
a vstoupil do backendu souborových operací.

## První nativní souborové operace

Běhy 147–155 staticky přeložily dosažené callbacky FIOS, včetně jeho
mediálních pracovníků a plánovače. Pracovní vlákna skutečně čekají na
lehké podmínce a po signálu zpracují požadavek. `sys_process_is_stack`
klasifikuje adresy podle horních čtyř bitů (`0xd`), jak očekává knihovna.

Sestavení 157 provedlo skutečný `stat("/dev_hdd1")`. Sestavení 159 potom
otevřelo/vytvořilo `cache.idx` a `cache.dat` v unikátním adresáři cache
a původní hra vypsala `Creating new disk-based cache files`. V tomto bodě
ještě neproběhlo načítání herních assetů. Identita původních PT_LOAD bajtů
a čtyři stále černé snímky jsou ověřeny v
`local/ratchet/native-bundle-159/verification.json`.

`native_fs.py` poskytuje guest deskriptory 3–255, skutečné open/read/write,
close, seek a stat/fstat. `/dev_bdvd` je pouze pro čtení, `/dev_hdd1` je
session cache a `/dev_hdd0` explicitní zapisovatelný kořen. Symlinky a
`..` se odmítají, neexistující soubory vracejí chybu. Zvláštní dotaz
firmwaru `fcntl 0xc0000006` vrací vložený ENOTSUP; firmware sám použije
běžné LV2 operace. Rozšířené/šifrované otevření a neznámé fcntl stále
končí diagnostikou. Jeden přenos má diagnostický limit 16 MiB.

Další běh doplnil `_sys_strncmp` a dosáhl dotazu na geometrii úložiště.
Velikosti sektorů/bloků odpovídají konfiguraci PS3 mountu; volné místo
se zjišťuje ze skutečného hostitelského úložiště.

Sestavení 161 už zapsalo po 512 bajtech do obou souborů cache. Původní
SPURS následně načetl další firmware overlay (EA `0x20266b00`, LS `0xa00`,
7456 bajtů). Běhy 163–166 doplnily skutečně dosažené SPU posuny celého
128bitového registru, SUMB a MPYU. Testy kontrolují hranice počtů bitů,
unsigned aritmetiku a překrývající se zdrojové/cílové registry.

V běhu 166 SPURS přes DMA načetl také spustitelný segment původního
herního SPU ELF na EA `0xbf4480` (entry LS `0x3050`, kód EA `0xbf4580`,
LS `0x3000`, délka `0x8c20`). Segment je zahrnut do dalšího AOT sestavení
po ověření ELF a hashe; neznámá instrukce nebo změna jejího slova stále
zastaví běh. Celá sada v bodě 166 prošla: **145 testů**, Vulkan povinný.
`native-bundle-166/verification.json` zachovává důkazy o tomto běhu.
Výstup zůstává na čtyřech černých snímcích, bez menu a bez herní geometrie.

Sestavení 171 už vykonalo původní herní SPU kód (dosažené LS `0x9c58`).
Další běhy doplnily halfword posuny/rotace a aritmetiku, zbývající varianty
quadword posunů a FSCRRD/FSCRWR se skutečným stavem každého vlákna a maskami
rezervovaných bitů. Nepodporované aritmetické FP instrukce nadále selhávají.
SPU emitter používá menší nativní funkce po stránkách, stejně jako PPU;
výběr stále pouze volá předem přeložený blok a ověřuje původní slovo.

Běh 174 dosáhl 103 219 646 PPU kroků. Herní SPU po 27 500 instrukcích
čeká na další událost. FIOS zapsal čtrnáct bloků po 64 KiB do indexu
cache a skutečně prověřil neexistující adresář instalace pod `/dev_hdd0`.
Následují původní funkce `fios::dearchiver`; první čtení archivu hry
v tomto bodě ještě není doloženo. Celá sada v bodě 173 měla 147 testů,
nový test FPSCR navíc prošel samostatně. Běh 174 má vlastní
`verification.json`, všechny čtyři zachycené snímky jsou nadále černé.

Sada v bodě 177 prošla se 148 testy a povinným Vulkanem. Běh 178 doložil
první pokus o otevření `packed/game/game.psarc` z disku. Náš extraktor
však používá primární ISO9660 jména, která jsou v tomto obrazu velkými
písmeny. Pro tuto konkrétní reprezentaci disku je potřeba:

```bash
export ANYPS3_DISC_CASEFOLD=1
```

Jde o explicitní adaptaci hostitelských názvů, nikoli změnu herního kódu
nebo obecné tvrzení o chování PS3 filesystémů. Přesný název má přednost,
jednoznačný ASCII case-fold umožní otevřít existující soubor. Kolize
zastaví běh, symlinky a průchod mimo kořen se dál odmítají. HDD a cache
zůstávají citlivé na velikost písmen. Cílená sada šesti FS testů prošla.

**Běh 179 poprvé skutečně otevřel a četl herní archiv**
`/dev_bdvd/PS3_GAME/USRDIR/packed/game/game.psarc`. Původní FIOS parser
načetl 32bajtovou hlavičku `PSAR` a 68 bajtů indexu, poté zavolal původní
porovnávací funkci při řazení záznamů. Její OPD `0xd1f150` odkazuje na
`0x717558`; následující sestavení ji zahrnuje do AOT. Nejde zatím o
načtení textur ani vykreslené menu. Report běhu 179 obsahuje hash archivu,
záznam čtení, zachování původních herních segmentů a čtyři černé snímky.

Běhy 180–182 dokončily čtení celého archivu GAME.PSARC (19 811 bajtů)
a zápis do skutečné cache. V `cache.dat` začíná archiv na offsetu 512
a jeho bajty se přesně shodují s původním souborem. Doplněny jsou skutečné
`fsync`/`fdatasync` a PPU MCRF s testem všech dvojic CR polí.

Běhy 183–184 předaly komprimovaný blok původnímu SPU programu. Nativně
přeložená dekomprese už vykonává tabulkové operace a celočíselné násobení;
její dokončení a výstup zatím nejsou ověřeny. Limit 100 000 SPU instrukcí
zastavil běh v LS `0x8928`, proto má následující sestavení diagnostický
strop 10 milionů instrukcí na SPU vlákno. Podrobné trasování se zapíná
`ANYPS3_SPU_TRACE=1`; DMA, čekání a chyby se logují i bez něj.

Celá sada v bodě 183 prošla se 150 testy a povinným Vulkanem. Následující
samostatný test násobení kontroluje znaménka, horní/dolní poloviny,
akumulaci a aliasy registrů. Zobrazení nadále tvoří čtyři černé snímky.

Celá sada v bodě 185 prošla se **151 testy**. Další sestavení doplnila
čtení SPU stavu (dosud neizolovaná vlákna s vypnutými přerušeními),
větvení s vypnutím přerušení a skutečné mailbox události SPU → PPU.
Povolení přerušení, izolované SPU a neznámé mailbox příkazy stále selhávají.
Plná fronta pozastaví odesílatele a zachová jeho zprávu pro opakování;
nepřipojený port vrací ENOTCONN u požadavku s odpovědí.

Běh 188 přenesl první 64KiB blok dekomprese zpět do hlavní paměti,
doručil osm událostí přes port 17 a dosáhl **109 994 752 PPU kroků**.
Hra už otevřela také `global_cached.psarc` a `global_uncached.psarc`.
SPU dokončilo přes 7,85 milionu instrukcí; následující stop je při DMA
na nerozpoznaný nebo rozdělený paměťový rozsah. Samotné počty DMA nejsou
důkazem správných rozbalených bajtů. Vzor `game.toc` má 136 620 bajtů,
SHA-256 `b540c6930178ed93d1850c7210069dd612df73a17a5cde49f42ad9693630c08d`;
nezávislé porovnání s nativním výstupem dosud zbývá.
Report `native-bundle-188/verification.json` potvrzuje zachování původních
herních segmentů a stále pouze čtyři černé snímky.

**Milník 190: výstup SPU dekomprese je ověřený bajt po bajtu.**
Diagnostický čtecí pozorovatel zachytil všech 136 620 bajtů `game.toc`
po třetí SPU události. Shodují se s nezávislým zlib dekódováním archivu
a s výše uvedeným hashem. Runtime také zvládá DMA přes sousední paměťová
mapování; před kopírováním ověří celý rozsah a oprávnění. Herní běh překročil
dřívější chybu na `0x409fee80 + 0x4000` a dosáhl 110 610 176 PPU kroků.
Aktuální stop je diagnostický limit 10 milionů instrukcí SPU, nikoli menu.
Celá sada prošla se **153 testy** a povinným Vulkanem.
Souhrn, omezení a reprodukce jsou v [milníku načítání](ratchet-acit-loading-milestone.md).


## Pokračování po SPU rozpočtu a floating point

Běh 191 používá explicitní `ANYPS3_SPU_MAX_STEPS=500000000` místo
výchozího limitu 10 milionů instrukcí na vlákno. Rozpočet přijímá pouze
kladné desítkové uint64 hodnoty; nula, znaménka, přetečení a nečíselné
vstupy se odmítají. Nativní test vykoná původně přeloženou smyčku přes
starou hranici a ověří stop před instrukcí překračující nový rozpočet.

První SPU v běhu 191 dokončilo 30 455 452 instrukcí a čeká na událost.
PPU dosáhlo 115 888 424 kroků a zastavilo se na `FSUBS` v `0x3acfc0`:
původní hra načetla single-precision NaN `0xffffffff`. Podpora NaN nyní
zachovává payload, znaménko a prioritu operandů, tiší signaling NaN a
nastavuje VXSNAN/VX/FX, FPRF a FI/FR. Odčítání nemění znaménko vstupního
NaN. Test obsahuje přesné instrukční slovo z tohoto stopu.

Běh 192 tento stop překročil a dosáhl callbacku přes OPD `0xd0af48`:
kód `0x4eb05c`, TOC `0xd34a88`. Jeho desetipoložková tabulka relativních
větví v `0x4eb088` i cíle byly ověřeny proti původnímu ELF a zahrnuty do
statického překladu. Běh 193 pak zastavilo další `FSUBS` v `0x3af508`:
`0 - 2^-142`, tedy přesný záporný binary32 subnormální výsledek.

Runtime nově podporuje subnormální operandy i výsledky. Před závěrečným
zaokrouhlením posune nezakulacený significand a zachová sticky remainder;
nejde o dvojí zaokrouhlení. Exact subnormal nenastavuje UX, tiny inexact
nastavuje UX a XX. FPRF rozlišuje znaménko, nulu a subnormální výsledek.
Racionální referenční testy zahrnují obě přesnosti, nejmenší hodnoty,
midpointy, přechod do normálního rozsahu a podepsanou nulu.
Priorita NaN a detekce tininess před zaokrouhlením byly porovnány také
s [primárním zdrojem QEMU PPC](https://github.com/qemu/qemu/blob/master/target/ppc/cpu_init.c).
Infinita bez podporované NaN propagace, overflow, alternativní rounding
mode a povolené FP traps stále explicitně selhávají.

Celá sada po NaN změnách prošla se 155 testy a povinným Vulkanem;
rozšířené racionální testy subnormálních hodnot prošly samostatně.
Běhy 191–193 mají vlastní `verification.json` s kontrolou původních
PT_LOAD bajtů a hashi artefaktů. Nadále zachycují čtyři černé snímky.
Menu ani navigace gamepadem tím ještě nejsou ověřeny.


Běh 194 překročil i subnormální `FSUBS` a dosáhl 117 709 281 PPU kroků.
Dosažený herní objekt s vtable `0xc38280` má čtrnáct původních OPD;
slot 2 odkazuje přes `0xcdbe00` na `0x2e8814`. Sestavení 195 zahrnuje
celou ověřenou tabulku, včetně všech kódových cílů, bez změny bajtů hry.

Běh 195 se zastavil na interním sysutil parametru `0x4c05`, délka 8.
Původní firmware export `cellAudioOutGetNumberOfDevice` (NID `0xe5e2b09d`,
kód `0x2011949c`) sestavuje právě tento dotaz a čte počet zařízení z druhého
slova odpovědi. Nativní runtime nemá nakonfigurovaný zvukový backend, proto
vrací počet nula; neplatný výstup vrací `0x8002b242`. Běh 196 tento dotaz
překročil a dosáhl 117 861 953 kroků, potom požádal o `set 0x4c09`, délka 28.
Export `cellAudioOutConfigure` (NID `0x4692ab35`) byl ověřen ve stejném
firmwaru; pro chybějící backend runtime vrací explicitní NOT_IMPLEMENTED,
pro sekundární výstup UNSUPPORTED_AUDIO_OUT. Nepotvrzuje konfiguraci ani
přehrávání. Původní firmware i hra rozhodují, jak s chybou naložit.

Celá sada v bodě 196 prošla se **156 testy**, Vulkan povinný. Následující
cílený test ověřuje i chybu audio konfigurace a správné jméno `set`
v odpovědi. Běhy 194–196 zachovávají původní PT_LOAD bajty a stále pouze
čtyři černé snímky; aktuální důkazy jsou ve vlastních `verification.json`.


Běh 197 prokazuje, že původní hra převzala chybu audio konfigurace
`0x8002b240` a pokračovala. PPU skutečně naplánovalo `TrophyInitThread`,
pro který další sestavení zahrnuje ověřený OPD `0xd08f60`/entry `0x4c59b8`.
Běh 198 už vykonává toto původní vlákno; stop je v původním `libsysutil`
na PC `0x2011056c`, LR `0x20221a98`, čtení z neplatného OPD `0x1b8`.
Nejde už o chybějící statický vstup. PPU dokončilo 118 208 831 kroků,
SPU `0x100` 30 465 692 instrukcí. Plná sada prošla se 156 testy a povinným
Vulkanem v 97.714 s. Původní herní PT_LOAD bajty i aktuální zdroje/profil
souhlasí se snapshotem sestavení 198; čtyři snímky zůstávají černé.

Požadované předání mezi modely upravuje kořenový [AGENTS.md](../AGENTS.md).
[Znalostní báze](knowledge/INDEX.md) obsahuje lokální CELL/SPU/PPU/RSX
reference, původní PDF a stránkové Markdown převody s manifestem.
[Runtime předání 198](knowledge/runtime-handoff-198.md) uvádí přesné
artefakty, reprodukci a první další diagnostickou akci. Menu a navigace
gamepadem nadále nejsou dosažené; dokumentační milník není jejich náhrada.


2026-10-14 (199–204): PRX import refs linkovány, nativní trophy služba (NPTR), SPU běží během PPU spánku; stop 204 = nepřeložená vtable metoda `0x3304b4`, vtable `0xc3c428` přidána do profilu, build 205 čeká. Menu/gamepad nedosaženy.

2026-10-14 (216–237): RSX software renderer, první guest-renderovaný snímek (bundle 228, ikona načítání), tisíce flipů načítací obrazovky; běh 237 přerušen pro předání. Menu/gamepad nedosaženy. Viz docs/knowledge/runtime-handoff-237.md.

2026-10-14 (238): zdroje/profil/artefakty 237 ověřeny proti hashům, nová sestava 238
úspěšně zkompilována a spuštěna s rozpočty 5 mld. Původní načítací ikona vizuálně
potvrzena, čtení pokračuje; běh je při zápisu živý, bez konečného ověření.
Menu/gamepad nedosaženy. Viz [záznam 238](knowledge/experiments/2026-10-14-loading-238.md).

2026-10-14 (238–240): 238 skutečně překročil starý SPU limit (755 M SPU, 274 M PPU),
načetl 16,46 MB a zastavil na nepřeloženém `0x962700`. Ověřena původní 14slotová
vtable `0xc53248`, OPD/TOC/kódové rozsahy a rozšířen profil. Renderer jednou
vyhodnocuje konstantní MOV shader při zachování všech pixelových operací;
174 testů s povinným Vulkanem OK, syntetické měření ~2,28× rychlejší.
239 zkompilováno, nespouštěno; nové 240 obsahuje i ověřenou tabulku a skutečná
hra běží v session 63143. Načítací ikona po změně vizuálně ověřena,
původní PT_LOAD a nové zdrojové/profilové hashe souhlasí. Překročení nového
stopu/menu/gamepad zatím neověřeny. Viz [240](knowledge/experiments/2026-10-14-vtable-240.md).

2026-10-14 (240–242): 240 dokončil první iteraci loaderu a zastavil na druhém typu
`0x9626a4`, vtable `0xc53208`. Původní šestipoložkový konstruktor ověřil všech
šest vtables (85 OPD slots); profil rozšířen o druhou a čtyři zbývající tabulky
s přesným původem. Runtime nezměněn od 174 testů s Vulkanem. 241 je živý v session
55131; 242 zkompilované, hra ještě nespouštěna. Menu/gamepad neověřeny.
Viz [241/242](knowledge/experiments/2026-10-14-loader-vtables-241-242.md).

2026-10-14 (konec 241, běh 242): 241 potvrdil první dvě iterace loaderu a zastavil
na třetím typu `0x962404`, který už širší profil 242 obsahuje. PT_LOAD a vlastní
hash ověření 241 prošlo; po jeho konci spuštěno 242 v session 31385. Cíl původního
menu a navigace gamepadem zůstává neověřený, hra 242 je živá.

2026-10-14 (242–244): 242 prošlo readiness třetího typu a zastavilo v jeho těle
na callbacku `0x53a244`. Ověřena původní registrace sedmi callbacků, rozšířen
profil. 243 předčasně zahájeno a přerušeno před hrou; 244 správně sestaveno
a skutečná hra běží v diagnostickém SDL obalu `native-virtual-244`, session 60472,
connected=1. Nativní virtuální input test (buttons/axes/pressure) a chyby protokolu
ověřeny; původní překlad/ELF nezměněny a hashovány. Menu ani virtuální/fyzická
navigace ještě nejsou prokázány. Viz [244](knowledge/experiments/2026-10-14-virtual-pad-and-callbacks-244.md).

2026-10-14 (konec 244): hra překročila callbackový stop a otevřela oba front_end
archivy. 444 čtení / 17 897 130 bajtů, 878,75 M SPU instrukcí; skutečný stop
`0x69f2b4` po 308,21 M PPU krocích: nepodporované FP dělení 154,0 / +0,0.
Session 60472 ukončena, hra returncode 1. PT_LOAD, artefaktové a diagnostické
hashe nezávisle ověřeny. Poslední snímek je stále načítací planeta; menu/nav
nedosaženy. Další krok: PPU division-by-zero sémantika a nativní regrese.

2026-10-14 (245): FP dělení nulou vrací architektonické nekonečno s FPSCR;
navazující odečítání s nekonečnem podporováno. Nativní regrese vykonává přesnou
původní pětici instrukcí a ověřuje guest uloženou hodnotu 0,75, flags, endianitu
a aliasy; šest FP testů a všech 175 testů s Vulkanem OK. Nové 245 kompilováno,
ELF/source/profile/base/manifest ověřeny a původní hra živá v session 6682
(`native-virtual-245`). Profil nezměněn, pad neutrální; menu/nav zatím neověřeny.
Viz [245](knowledge/experiments/2026-10-14-fp-zero-divide-245.md).

2026-10-14 (konec 245, nové 246): skutečná hra 245 vykonala podporované
zero-divide (FPSCR ZX) a zastavila na interním switch případu 13 `0x69dd30`.
Konečné původní PT_LOAD/artifact/base/manifest ověření OK, poslední snímek
stále loading planeta. Ověřena celá původní 14entry tabulka `0x69db38`,
unsigned-byte bound, signed-relative offsets a všech 12 různých cílů.
Profil +12 seeds, runtime shodný s testovaným 245. Nové 246 zkompilováno,
input identity ověřena a skutečná hra živá v session 6422, pad neutrální.
Menu/nav stále neprokázány. Viz [246](knowledge/experiments/2026-10-14-byte-switch-246.md).

2026-10-14 (předání 246): uživatel zrušil automatický cíl a požádal o předání.
246 ukončeno Ctrl-C (session 6422 exit 130, guest rc neznámý) při 266 čteních /
13 048 193 bajtech, 17 captures, stále loading planeta. Žádný nový sémantický
stop, case 13 ještě neověřen. Interruption manifest nezávisle ověřuje PT_LOAD,
source/profile/base/diagnostické hashe, logy a captures; logy po konci nerostou.
Žádný živý námi zadaný build/run handle. Menu/nav nesplněny. Úplné ověřované
[předání 246](knowledge/runtime-handoff-246.md), aktuální CURRENT/HANDOFF/INDEX
aktualizovány; další běh teprve po pověření, nový nepoužitý adresář 247.

## 247–255 (2026-10-14 večer, Claude Opus 5.5 + codex GPT-6.1 Sol subagenti)

247 prošlo case 13; překlad všech 34 512 OPD funkcí z původní `.opd` (2,75 M instrukcí),
detektor switchů 159→337/363 + 23 tabulek rekonstruovaných subagentem, nové PPU/VMX/FP instrukce,
split-safe FS/memcpy, libc, cellAudio (null sink), trylock/atexit/malloc, SDL okno (`ANYPS3_PRESENT=sdl`),
~2–5× rychlejší `GuestMemory`. Nejdále 252/253: 541 M kroků, 67 snímků, stále načítací planeta;
stop 253 = nepřeložený SPU job CollNarrow. 255 (s SPU overlayi) nedokompilováno — kompilace
jediné ~458MB TU je hlavní brzda. Viz [záznam](knowledge/experiments/2026-10-14-full-opd-coverage-247-250.md).
Menu ani navigace gamepadem zatím nedosaženy.

## 256–260 (2026-10-15, Claude Opus 5.5 + codex GPT-6.1 Sol subagenti)

Generovaný překlad rozdělen do 193 paralelních jednotek (`units.json`; build ~4 min místo nedokončitelné
LTO TU). 256 prošlo CollNarrow, stop na chybějícím jobu CollBroad → profil 257 odvozuje 138 SPU jobů z původních
datových odkazů. 257 stop: `lwarx` na konci segmentu libio → PPU rezervace jen přes čtené bajty (258).
258 stop: `sys_config_open` z nativního libio (cellKb/cellMouse) → HLE Kb/Mouse bez zařízení
a `cellPadSetActDirect` bez aktuátoru (260). Codex: `_sys_*printf`, bitově shodné zrychlení RSX draw.
**260 nejdále: 1 214 M PPU kroků za ~11 min**, stále načítací planeta; stop na SPU FP instrukci
(`fcgt`), statický sken našel tisíce chybějících SPU FP instrukcí — implementace delegována.
Viz [záznam 256–260](knowledge/experiments/2026-10-15-split-units-256.md). Menu ani navigace gamepadem zatím nedosaženy.

## 261–284 (2026-10-15, Claude Opus 5.5 + codex GPT-6.1 Sol subagenti)

Plošný přístup místo čekání na jednotlivé stopy: statický sken 99 chybějících syscallů
a paralelní agenti FS/SYNC/SPU/RSXNET/SAVE/IMPORTS3/VSHUI/RSXSWEEP. Doplněno: 42 SPU FP instrukcí
(frest/frsqest LUT z RPCS3, odvozené), DMA listy, okno SPU skupin, SPU modulová tabulka, `_sys_heap_*`,
NPMG/NPDR offline (chybějící EDAT → ENOENT), trofeje (NPTR), atexit jako guest volání, ESRCH pro
ukončené vlákno (283), NV4097 semafory 0x1d70/0x1d74 (284), RSX rasterizace 3–5× rychleji
(~24 M kroků/s). **284: 1 034 M kroků, stop `nonempty RSX image transfer requires a native blitter`**
(NV3089), stále načítací planeta. Po 284: RSXSWEEP (NV0039/NV3089/NV308A + 272 metod, 373 testů OK),
VSH dialogy MSGD/savedata ovládané padem, vmule/vmulo, mmapper metadata. Viz
[záznam 256–284](knowledge/experiments/2026-10-15-split-units-256.md). Menu ani navigace zatím nedosaženy.

## 285–311 (2026-10-15/16, Claude Opus 5.5 + codex GPT-6.1 Sol)

Podrobnosti v [záznamu 256–305](knowledge/experiments/2026-10-15-split-units-256.md) a
[záznamu 311](knowledge/experiments/2026-10-16-oom-alias-fps-311.md). 296–298: první původní obrazovky
front-endu a navigace virtuálním padem (titulky YES/NO). 302–306: dvě HW vlákna PPU, nové SPU joby, GPU
renderer (Vulkan graphics) na RTX 4070 SUPER. **309: původní hlavní menu, virtuální šipka dolů NEW GAME → LOAD,
START přečten** (snímky `native-virtual-309/evidence-mainmenu-*.ppm`), ~0,7 FPS. 310/311 (RSXVK6/7) se zasekly na
načítání; 311 způsobil OOM celé session — příčina: NVIDIA kompilace 125kB fragment shaderu (emulace quadu).
Běží RSXVK9 (přímý GLSL jako RPCS3). Cílové FPS ani fyzický gamepad zatím neověřeny.

## 312–323 (2026-10-16, Claude Opus 5.5 + codex GPT-6.1 Sol)

Podrobnosti v [záznamu 311](knowledge/experiments/2026-10-16-oom-alias-fps-311.md). Cílové FPS hry ověřeno za běhu:
**59,94** (VSYNC bez stropu). Výkon původního hlavního menu: 309 ~0,7 → 316c ~1,7 → 317 5,9 → 318 6,1–6,4 →
319 9,75–9,99 → 320 11,0–11,3 → **322 15,66 FPS** (titulka za ~2 min místo ~15). Hlavní kroky: přímý GLSL bez
explozí kompilace (RSXVK9), GPU dekódování textur/RTT/VP (RSXVK10–11), per-thread `usleep`, rychlé SPU bloky a FP/DMA
(SPUBLOCK, SPUPERF), persistentní ochrana stránek (RSXVK12), zarovnané guest backingy (320), odložené labely/reporty
(RSXVK13), `-march=native`. Navigace virtuálním padem DOWN → LOAD ověřena v 318–322. Zjištěno: GPU běží v P8
(210–285 MHz) kvůli bursty zátěži; konzervativní paralelní SPU worker (PAR1) je ve hře pomalejší. Běží PAR2 (paměť
bez zámku, vlákno na SPU) a RSXVK14 (Vulkan pipelining). Fyzický gamepad není připojen.

## 324–331 (2026-10-17, Claude Opus 5.5 + codex GPT-6.1 Sol)

Podrobnosti v [záznamu 311](knowledge/experiments/2026-10-16-oom-alias-fps-311.md). Souběžná architektura podle Cellu:
SPU vlákno pro každé SPU (`ANYPS3_SPU_THREADS=1`, PAR2–PAR5, parkování blokujících kanálů), paměť bez globálního
zámku (atomická guest slova, rezervace přes 128B verze, page broker), herní RSX worker (`ANYPS3_RSX_WORKER=1`,
PAR6–PAR7), Vulkan renderer s timeline submity, průběžnými submity a compute dekódováním textur (RSXVK14–19).
Sériový režim 326 → 331: 24,4 → 25,8 FPS; **plně paralelní 331tw: původní menu 38–40 FPS**, navigace DOWN → LOAD
virtuálním padem ověřena. Cíl 59,94 FPS zatím nedosažen; fyzický gamepad nepřipojen.

## 332–337 (2026-10-17, Claude Opus 5.5 + codex GPT-6.1 Sol)

Podrobnosti v [záznamu 332+](knowledge/experiments/2026-10-17-menu-fps-332.md). Herní profily po vláknech (eu-stack,
`profile_menu.sh`, `stall_probe.sh`, `nav_check.sh`) ukázaly, že hlavní vlákno trávilo jen 3 % v přeloženém PPU kódu:
PAR9 (levný koordinátor, blokování bez výjimek), PAR10 (asymetrické hazardy, PPU 16,9 → 5,6 ns/instr v syntetice),
RSXVK20/20b (GPU cleary, jeden claim pro povrchy, neblokující labely), oprava O(S²) `GpuPages::commit_spans`
(~25s zástavy), PAR11 (metadata bez brány/membarrier, CPU dirty bez RPC) a RSXVK21 (FIFO závod s CPU clearem,
stabilní watche, RTT reuse). **337: původní menu 57,5–59,5 FPS (plný takt 59,94 Hz ve většině intervalů), RSX
vlákno 6 ms/snímek, navigace virtuálním padem 10/10.** Zdánlivá ztráta stisků byla auto-repeat hry při 1s držení.
Fyzický gamepad nepřipojen.

## 338–345 (2026-10-17 večer, Claude Opus 5.5 + codex GPT-6.1 Sol)

Podrobnosti v [záznamu 338+](knowledge/experiments/2026-10-17-ingame-338.md). Build 338 (PAR12) byl nepřeložitelný
(> 10 GB na TU, OOM shodil session) → 339 (rychlá cesta out-of-line, menu PPU 11,1 ms/snímek). Za menu: NEW GAME →
původní výběr obtížnosti a jasu → systémový dialog uložení. SAVEFULL (codex) + integrace: schéma podle firmware, overlay
dialogů, hook padu, oprava mapování podmínek kanálu (PacketWrite čekal na 0x86000500), povinný `elmProgress` →
**první skutečný save na HDD** (344), boot ListAutoLoad a **LOAD ze systémového seznamu** (345, USRI getStat).
Okno: FIFO → MAILBOX, menu v okně 20 → 59,9 FPS. Úvodní filmeček běží 8–9 FPS (SPU) → SPUPERF2; na jeho konci
`SPU code differs from static image/overlays` (SPU kód mimo statický obraz) → SPUDYN. Gamepad: virtuální i jaderný
uinput (evdev → SDL → cellPad) ověřen v menu; fyzický HW nepřipojen. Zvuk: hra hlásí 0 zařízení; vývoj jen tiše
(absolutní pravidlo: z PC nikdy slyšitelný zvuk) → AUDIOFULL2.

**2026-10-17/18 — hratelná úroveň a výkon (builds 359–374):** LOAD → `great_clock_a` → hratelná úroveň, Clank ovládán
virtuálním i uinput gamepadem (363). Výkon úrovně: ~1,5 FPS (359) → 4,6–5,3 (369) → 9,1 (372/373, PAR14 deadlock fix,
PPUGEN1 bloky) → **~10 FPS (374, PAR15 selektivní SPU RPC + PROF1 vestavěný sampler)**; cíl 59,94. Snímek ≈ PPU 50 ms
+ RSX 55 ms bez překryvu → PIPE1, PPU paměťové helpery ~35 % → PPUMEM1, CPU RSX → RSXVK22 v24. Detail
`docs/knowledge/experiments/2026-10-17-ingame-338.md`.
Konec 2026-10-18: **377 ~14,8 FPS v úrovni** (RSXVK22 v24 CPU RSX −42 %, SPUDYN2 relokovaný SPU kód místo interpretu,
oprava souběžného `sys_ppu_thread_exit`). Hra čeká ~36 % času na engine counter (0x696c10, `usleep(30)`) → PIPE1.
Původní označení jako RSX fence nebylo doložené; PIPE2 následně určil SPU producenty group2 a4.


**2026-10-19 — pokračování379+:** původní377 artefakty/source/PT_LOAD znovu ověřeny;
379 =377+HLEFAST1, build rc0 a source shoda, původní úroveň14,64 FPS/120s.
380 =379+oprava timeru a atexit testu, **15,02 FPS/134s**, původní úroveň a virtuální pohyb zachyceny.
Oba běhy po měření ukončeny SIGKILL, verification−9/stop null/PT_LOAD obě shody;60 FPS nedosaženo.
PIPE2 prokazuje group2 i4 engine completion counter jako SPU MFC PUTLLUC.
Ve380 group4 vykonává původní mikroprogram0x27, všech5264B ověřeno proti EBOOT;
úplná vazba na dominantní27ms čekání zůstává otevřená.
Potvrzená oprava plánovače: audio100µs nesmí překrýt dřívější PPU timer;11 nativních scénářů PASS.
Zastaralý atexit test nově kontroluje správné blokování wait_kind13 a zachování stavu,2/2PASS.
SPUFP2 dodávka27 cílených testů PASS, PPUMEM2 dokončené izolované testy/sanitizéry; oba neintegrované,
herní přínos neověřen. Celá sada sloučeného380 neprovedena. Toto kolo končí pro pokračování v jiném AI prostředí.
Viz [záznam379+](knowledge/experiments/2026-10-19-performance-379.md).
Před finálním předáním dokončena vyžádaná [rešerše PS3 rekompilací](knowledge/experiments/2026-10-19-web-ps3-recomp.md):
6 konkrétních portů včetně Drakengard3 a Soulcalibur II HD,5 oblastí pro další obecné optimalizace,
34 veřejných podkladů ověřeno; žádný jejich kód se v tomto kole neintegroval.

## 2026-10-19 odpoledne — vlna FAST (Claude Opus 5.5)

Goal uživatele: hra na 60 FPS s rezervou. Ověřeno, že úroveň běží v herním režimu 0 (cíl 59,94 FPS, žádný strop).
Výchozí měření novým `measure_level.sh`: build 380 **14,25 FPS** v `great_clock_a`. Tři subagenti (PPU překlad+paměť,
SPU bloky+FP, RSX cena draw) v izolovaných stromech. Viz [záznam FAST](knowledge/experiments/2026-10-19-fast-wave.md).

Konec session 2026-10-19 ~22:00: integrace 386 = 21,35 FPS, **460 (386 + RSX2) = 22,59 FPS** v původní úrovni
(PPU 2,06 → 0,42 ns/instr, SPU 12,6 → 2,2 ns/instr, RSX worker 38,7 → 28,3 ms/snímek). 60 FPS nedosaženo;
další postup v [záznamu FAST](knowledge/experiments/2026-10-19-fast-wave.md).
