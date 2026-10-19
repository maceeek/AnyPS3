# PRX import references (atribut 0x2000) a stop `0x1b8`

Datum: 2026-10-14, Claude Opus 5.5. Cíl zůstává původní menu ACIT a navigace gamepadem.

## Ověřená příčina stopu sestavení 198

- **Ověřené (staticky, původní obraz):** `libsysutil_np_trophy` (analysis ELF SHA-256 `8fc9fd14…b0b38d`) má v datovém segmentu 1 na offsetech `0x470`/`0x474` hodnoty `0x168`/`0x1b8` bez záznamu v PT_SCE_PPURELA (422 relokací, sousední sloty `0x46c`, `0x478` relokace mají). OPD modulu leží v seg1 od `0xac` po 8 bajtech; `0x168`/`0x1b8` nejsou adresy OPD.
- **Ověřené (disassembly):** helper `0x20221a24` ukládá TOC slot `r2-0x7eec` do paketu `+0x6c` a uživatelská data do `+0x70`; libsysutil `0x201104d0` (export cellSysutil `0x6ba1d72f`) při růstu bufferu volá `+0x6c` jako OPD. Init paketu `0x20110808` (`0xcfad36de`) nuluje buffer `+0x3c` i kapacitu `+0x40`, takže první přidání parametru callback volá vždy — není to nedosažitelná větev.
- **Ověřené (struktura importů):** import `cellSysutil` trophy má atribut `0x2009`. Za 29 NID následuje druhé pole stejné délky s ukazateli na seznamy `(type, address, addend)` ukončené `type=0`. Pro NID `0xb59872ef` je seznam `[(1, 0x20230474, 0)]`, pro `0x7fc8f72c` `[(1, 0x20230470, 0)]`. Totéž mají `libsysutil_userinfo`, `libnetctl`, `libsysutil_np` (celkem 12 cílů, vše typ 1). Hlavní ELF hry atribut `0x2000` nemá.
- **Reference (RPCS3, `local/reference/rpcs3/rpcs3/Emu/Cell/PPUModule.cpp:607-667` a `:1034-1046`):** `ppu_patch_refs` zapisuje do ref cílů adresu linkovaného exportu (`faddr + addend`), podmínkou je `lib.attributes & 0x2000` a ukazatel `nids[i + num_func]`. Typy 4/6/57 RPCS3 také zná, v našich modulech se nevyskytují.
- **Vyvráceno:** chybný TOC registr (diagnostika 198), chyba PRX relokátoru (slot nemá relokaci záměrně) a nutnost mapovat nulovou stránku.

## Změna

- `anyps3/prx.py`: `linkage()` u importů s `0x2000` čte ref seznamy (`import_references`); podporován jen ověřený typ 1, jiný typ, nezarovnaná adresa či neukončený seznam explicitně selžou. `link_references()` zapíše do cílů adresu OPD exportu `+ addend`; nenalinkovaný import selže.
- `anyps3/build_game.py`: po relokaci všech modulů linkuje ref cíle podle `exported_bindings`, kód exportu přidá mezi statické vstupy a zápisy eviduje v `build.json` → `firmware_modules[].import_references`.
- **Odvozené omezení:** zápis probíhá staticky při sestavení. Odpovídá loaderu, protože data modulů jsou předem v kombinovaném obrazu a slot čte jen kód modulu po jeho načtení; volající je sám libsysutil, takže exportér je načtený. Pořadí lazy linkování lv2 není emulováno.
- Test: `tests/test_prx.py::test_import_references_receive_linked_export_descriptor` (literální očekávání `0x20130bb4`/`+8`, chyby pro nenalinkovaný import a typ 4, bez atributu se ref nečtou). Log `local/ratchet/tests-prx-199.log`.

## Runtime

Sestavení `local/ratchet/native-bundle-199`: trophy slot `0x20230474` → OPD `0x2013093c` (kód `0x2011446c`, dispatcher s 12položkovou jump table, všechny cíle přeloženy), `0x20230470` → `0x20130954` (kód `0x2011439c`). Výsledek běhu viz [CURRENT](../CURRENT.md).

## Pokračování 2026-10-14 (sestavení 199–204)

- **199:** stop `0x1b8` odstraněn; trophy posílá paket `NPTR` příkaz `f`.
- **200–203 (ověřené runtime):** nativní VSH strana trophy služby v `anyps3/sysutil_transport.py`: `f` CreateContext (ID 1–4, kontrola podpisu `0xB9DDE13B`, TROPDIR), `8` CreateHandle (leaf `mem_context`, ID 1–4), `o` GetRequiredDiskSpace (TRP v1: velikost − 0x40 − n·0x40 = 2 997 008 B), `1` DestroyContext, `9` DestroyHandle; dále `h` (status 1/3) a `i` (instalace TRP do `hdd0/home/00000001/trophy/<conf>`, VSH události typu 299 přes sysutil frontu, odpověď se struct uzlem `platinumId`) — `h`/`i` zatím jen nativní test, hra je v tomto běhu nevolala. Formát stromu a klíče odvozeny z disassembly libsysutil_np_trophy/libsysutil (viz CURRENT). Testy `tests/test_trophy.py`.
- **204:** `sys_timer_usleep` nyní během spánku spouští běžící SPU (3200 instrukcí/µs); dřív hlavní vlákno vyčerpalo 5 s diagnostický rozpočet ve smyčce `0x4e4168` čekající na SPU úlohu. SPU `0x100` doběhlo (110 M instrukcí). Test `tests/test_spu.py::test_ppu_sleep_grants_running_spu_the_slept_interval`.
- **Stop 204:** virtuální volání `0x669b28` přes vtable `0xc3c428` slot 2 → OPD `0xce3178` → `0x3304b4` (TOC `0xd34a88`) nebylo staticky přeloženo. Vtable (10 OPD před prvním null) přidána do profilu po ověření v původním ELF; **nové sestavení 205 zatím neproběhlo**.

## Pokračování (205–215)

- 205–207: NPTR `term` (sceNpTrophyTerm) a `sys_lwmutex_destroy` (RPCS3 `sys_lwmutex_.cpp`, owner `0xfffffffe`).
- 208: statusCb hry OPD `0xcd3b38` → `0x29db84` v profilu; trophy instalace na HDD proběhla (55 souborů, 37 ikon).
- 209–210: `fsqrt` (přesné celočíselné), single-precision aritmetika z plných double operandů s jedním zaokrouhlením
  (Book I: nedefinováno — **hypotéza** chování implementace), `stfs` = Power ISA SINGLE (QEMU `helper_tosingle`).
- 211: handlery sysutil událostí 299/310 v `firmware_static_seeds`.
- 212–215: vtable `0xc3c378`, statický rozpočet překladu 1 000 000, ověřené jump tables, `stwbrx/lwbrx/sthbrx/lhbrx`.
- 229–231: automatická ověřená detekce GCC switch tabulek `anyps3/jump_tables.py` (základ dokázán daty TOC → pointer →
  adresa za `bctr`), evidence v `build.json` → `verified_jump_tables`. Test `tests/test_jump_tables.py`.
