# PPU dělení nulou a původní loading clamp (245)

Cíl zůstává původní funkční menu ACIT a navigace gamepadem. Tento záznam
nepotvrzuje menu ani navigaci. Datum 2026-10-14; Git není dostupný.

## Reprodukce a původní instrukce

**Ověřené:** `native-virtual-244/verification.json`, `stderr.log` a nezávislé
`terminal-identity.json` dokládají stop `0x69f2b4`, LR `0x69f060`,
308 205 078 PPU kroků, `FP division by zero is unsupported`.
f13=154,0 (`0x4063400000000000`), f6=+0,0; FPSCR `0x82004000`.
Původní ELF SHA256 `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`.
Přesná slova, lift a operandová evidence: `local/ratchet/target-245-evidence.json`.

Původní posloupnost `0x69f2b0..0x69f2c0`: FRSP, FDIVS (`ed8d3024`),
FSUB, FSEL, STFS. FSEL vybírá f8=0,75 při nezáporném výsledku FSUB.
**Odvozené:** korektní +Infinity z dělení a následné odečtení 0,75 umožní
původnímu kódu uložit 0,75; dělení není požadavkem na přeskok instrukce.

## Zdroje a hranice

**Ověřené z textu původního manuálu:** IBM *PowerPC User Instruction Set
Architecture, Book I*, v2.02, January 28, 2005,
[původní PDF na mirroru](https://powerpc.dev/general/PPC_Vers202_Book1_public.pdf):
fyzické strany 97–99 (FPSCR, FPRF), 101 (infinity/NaN), 107–108
(invalid operation a zero divide; tištěné strany 97–98).
Při ZE=0 vrací konečné nenulové číslo / nula nekonečno se XOR znaménkem,
nastavuje ZX, čistí FR/FI a klasifikuje FPRF. 0/0 a Infinity/Infinity jsou
jiné, invalid operace (VXZDZ/VXIDI); protilehlá nekonečna při sčítání VXISI.
Generovaný quiet NaN je `0x7ff8000000000000`.

Cell Architecture v1.02, původní lokální PDF strana 47, sekce 4.1,
uvádí kompatibilitu Book I bez inkompatibilit a současně varuje, že CBEA
automaticky nepřebírá pozdější rozšíření PowerPC. Proto zde není použita
novější Power ISA jako tichá náhrada. Lokální hash Cell PDF je v manifestu.

Doplňkový primární zdroj: Motorola/IBM *PowerPC Microprocessor Family:
The Programming Environments*, Rev.1, 1/97,
[PDF u NXP](https://www.nxp.com/docs/en/user-guide/MPCFPE_AD_R1.pdf),
fyzická strana 145, tabulka 3-13, potvrzuje ZE=0 výsledek ±Infinity.
RPCS3 `PPUInterpreter.cpp:6198` a `:6536` při revizi
`ffba2bf09ef3980638f9327b25240d81414034ab` provádí host division; tato
reference sama není důkaz úplné FPSCR sémantiky a algoritmus nebyl kopírován.

**Omezení získání zdrojů:** web nástroj otevřel Book I (230 stran) a NXP PDF
(828 stran). Lokální download Book I přes curl selhal DNS (log
`/tmp/anyps3-powerpc-download.log`); nový lokální PDF ani jeho SHA256 nebyly
získány. Požadavky web screenshotů tabulek/exception stran většinou skončily
cache miss. Závěry mají oporu v textu původních PDF a shodě obou primárních
zdrojů, nikoli v tvrzení o úplné nové lokální knihovně nebo rekonstrukci tabulek.

## Trvalá změna a testy

`anyps3/floating.py:36`: nové výjimkové příznaky nastavují FX jen při
přechodu konkrétního sticky bitu 0→1; invalid případy nastavují VX.
`:165` podporuje speciální dělení v obou přesnostech; `:205` podporuje
navazující sčítání/odečítání s nekonečny. FR/FI a FPRF se aktualizují,
starší exception bity zůstávají sticky. Finite arithmetic algoritmus nezměněn.
Nepodporované rounding/NI/enabled-trap režimy dál explicitně zastavují.
Další instrukce s nekonečny nejsou touto změnou obecně vyřešeny.

`tests/test_floating.py:34`: nativní regresní vektory mají literální IEEE/FPSCR
očekávání, testují oba rozměry přesnosti, XOR znaménka, subnormal/zero,
0/0, Infinity/Infinity, Infinity/0, finite/Infinity, NaN precedence,
sticky FX/ZX/VX, FR/FI, CR1 record, destination aliases a původní pětici
instrukcí včetně big-endian guest store hodnoty `0x3f400000`.
Unsupported-mode test kontroluje nezměněný FPR, PC, CR a FPSCR.
Nový test před opravou prokazatelně selhal na původním explicitním stopu:
`local/ratchet/tests-fp-245-before.log`. Po změně šest cílených testů OK
(10,194 s), `local/ratchet/tests-fp-245-final.log`.

Zdrojový hash `floating.py`: `e295daf36fc5155ae1d357931e6102322c28a67b939eacbdc37542bf2597dcab`;
test hash: `053da24063cd9c49c04218ec769d65d3240bdfde646b38bcdecf65a3eaad4ca6`.
Profil 245 je byte-identický s 244; jediná runtime source změna je floating.py.

## Nové sestavení a ověření

```bash
python -m unittest tests.test_floating -q > local/ratchet/tests-fp-245-final.log 2>&1
ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q > local/ratchet/tests-native-245.log 2>&1
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf --firmware local/ratchet/firmware-verified/dev_flash/sys/external --key-file local/ratchet/app-key.json --profile profiles/ratchet-acit.json --max-steps 5000000000 -o local/ratchet/native-bundle-245 > local/ratchet/build-245.log 2>&1
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-245 local/ratchet/native-virtual-245 > local/ratchet/build-virtual-245.log 2>&1
```

Celá regresní sada je při prvním zápisu živá v session 10130; původní build
245 v session 72516 úspěšně dokončen. C++ kompilace diagnostického SDL
obalu živá v session 50860. Herní 245 zatím nespouštěno. Ovladač bude
neutrální; přesné potvrzení runtime posunu vyžaduje následný skutečný běh.

### Aktualizace po kompilaci a celé sadě

**Ověřené:** 175 testů, 202,918 s, OK bez skips; Vulkan povinný a skutečně
vykonán podle `tests/test_vulkan.py`. `tests-native-245-result.json` potvrzuje
exit 0. Session 10130 ukončena; kompilace 50860 exit 0.
Nové ELF původní PT_LOAD, source/profile hashe a byte-identický base C++
ověřeny; diagnostické tři hashe manifestu ověřeny po kompilaci. Důkaz
`native-virtual-245/input-identity.json` je vstupní, nikoli terminální/menu.

```bash
ANYPS3_VIRTUAL_PAD_CONTROL=/home/martink/AnyPS3/local/ratchet/native-virtual-245/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-245 5000000000 > local/ratchet/run-virtual-245.log 2>&1
```

Skutečná hra 245 živá v session **6682**. Herní vstupy/control zůstávají
neutrální. Není znovu spuštěný 244 a není pouze syntetický test.
První další akce: pollovat stejný handle, porovnat jeho skutečný stop/captures;
po terminálu zahrnout vlastní verification a diagnostický manifest/base hash.

V 17:14:00 UTC stejný handle potvrzen živý, bez STOP a bez terminálního
verification: 231 čtení / 11 634 763 bajtů, draw 6400, 13 captures.
Snímek `frame-000013.png` vizuálně potvrzuje původní načítací planetu.
FP stop dosud tímto během není překročen; žádné menu/nav tvrzení.

### Konečný skutečný běh 245 (17:27:41 UTC)

**Ověřené:** session 6682 ukončena, hra returncode 1; nový stop
`pc=0x69dd30`, `lr=0x69dabc`, 308 156 479 PPU kroků:
`target has no statically translated block`. FPSCR `0x86002000` obsahuje
sticky ZX; původní hra tedy skutečně vykonala podporované dělení nulou.
Přesné předchozí PC této operace log netrasuje; nezaměňovat tento důkaz
za instruction trace konkrétního `0x69f2b4`. Jeho přesná původní posloupnost
je samostatně prokázaná nativní regresí. FP zero-divide už tento běh nezastavilo.

444 čtení / 17 897 130 bajtů, oba původní front_end archivy otevřeny.
SPU 0x100 878 746 602 instrukcí, waiting; 15 252 flipů, 30 496 drawů,
33 captures, prezentace do okna 0. Snímek 33 stále načítací planeta.
Vlastní `verification.json` a nezávislé `terminal-identity.json` ověřeny:
šest artefaktových a tři diagnostické hashe, identický base překlad a všechny
původní PT_LOAD bajty. Ovladač zůstal neutrální, menu/nav nejsou prokázané.
Nový stop je případ 13 původní tabulky; pokračování v
[jump table 246](2026-10-14-byte-switch-246.md).
