# Ratchet & Clank: A Crack in Time — kompletní disk a paměťový runtime

Datum: 2026-10-13. Navazuje na [první milník](ratchet-acit-milestone.md).

Download doběhl. Celý obraz prošel CRC, všech 811 souborů je rozbaleno
a první přeložená funkce skutečně čtoucí data hry byla sestavena a spuštěna
nativně. Hra zatím nenaběhne; volání funkcí a systémové služby nejsou hotové.

## Integrita a extrakce

| Položka | Ověřený výsledek |
|---|---|
| ZIP | 20 806 818 527 bajtů, platný čitelný centrální adresář, 2 položky |
| ISO | 24 772 083 712 bajtů, CRC32 `7b8065f1` odpovídá ZIP |
| README v ZIP | CRC ověřeno samostatným přečtením přes `zipfile` |
| Extrakce ISO | 811 souborů, 0 chybějících, 24 767 909 758 bajtů |
| Srovnání s částečnou extrakcí | SHA-256 všech 661 dříve získaných souborů souhlasí |
| Nově získané soubory | 150, včetně `DEBUG.DAT` a dalších videí/dat |

Kontrola CRC ISO proběhla během úplné dekomprese nástrojem `recover-zip`.
Čtení centrálního adresáře a README bylo ověřeno navíc přes standardní
knihovnu Pythonu. Samotný `recover-zip` dál kontroluje pouze ISO položku,
proto v jeho reportu zůstává `source_archive_complete: null`.
Souhrn obou kontrol je v `local/ratchet/complete-verification.json`.

EBOOT i analytický ELF mají stejné hashe jako v prvním milníku. Profil
BCES00511 01.00 zůstává platný. Omezení rekonstrukce SELF a neověřeného
ECDSA podpisu popsaná v prvním milníku se nemění.

`DEBUG.DAT` má 502 843 bajtů a začíná `IGHW`. Jednoduchý průchod nalezl
15 299 tisknutelných ASCII řetězců délky alespoň 5, mezi nimi názvy tříd,
enumů a objektů. To je vodítko pro další rozbor dat; zatím nemáme ověřenou
vazbu těchto názvů na adresy PPU funkcí ani parser celého formátu.

## Nová implementace

Překladač podporuje D-form instrukce `lwz`, `lbz`, `lhz`, `lha`, `stw`,
`stb`, `sth`, jejich update varianty a DS-form `ld`, `ldu`, `lwa`, `std`,
`stdu`. Neplatné update operandy a rezervované DS varianty odmítá.

Generovaný C++ paměťový runtime načte PT_LOAD segmenty z původního ELF,
doplní BSS nulami a vytvoří 1MiB zásobník s počátečním SP `0xd00fff00`.
Big-endian přístupy kontrolují rozsahy a práva; adresa hry nikdy není přímo
hostitelský ukazatel. Zápis do spustitelných segmentů, překryvy mapování,
přístupy přes hranici segmentu a mapa přes 512 MiB se odmítají. Paměťové
chyby uvádějí adresu vykonávané instrukce.

Program vyžaduje ELF jako argument. Před načtením porovná velikost a CRC32
se vstupem při generování. CRC slouží proti neúmyslné záměně, není podpisem
ani kryptografickým ověřením. Bezpaměťové demo nadále funguje bez argumentu.
Zásobník je testovací; nemá argumenty procesu, TLS ani inicializaci enginu.

## Nativní důkaz na skutečné funkci

| Položka | Hodnota |
|---|---|
| OPD | `0x00d2bec8` |
| Kód | `0x009011b8` |
| TOC | `0x00d34a88` |
| Instrukce | `lwz r3,-6352(r2); blr` |
| Čtená adresa | `0x00d331b8` |
| Bajty v ELF | `00 de 50 d8` |
| Očekávané a skutečné r3 | `14569688` (`0x00de50d8`) |
| Návratový kód nativního programu | 0 |

Očekávaná hodnota byla nezávisle přečtena z mapování ELF v Pythonu a porovnána
s výstupem nativního programu. Jde o izolovanou dvouinstrukční funkci nad
počátečními daty ELF, nikoli o běh enginu nebo důkaz funkčnosti načteného objektu.

Statický průchod nyní přijímá 2 971 z 34 512 unikátních vstupů z OPD
(dříve 1 370), při limitu 256 instrukcí. Počet zahrnuje triviální funkce
a neověřuje platnost argumentů ani běhového stavu. Není to procento hratelnosti.

Překlad startu nyní projde přes první `lwz`, `stdu` a `std`, ale zastaví se na:

```text
0x0001029c: 0x48000115 — bl 0x000103b0
```

Celý start se negeneroval ani nespouštěl. Další konkrétní úkol je LR,
volání/návraty, následně podmíněné větvení a první skutečně dosažená služba.

## Ověření a opakování

Všech **54 testů prošlo**. Paměťové testy sestavují a spouštějí syntetické
programy pro endianitu, nulování BSS, šířky a znaménka loadů, update formy,
záporný DS displacement, zachování starého SP při `stdu`, neplatné mapování,
přístupová práva a záměnu vstupního ELF. Vybrané opkódy byly navíc ověřeny
lokálním LLVM assemblerem. Nativní sestavení používá C++17,
`-O2 -Wall -Wextra -Werror`.

```bash
python3 -m unittest discover -s tests -v
python3 -m anyps3 inspect local/ratchet/EBOOT.analysis.elf \
  --profile profiles/ratchet-acit.json --json
./local/ratchet/leaf-memory-verified local/ratchet/EBOOT.analysis.elf
```

Pro nový překlad použijte nové výstupní jméno:

```bash
python3 -m anyps3 recompile local/ratchet/EBOOT.analysis.elf \
  --entry 0xd2bec8 -o local/ratchet/leaf-memory-new.cpp
c++ -std=c++17 -O2 -Wall -Wextra -Werror \
  local/ratchet/leaf-memory-new.cpp -o local/ratchet/leaf-memory-new
./local/ratchet/leaf-memory-new local/ratchet/EBOOT.analysis.elf
```

| Lokální artefakt | Obsah |
|---|---|
| `local/ratchet/disc.complete.iso` | Kompletní ISO, ověřené CRC |
| `local/ratchet/game-complete/` | Kompletní extrakce s manifestem a SHA-256 každého souboru |
| `local/ratchet/complete-verification.json` | Souhrn integrity a srovnání s částečnou extrakcí |
| `local/ratchet/recovery-complete.json` | Ověření dekomprese a CRC ISO položky |
| `local/ratchet/disc-inventory-complete.json` | Kompletní inventář ISO |
| `local/ratchet/analysis-memory.json` | Nový statický průchod EBOOT |
| `local/ratchet/native-memory-verification.json` | Porovnání očekávané a nativně načtené hodnoty |
| `local/ratchet/leaf-memory-verified.cpp`, `leaf-memory-verified` | Generovaný kód a nativní program |
| `local/ratchet/debug-strings.txt` | Pomocný výpis řetězců DEBUG.DAT |
| `local/ratchet/tests-memory.log` | Výsledky celé testovací sady |

Herní data, lokální parametry klíče, odvozený kód a velké obrazy zůstávají
v ignorované složce `local/`. Původní archiv ani částečná extrakce se neměnily.
