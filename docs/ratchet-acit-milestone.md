# Ratchet & Clank: A Crack in Time — první milník

Historický stav prvního průchodu. Download mezitím doběhl; aktuální výsledky
úplné extrakce a paměťového runtime jsou ve [druhém milníku](ratchet-acit-memory-milestone.md).

Datum: 2026-10-13. Cíl: ověřit AnyPS3 na skutečné binárce, získat konkrétní
mapu další práce a nativně spustit první přeložené funkce.

## Dosažený výsledek

Ze souborů poskytnutých uživatelem byl získán celý EBOOT. Všechny převzaté
programové segmenty a tabulka sekcí prošly kontrolou HMAC-SHA1. Dvě malé
funkce byly přeloženy do C++, sestaveny místním kompilátorem a spuštěny
nativně. Vrací očekávané hodnoty 5 a 21.

Jde o jednoduché návratové funkce se čtyřmi instrukcemi v dosažitelné cestě,
nikoli o inicializaci enginu, renderování nebo spuštění celé hry.

## Vstup a úplnost

| Položka | Zjištění |
|---|---|
| Identita z PARAM.SFO | BCES00511, APP_VER 01.00, VERSION 01.00 |
| Minimální systémová verze z PARAM.SFO | 03.0100 |
| Svazek ISO | RATCHET5_5 |
| Stažený archiv | 18 197 200 824 bajtů, přípona `.zip.crdownload` |
| Deklarovaná komprimovaná velikost ISO v ZIP | 20 806 817 955 bajtů |
| Obnovená část ISO | 21 956 151 873 bajtů |
| Deklarovaná velikost ISO | 24 772 083 712 bajtů |
| Soubory | 811 unikátních souborů; 661 rozbalených, 150 nedostupných |
| Rozbalená data | 21 949 243 531 bajtů |
| EBOOT.BIN | 14 517 840 bajtů, retail APP SELF, revize klíče 1 |
| Analytický ELF | 14 515 408 bajtů, ELF64 big-endian PowerPC64 |

Počáteční inventář obsahoval 812 záznamů souborů. Soubor
`GLOBAL_UNCACHED.PSARC` má dva extenty; po jejich spojení je počet 811.
Extrakce je podporuje a nesmí druhým extentem přepsat první.

Archiv je neúplný a CRC celého ISO nelze ověřit. Extrahovaly se jen soubory,
jejichž všechny extenty leží v obnovené části obrazu. Původní stažený soubor
nebyl upraven. Seznam chybějících souborů je v lokálním manifestu extrakce.

SHA-256 EBOOT.BIN:
`0d3c7e78fc66a8f964fbaab9d90436b717a8f60ec999f1245d1134131e7e9f83`

SHA-256 analytického ELF:
`6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`

## Rozsah ověření SELF

Převzato je pět programových sekcí (včetně tří prázdných) a tabulka ELF sekcí.
Všech šest má úspěšně ověřený HMAC. Nealokovaná pomocná data SELF typu 3 se
nepřenášejí; tento blok nemá význam load segmentu. Původní jména sekcí nejsou
k dispozici a identifikují se indexy a adresami.

Analytický ELF **není bajtově přesná rekonstrukce původního ELF**. Mezery
a neobnovená data mimo load segmenty jsou nulové. SHA-1 uvedený v původním
SELF se proto neshoduje; stav je výslovně uveden v reportu. ECDSA podpis SELF
se neověřoval. Shoda HMAC ověřuje převzatý obsah, nikoli pravost celé distribuce.

Formát a výběr parametrů pro APP revizi 1 byly ověřeny proti
[RPCS3 unself](https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Crypto/unself.cpp)
a [RPCS3 key vault](https://github.com/RPCS3/rpcs3/blob/master/rpcs3/Crypto/key_vault.cpp).
Parametry pro lokální extrakci jsou uloženy pouze pod ignorovaným `local/`;
nástroj žádné klíče neobsahuje ani nestahuje.

## Mapa binárky

| Položka | Výsledek |
|---|---|
| ELF entry / OPD | `0x00ca6d48` |
| První instrukce | `0x00010280` |
| TOC | `0x00d34a88` |
| Sekce OPD | index 22, adresa `0x00ca6d38`, velikost 548 176 bajtů |
| Validní OPD deskriptory | 68 522 |
| Unikátní adresy kódu z OPD | 34 512 |
| Importované funkce | 246 ze 17 knihoven |
| Vložené strukturálně validní SPU ELF | 3 |
| Vstupy přijímané současným překladačem | 1 370, limit 256 instrukcí na vstup |

Adresy z OPD nejsou úplný seznam všech funkcí hry. Počet přijatých vstupů
obsahuje prázdné návraty, triviální funkce a stuby. Není to procento
hratelnosti; hromadný průchod je statická kontrola, ne test spuštění všech funkcí.

Vložené SPU ELF začínají na souborových offsetech `0x00be4480`,
`0x00bedc00` a `0x00bfc300`. SPU programy zatím nebyly vykonány.

| Knihovna | Importy |
|---|---:|
| cellSpurs | 36 |
| cellSysutil | 32 |
| cellHttp | 28 |
| sysPrxForUser | 27 |
| sys_fs | 19 |
| sceNp | 18 |
| cellGcmSys | 16 |
| cellNetCtl | 12 |
| sys_io | 12 |
| cellSsl | 11 |
| sceNpTrophy | 11 |
| sys_net | 8 |
| cellAudio | 7 |
| cellSysmodule | 4 |
| cellHttpUtil | 2 |
| cellRtc | 2 |
| cellUserInfo | 1 |

NID a adresy importů jsou v JSON reportu; jména jednotlivých funkcí zatím
nejsou vyřešena. Statický import neznamená, že ho hra vyvolá při startu.

## Nativní ověření

| OPD | Adresa kódu | Počet instrukcí | Výsledek |
|---|---|---:|---|
| `0x00d21cd8` | `0x009d5548` | 4 | `r3=5` |
| `0x00d21d00` | `0x009d5588` | 4 | `r3=21` |

Výstupy byly sestaveny přes `c++ -std=c++17 -O2 -Wall -Wextra -Werror`.
Tyto konkrétní funkce nepotřebují hostitelský ekvivalent paměti ani systémových
služeb. Testovací `main` není vstupem celé hry.

## První skutečná překážka

Překlad startu zpracuje tři instrukce nastavující registr `r2` a zastaví se:

```text
0x0001028c: 0x80420004 — lwz r2,4(r2)
```

Následují `stdu`, `std` a `bl`. Z další práce má tedy přednost:

1. Mapa paměti ELF, big-endian load/store a inicializace zásobníku.
2. `lwz`, `ld`, `std`, `stdu`, zachování LR a volání funkcí.
3. Podmíněné větvení a diagnostika první skutečně vyvolané systémové služby.
4. SPU/SPURS a grafika podle toho, kam se skutečný běh dostane.

## Lokální artefakty a opakování

Herní data a generovaný kód zůstávají v ignorované složce `local/ratchet/`.

| Cesta relativně k projektu | Obsah |
|---|---|
| `local/ratchet/disc.partial.iso` | Obnovená část ISO (smazáno 2026-10-14, nahrazeno `disc.complete.iso`) |
| `local/ratchet/game/` | 661 rozbalených souborů a manifest (smazáno 2026-10-14, nahrazeno `game-complete/`) |
| `local/ratchet/recovery.json` | Stav obnovy neúplného ZIP |
| `local/ratchet/disc-inventory.json` | Inventář a dostupnost souborů |
| `local/ratchet/extraction.json` | Hash každého vytaženého souboru a chybějící cesty |
| `local/ratchet/EBOOT.analysis.elf` | Analytický ELF |
| `local/ratchet/self-integrity.json` | Ověření HMAC, hashe a omezení rekonstrukce |
| `local/ratchet/analysis.json` | Sekce, importy, SPU a výsledky průchodu překladačem |
| `local/ratchet/leaf-return5`, `leaf-return21` | Sestavené nativní testy |

```bash
python3 -m anyps3 sfo local/ratchet/game/PS3_GAME/PARAM.SFO
python3 -m anyps3 inspect local/ratchet/EBOOT.analysis.elf \
  --profile profiles/ratchet-acit.json --json
python3 -m anyps3 analyze local/ratchet/EBOOT.analysis.elf
./local/ratchet/leaf-return5
./local/ratchet/leaf-return21
python3 -m unittest discover -s tests -v
```

Novou extrakci lze provést přes `recover-zip`, `disc-extract --allow-partial`
a `unself --key-file`. Výstupní soubory a cílová složka musí být nové.
Aktuální testovací sada má 43 testů; všechny prošly, včetně syntetického
šifrovaného SELF, ZIP64, multi-extent ISO a nativního sestavení.
