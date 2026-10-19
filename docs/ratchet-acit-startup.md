# Nativní start Ratcheta a knihoven firmwaru

Ověřeno 2026-10-14 na Linuxu x86-64, sestavení `native-bundle-121`. **Hra není hratelná a nevytvořila herní snímek.**
Cíl „dostat se do hry“ zůstává nesplněný.

Následný grafický milník přidal [Vulkan čištění plochy](vulkan-rendering.md),
ověřené samostatným syntetickým RSX FIFO a kontrolou všech pixelů. Níže uvedená
čísla popisují herní start sestavení 121, před přidáním tohoto backendu.
Aktuální [postup směrem k menu](menu-progress.md) již zahrnuje herní clear
a čtyři zachycené černé snímky; menu stále chybí.

## Co skutečně proběhlo

Původní kód hry a třinácti knihoven firmwaru se před spuštěním překládá do
C++17 a hostitelský kompilátor jej sestaví pro x86-64. Totéž platí pro SPU
bootstrap, firmware SPURS a dva další bloky původního herního správce úloh.
Za běhu se vybírají předem přeložené operace. Není přítomen dekodér instrukcí
PPU/SPU, JIT ani backend RPCS3. Registry a big-endian paměť zůstávají explicitním
stavem programu; nejde o rekonstrukci původních zdrojových kódů.

Dosavadní běh překročil **102 milionů kroků PPU**. Původní engine dokončil
náročnou inicializační smyčku, která zapisuje procedurální data textur do VRAM,
a pokračoval do další inicializace. Šest SPU kontextů celkem dokončilo
**24 360 instrukcí**. Počty kroků nejsou procentem dokončeného portu.
Kroky PPU zahrnují také přechody importů a návraty modulů.

Původní engine vypsal:

```text
Build Config - Final

total mem:        256.000
application mem:  234.948
global alloc:     233.000
system heap:      1.948
```

Při startu engine získal 233 MiB globální paměti a 249 MiB VRAM se skutečným
hostitelským uložením. Vlákno SysUtil později uvolnilo 3 MiB hlavní paměti
pro systémový kontejner.
Vzniklo devět PPU kontextů: hlavní vlákno, `SpursHdlr1`, `SpursHdlr0`,
`PuJobDispatch`, `_gcm_intr_thread`, `igSpursHdlr1`, `igSpursHdlr0`
`igSpuPrintHandler` a `SysUtilThread()`. Mají oddělené registry, TLS a priority; podporované
blokující operace předávají řízení jinému kontextu.

## Vstupy a firmware

Vydání je BCES00511 01.00, analytický EBOOT má SHA-256
`6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`.
Původ a úplnost disku popisuje [paměťový milník](ratchet-acit-memory-milestone.md).

Firmware **3.01** pochází z přiloženého `PS3UPDAT.PUP`. Offline extraktor
ověřil HMAC sedmi položek PUP a převzatých sekcí `dev_flash`: 905 souborů,
162 818 260 bajtů. ECDSA podpisy se neověřují. Klíče ani herní soubory nejsou
součástí distribuovaného kódu.

Společný obraz obsahuje `libnet`, `libssl`, `libhttp`, `libsysutil`,
`libsysutil_userinfo`, `libmic`, `libfs`, `libnetctl`, `libsysutil_np`,
`librtc`, `libsysutil_np_trophy`, rezidentní `libsre` a rezidentní `libgcm_sys`.
Původní `module_start` GCM se vykoná před hrou. Inicializace požadovaných
modulů ID 0, 1, 2, 3, 4, 9, 14, 20, 21, 22, 34, 50 a 61493 skončila.
To není ověření všech API těchto modulů.

Parser původního `cellSysutil` zpracoval nativní odpovědi na připojení IPC,
dotaz i změnu konfigurace videovýstupu, jazyk, potvrzovací tlačítko a metadata
startu. Metadata pocházejí ze skutečného PARAM.SFO připojeného disku.

`cellSysCacheMount` a `cellSysCacheClear` mají nativní veřejné API. Cache
`BCES00511` se skutečně připojila k `/dev_hdd1`, s pracovním hostitelským
adresářem pod explicitním `ANYPS3_CACHE_ROOT`. První připojení vytváří prázdný
adresář a vrací CLEARED; opakované připojení stejného ID v procesu zachová
obsah a vrací RELAYED. Vymazání skutečně odstraní obsah tohoto adresáře.
Mezi procesy se cache nepřenáší a její adresáře zůstávají jako diagnostické
artefakty. Interní VSH požadavek `cacheInit` nadále není implementovaný.

Paměťové kontejnery rezervují skutečný rozpočet hlavní paměti, reportují
velikost a volnou část a při zrušení rozpočet vrátí. Nelze zrušit kontejner
s používanou pamětí. Alokace z kontejneru bude vyžadovat další službu.

## Grafická inicializace

Původní `cellGcmInit` vytvořil grafický kontext a obslužné vlákno. Nativní
runtime mapuje zařízení na `0x60000000`, DMA na `0x60100000`, informace
ovladače na `0x60200000` a reports na `0x60300000`. I/O oblast velikosti
233 MiB od offsetu nula odkazuje na skutečnou hlavní paměť od `0x40000000`.
VRAM začíná na `0xc0000000` a účtuje se odděleně od hlavní paměti.

FIFO vykonává pořadí příkazů, GET/PUT, jump/call/return, reference a semafory.
Čekání na semafor se uvolní až po skutečné změně příslušné paměti. Neznámý
příkaz zastaví běh. Dosavadní inicializace zpracovala **331 paketů** a uložila
**511 hodnot konfiguračních registrů**, GET = PUT = `0x1430`.
Samotné uložení registru nepotvrzuje vykreslení jeho účinku.

Engine nastavil RGB výstup 1280 × 720, pitch 5120 a dva displejové buffery:
ID 1 na offsetu `0`, ID 2 na `0x398000`. Nastavil také následující dlaždice:

| ID | Offset ve VRAM | Velikost | Pitch | Komprese |
|---|---|---|---|---|
| 2 | `0x0000000` | `0x880000` | 5120 | 7 |
| 1 | `0x0880000` | `0x580000` | 8192 | 7 |
| 0 | `0x0e00000` | `0x580000` | 8192 | 11 |
| 4 | `0x1380000` | `0x2c0000` | 4096 | 11 |
| 5 | `0x1640000` | `0x2c0000` | 4096 | 0 |

Dvě Z-cull oblasti mají rozměry 960 × 704. Jde o ověřené popisy oblastí;
komprese dlaždic ani vlastní Z-cull výpočty zatím nejsou implementované.
Skutečné kreslení, upload shaderů a neprázdné blity nejsou implementované.
Nový Vulkan backend podporuje jen úplné barevné čištění lineární ARGB8 plochy
bez MSAA a dlaždic; herní komprimované plochy zatím nepokrývá.
Nulové čištění a blit s prázdným obdélníkem nemají
obrazový účinek. Není vytvořen hostitelský herní snímek ani otevřené herní okno.

`cellGcmSetFlipMode` a jeho druhá varianta mají nativní implementaci veřejného
API: kontrolují argumenty a nastavují politiku výstupu. Nevykonávají interní
nedokumentované volání `sys_rsx_attribute(677)`, které zůstává nepodporované.
Toto není důkaz dokončeného flipu ani implementace prezentace.

## Vstupní zařízení

`cellPadInit`, `End`, `GetInfo2`, `SetPortSetting` a `GetData` používají SDL2
na hostiteli. Podporují připojení/odpojení, skutečná tlačítka, analogové osy,
změnové příznaky a tlakové hodnoty spouští. Pohyb a vibrace nejsou inzerované.
V ověřovaném startu nebyl připojen fyzický ovladač a služba správně ohlásila
**nula zařízení**. Integrační test připojuje skutečné virtuální zařízení SDL,
řídí jeho vstupy a ověřuje zprávy PS3 i odpojení. Nejde o test ovládání hry.

## SPU a původní herní správce úloh

Skupina `CellSpursKernelGroup` má jedno vlákno. Další skupina
`igCellSpursKernelGroup` typu `0x18` má pět pracovních vláken bez kontextového
přepínání. Rezervují fyzické SPU a neúčtují neexistující úložiště uloženého
kontextu do hlavní paměti. Každé má vlastní skutečnou 256KiB lokální paměť.

Bootstrap má vstup LS `0x838`. Obrazy a overlaye jsou v profilu svázané
s SHA-256. Každá vykonaná operace kontroluje přesné původní slovo; pro
překrývající se overlaye se předem přeloží známé varianty. Změněný či nový
kód nemůže spustit interpreter nebo automatický překlad za běhu.

| Zdroj | Cíl LS | Bajtů | Význam |
|---|---|---|---|
| `0x20268880` | `0x00a00` | `0x1f80` | Původní firmware SPURS |
| `0x00ad9d00` | `0x00a00` | `0x230` | Původní herní workload |
| `0x00ad9780` | `0x3f580` | `0x510` | Původní herní správce úloh |

GETLLAR/PUTLLC sledují skutečné rezervace včetně zápisů přes aliasy.
Čekání na ztrátu rezervace ponechá PC na instrukci a uvolní procesor.
SNR kanály podporují oznámení PPU, přepsání/OR, nulové hodnoty i blokující
čtení. Dosavadní stavy jsou 4 981 dokončených instrukcí v prvním vlákně,
čtyřikrát 3 916 a jednou 3 715 v herní skupině. Herní správce úloh čeká na
LS `0x3f760` na skutečnou změnu paměti; není násilně označený za dokončený.

Plánovač je kooperativní a DMA synchronní. Není časově věrný PS3. Další
události, mailboxy, úplná ISA a úplný životní cyklus vláken stále chybí.

## Matematika a její hranice

Skalární FMADD/FMSUB a záporné varianty mají jediné zaokrouhlení a jsou
ověřené proti nezávislému racionálnímu výpočtu. VMX zahrnuje další načítání,
permutace, splaty, celočíselnou aritmetiku, porovnání, konverze, sčítání a FMA.
Zachovává skalární FPSCR a samostatně aktualizuje CR6/VSCR. Vyjmenované operace
zpracovávají i nekonečna a NaN; další neimplementované případy zastavují běh.

`vrefp` používá přesně zaokrouhlenou reciprokou hodnotu. Nereprodukuje
odhadovou tabulku Cell, takže její poslední bity a následné výpočty se mohou
lišit od PS3. Nepodporované podnormální výsledky, přetečení a režim VMX
non-Java zůstávají explicitními chybami. Původní texturová rutina tedy
prošla nativně, její výstup ale není ověřený proti snímku paměti skutečné PS3.

## Reprodukce

S přítomnými lokálními daty a novým výstupním adresářem:

```bash
python -m anyps3.build_game local/ratchet/EBOOT.analysis.elf \
  --profile profiles/ratchet-acit.json \
  --firmware local/ratchet/firmware-verified/dev_flash/sys/external \
  --key-file local/ratchet/app-key.json --max-steps 200000000 \
  -o build/ratchet-native

c++ -std=c++17 -O0 -Wall -Wextra -Werror \
  build/ratchet-native/program.cpp -o build/ratchet-native/program

mkdir -p local/ratchet/cache
ANYPS3_DISC_ROOT=local/ratchet/game-complete \
  ANYPS3_CACHE_ROOT=local/ratchet/cache \
  ./build/ratchet-native/program build/ratchet-native/program.elf

python -m unittest discover -s tests -q
```

Běh vyžaduje sdílenou knihovnu SDL2 pro Linux. Výchozí limit jednoho milionu
kroků nestačí pro původní generování textur. Volba `--trace` zaznamenává každý
krok PPU; pro tento dlouhý průchod by vytvářela několik gigabajtů diagnostiky.

**127 testů prošlo.** Nativní testy zahrnují kontexty a TLS, priority,
rezervace PPU/SPU včetně ABA, DMA, SNR, překrývající se statické overlaye,
rezidentní inicializaci PRX, vstup SDL, FIFO RSX a konfiguraci paměti,
skalární FMA a VMX. Testy nevyžadují herní soubory a nejsou testem hratelnosti.

## Aktuální zastavení a artefakty

```text
STOP pc=0x201142e8 lr=0x20113aa8 steps=102817041:
unsupported native sysutil request family: initGameData
```

Jde o původní požadavek GDAT `initGameData`, 176 bajtů na kanálu 1,
v novém vlákně `SysUtilThread()`. Engine předtím skutečně odmapoval a uvolnil
tři 1MiB objekty a vytvořil 3MiB paměťový kontejner. Následující práce musí
implementovat kontrolu a vytváření herních dat nad skutečným úložištěm,
včetně odpovědí a callbacků očekávaných původním firmwarem. Úspěch se nyní
nevrací. Program končí kódem **1**. Poté stále zbývá renderer, souborové
služby pro herní obsah, zvuk a další dosažená rozhraní.

Běh provedl **102 817 041 kroků PPU**, **24 360 instrukcí SPU** a **331 paketů
RSX**. Kompletní stopa PPU je pro tento dlouhý běh vypnutá; počet různých
vykonaných adres proto neuvádíme. Stavy SPU a FIFO jsou vypsané při zastavení.

Lokální artefakty v `local/ratchet/native-bundle-121/`:

- `program.cpp`, `program.elf`, `program`: zdroj AOT, paměťový obraz a binárka.
- `build.json`: identity vstupů, modulů, statických cílů a overlayů.
- `verification.json`: hashe artefaktů, přesné zastavení, počítadla a porovnání
  všech původních herních PT_LOAD bajtů se společným obrazem.
- `stderr.log`: importy, syscally, přepínání vláken a úplná stopa SPU.
- `stdout.log`: skutečný výstup enginu.

Log kompletních testů je `local/ratchet/tests-native-121.log`. Souhrnná evidence
uvádí také hashe zdrojových modulů a profilu. Původní EBOOT zůstal beze změny
a všechny jeho souborově podložené PT_LOAD bajty jsou zachované ve společném
obrazu. SHA-256 společného obrazu je
`2a14cfb8f0ce93297384e7dc3663b1e0e5b008723aa4e30409deaef6e090422b`.
Mezibuildy jsou pracovní diagnostika; aktuální reprodukovatelný stav je build 121.

## Lokální technická reference

Uživatel dodal RPCS3 do `local/reference/rpcs3`. Ověřená čistá revize:
`ffba2bf09ef3980638f9327b25240d81414034ab`. Používá se pro čtení ABI a ISA,
ne jako spuštěný či linkovaný backend. Reference zahrnují `PPUInterpreter.cpp`,
`SPUThread.cpp`, `SPUOpcodes.h`, `lv2/sys_spu.cpp`, `lv2/sys_rsx.cpp`,
`Modules/cellPad.h`, `Modules/cellGcmSys.cpp` a definice RSX.

Primární podklady:
[RPCS3 v ověřené revizi](https://github.com/RPCS3/rpcs3/tree/ffba2bf09ef3980638f9327b25240d81414034ab),
[SPU ISA v1.2 IBM/Sony/Toshiba](https://arcb.csc.ncsu.edu/~mueller/cluster/ps3/SDK3.0/docs/arch/SPU_ISA_v1.2_27Jan2007_pub.pdf)
a [PPU assembler IBM](https://www.ibm.com/docs/ssw_aix_71/assembler/assembler_pdf.pdf).
