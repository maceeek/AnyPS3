# Úplné pokrytí původních OPD, obecnější switch detektor a VMX/FP (247–250)

Datum 2026-10-14, Claude Opus 5.5. Cíl: původní menu ACIT a navigace gamepadem —
**zatím nedosaženo**. Tento záznam řeší systémovou příčinu opakovaných stopů
`target has no statically translated block` (jeden ~25min běh na jeden cíl).

## 247: nezávislý běh připraveného profilu 246

`native-virtual-247` = obal `virtual_pad.py build` nad nezměněným
`native-bundle-246`; `program.base.cpp`, ELF a profil byteově shodné s 246,
binárka dokonce hash `4b88dfb3…` jako 246. Neutrální pad.

```bash
PYTHONPATH=. python local/ratchet/tools/virtual_pad.py build local/ratchet/native-bundle-246 local/ratchet/native-virtual-247
ANYPS3_VIRTUAL_PAD_CONTROL=$PWD/local/ratchet/native-virtual-247/control.txt ANYPS3_FRAME_STRIDE=600 PYTHONPATH=. python local/ratchet/run_native.py native-virtual-247 5000000000 > local/ratchet/run-virtual-247.log 2>&1
```

**Ověřené:** původní case 13 `0x69dd30` z 246 byl překročen. Nový stop
`pc=0x6a2544`, `lr=0x6a39a4`, CTR `0x6a2544`, 308 706 302 PPU kroků,
`target has no statically translated block`; returncode 1. 444 čtení /
17 897 130 bajtů, SPU 0x100 878 746 803 instrukcí, 15 274 flipů, 30 540 drawů,
33 snímků, host prezentace 0. `verification.json`: oba PT_LOAD exact_match.
Poslední snímek `native-virtual-247/frame-000033.png` stále načítací planeta.
Cíl `0x6a2544` je slot další byte-indexed switch tabulky s dispatchem `0x6a1aa0`
(14 položek); nový detektor (níže) ji v 249/250 nachází automaticky.

## Původní `.opd` jako zdroj statických cílů (ověřené)

`readelf -S local/ratchet/EBOOT.analysis.elf` (SHA256 `6debdca0…`): sekce [22]
`0xca6d38`, size `0x85d50`, offset `0xc96d38`, WA, align 8. Všech 68 522
osmibajtových deskriptorů má TOC `0xd34a88` (= TOC vstupního OPD) a spustitelný
zarovnaný kód; 34 512 unikátních kódových adres. Dřívější ručně ověřené OPD
(`0xd11570`, `0xd0efb0`, …) leží v této sekci.

Změna: `anyps3/build_game.py:opd_section_seeds` — profil `static_opd_section`
(address, size, toc, sha256 `8fde4865…`) se při každém buildu znovu ověří
(hash sekce, TOC každé položky = TOC procesu, zarovnaný spustitelný kód).
Seeds jen přidávají překladové cíle; skok vždy určuje původní guest kód.
Strop přeložených instrukcí 1 M → 4 M (`program.py`, `build_game.py`).
Testy: `tests/test_opd_seeds.py` (unikátní/sorted, cizí TOC, nezarovnání,
nespustitelné, za koncem kódu, identita, TOC procesu, velikost, zarovnání).

Výsledek: 294 384 → 2 735 839 přeložených instrukcí (250), C++ ~424 MB.

## Obecnější detektor switch tabulek (`anyps3/jump_tables.py`)

Na všech 363 `bctr` původního textu: dříve 159 tabulek, nyní 337, **žádná
dřívější tabulka ztracena ani změněna** (porovnání starého a nového detektoru).
Ručně ověřená tabulka 246 `0x69db38` je nyní detekována se stejnými 14 sloty.
Rozšíření, všechna s důkazem z dat (`word(base+d) == bctr+4`):

- škálování `rlwinm rT,rI,2,MB,29`, maska musí pokrýt indexy 0..K;
- přesné def-use intervaly řetězce místo zákazu zápisu kdekoli;
- whitelist s explicitními zápisy GPR **i CR polí** (oprava: `ldu` zapisuje RA);
- index přes `mr` nebo hodnotu zachovávající `extsw`/`extsh` (K<0x8000)/`extsb`
  (K<0x80)/`clrlwi`/`clrldi`; mez na `clrlwi rK,rI` když SCALE čte jen ponechané bity;
- báze přímo z TOC (`lwz rB,d(r2)`); prologový load před epilogovým `blr`;
- tabulky do 4096 položek a cíle do 1 MiB (původní tabulka `0x3dfa58` má 2239
  položek, offsety do `0x3987c`, všechny zarovnané a spustitelné).

Tabulky pouze přidávají překladové cíle; runtime `bcctr` skáče na skutečné CTR.
Zbývá 26 `bctr`: 20 víceblokových (mez v jiném bloku), 1 `-O0` kód přes zásobník,
1 memset computed jump bez tabulky, ostatní; ty dál explicitně zastaví.
Testy: `tests/test_jump_tables.py::GeneralizedJumpTableTests` z původních slov
`0x1e6e4`, `0x19d48`, `0xb3714` (enkodéry ověřeny proti bajtům ELF), odmítnutí
přepisu CR pole, úzké masky, extsb nad 0x7f, nesprávné báze a nezávislého `clrlwi`.

## Nové PPU instrukce (nalezené v nově přeložených trapech)

| Instrukce | Výskyt | Sémantika / zdroj | Test |
| --- | ---: | --- | --- |
| lfsx/lfsux/lfdx/lfdux/stfsx/stfsux/stfdx/stfdux | 134+75+… | jako D-form | `test_indexed_fp_loads_and_stores` |
| fsqrts | 41 | `fp_sqrt` s jediným zaokrouhlením na binary32 | `test_fsqrts_rounds_once_to_single` (racionální orákulum) |
| fres, frsqrte | 10, 22 | odhad: správně zaokrouhlený výsledek (kompat. volba jako vrefp), XX nezměněn, FR/FI nedefinované | `test_fres_and_frsqrte_estimates` |
| mtfsf | 1 | pole dle FLM, FEX/VX přepočítané souhrny | `test_mtfsf_fields_and_summaries` |
| dcbst | 1 | bez funkčního efektu (koherentní host paměť) | — |
| vmaxfp/vminfp | 11/33 | NaN vA→vB quiet, max(+0,−0)=+0 (RPCS3 `PPUInterpreter.cpp:599-615`, `simd.hpp:1436`) | `test_float_maximum_and_minimum` |
| vmrg*, vupk[hl]s[bh], vcmpequ[bh][.], vmax/vmin[su][bhw], vrl[bhw], vpk[us][hw][us]s, lvsl/lvsr | 1–5 | opkódy dle RPCS3 `PPUOpcodes.h:212-340,413,430` | `test_integer_element_families_and_shift_vectors` |
| vrsqrtefp | 2 | odhad (kompat. volba) | `test_reciprocal_square_root_estimate` |

Zbylé trapy 250 (333): úmyslné `tw 31` (109×) a `twi` na neplatných místech
(159×), neimplementované importy (63, hlavně sysPrxForUser/cellAudio), několik
VMX. Všechny při dosažení explicitně zastaví.

Celá sada: `ANYPS3_REQUIRE_VULKAN=1 python -m unittest discover -s tests -q`
→ 191 testů OK (`local/ratchet/tests-250.log`).

## Sestavení 248–250

- **248**: jen OPD seeds (starý detektor), 2 598 962 instrukcí, 625 trapů.
  Kompilace bez LTO ~9 min / ~21 GB RSS, záměrně zastavena (zastaralé);
  `NOT_RUN.txt`.
- **249**: + nový detektor a FP indexed. `virtual_pad.py build --lto` (nová
  volba, příznaky v manifestu): GCC 16 ICE SIGSEGV v `get_loop_body` (pass ch)
  na `page_73` během LTRANS po 6:35; `NOT_RUN.txt`.
- **250**: + všechny instrukce výše, 2 735 839 instrukcí, 333 trapů, 336 tabulek
  (vč. `0x6a1aa0` → `0x6a2544`). Obal kompilován s `ulimit -s unlimited` a LTO.

**Hypotéza:** ICE 249 je vyčerpání zásobníku GCC při rekurzi přes obří funkci;
rozliší ji úspěšná kompilace 250 s neomezeným zásobníkem.

## 250: skutečný běh (ověřené)

`native-virtual-250` (LTO obal, `ulimit -s unlimited` — kompilace 6:30 bez ICE, hypotéza
o zásobníku GCC potvrzena). Hra prošla místem stopu 247 i všemi dřívějšími:
**478 661 064 PPU kroků** (dříve 308 M), 29 798 flipů, 59 588 drawů, 57 snímků, HDD cache
zápisy (`/dev_hdd1/cache.dat`), načítání `front_end/level_cached.psarc`; poslední snímek
stále načítací planeta. Oba PT_LOAD exact_match. Stop:
`pc=0x2018df64` (syscall 802 cellFsRead v libfs), `lr=0x710770`:
`unmapped or split guest memory access address=0x42effd2a size=11698 write=1` — buffer
přes dvě sousední, samostatně mapované oblasti (hranice `0x42f00000`). Nejde o chybějící blok.

## 251/252: opravy po 250

- `anyps3/native_fs.py`: cellFsRead/Write přes `GuestMemory::transfer` (validace celého rozsahu
  napříč oblastmi); test `tests/test_native_fs.py` (čtení/zápis přes hranici, nemapovaná druhá půlka → stop).
- `anyps3/services.py` `guest_copy` (memcpy/memset) a nové `anyps3/guest_libc.py` (_sys_memcmp, memmove,
  strchr, strrchr, strncpy, strncasecmp dle RPCS3 `sys_libc_.cpp:99-370`) rovněž přes `transfer`;
  test `tests/test_guest_libc.py`.
- **cellAudio** (7 NID) implementoval codex subagent (GPT-6.1 Sol high) dle zadání
  `local/ratchet/codex/task-audio.md`; výsledek a revize: `local/ratchet/codex/audio-result.md`,
  `anyps3/native_audio.py`, `tests/test_native_audio.py` (9 testů). Sink jen výslovně
  `ANYPS3_AUDIO_SINK=sdl|null`; běhy 252+ používají `null` = diagnostický tichý sink (není důkaz audia).
- Profil: +23 switch tabulek / +309 seeds z rešerše subagenta `local/ratchet/codex/switches-250.md`
  (22 verified, 3 unclear = jen překladové cíle; sloty nezávisle přepočteny `add_jump_table.py`;
  `0x6eec40` a `0x7102f0` už v profilu byly a shodují se). Log `local/ratchet/profile-jump-tables-251.log`.
- Výkon (252): `GuestMemory::mark_write` dříve inkrementoval `std::map` verzi pro každou zapsanou
  128B řádku; nyní verze jen pro řádky pozorované rezervací (tokeny se porovnávají jen mezi
  rezervací a podmíněným zápisem). `access` má cache posledního regionu validovanou proti aktuálnímu
  vektoru. Test `tests/test_memory.py::RegionCacheAndReservationTests`.
- Celá sada nad zdroji 251: 202 testů OK (`local/ratchet/tests-251.log`); po výkonové změně cílené
  sady memory/runtime_objects/spu/lightweight_sync/program 81 OK + nový test OK.
- 251 záměrně nespuštěna (`NOT_RUN.txt`), nahrazena 252.

Další dvě rešerše subagentů: `docs/knowledge/experiments/2026-10-14-import-research-250.md`
(všech 63 neimplementovaných importů 250, ABI, priority) a `local/ratchet/codex/switches-250.md`.

## 252–255 (ověřené běhy a změny)

- **252** (split-safe paměť, libc, cellAudio null sink, výkon): ~2–5× rychlejší (203 čtení za 38 s;
  stop 250 překonán za ~16 min místo ~38). Stop `pc=0x637098`, 541 422 867 kroků:
  `FP infinite arithmetic is unsupported` — původní normalizace nulového vektoru
  (`fsqrt 0`, `fdiv 1/0=+∞` se ZX, pak `fmuls 0×∞`). 35 434 flipů, 67 snímků, PT_LOAD exact.
  Oprava: `fp_multiply`/`fp_fused` s nekonečny (∞×0 → výchozí QNaN + VXIMZ + VX, ∞−∞ → VXISI),
  test `test_multiply_and_fused_with_infinities` vč. přesné původní instrukce a FPSCR `0x86008000→0xa6111000`.
- **253**: FP prošlo (FPSCR `0xa6124000` nese VXIMZ); stop 541 456 384 kroků
  `SPU target has no static translation`: SPU `0x1000200` na LS `0x10500` — herní SPU kernel
  (vlastní SPU ELF hry `0xbf4480`, LS `0x3000`) nahrál DMA job **CollNarrow** `0xb8b180` (0x10be0 B) na LS `0x100`;
  dříve i **CollBroad** `0xb9bd80` (0xc680 B). Jména z původní konstantní tabulky `0xca147c` →
  řetězce `0xac2638`/`0xac2648`.
- **254**: + `fcmpu/fcmpo` s NaN/∞ (FU, VXSNAN, VXVC) a saturující `fctiw[z]/fctid[z]` (VXCVI);
  zkompilováno, nespuštěno (`NOT_RUN.txt`), nahrazeno 255. Test `test_compare_and_integer_conversion_specials`.
- **Importy (codex, `local/ratchet/codex/imports2-result.md`)**: `sys_lwmutex_trylock`, atexit registr,
  `_sys_malloc/_sys_free`, vlastnictví SPU image, hranice module unload; heap ponechán trap (nedoložené argumenty).
  225 testů OK.
- **Okno (codex, `local/ratchet/codex/present-result.md`)**: `ANYPS3_PRESENT=sdl` prezentuje skutečný guest scanout
  do SDL2 okna (throttle 60 Hz, `presented_frames` skutečné, zavření okna = explicitní konec).
- **Profil 255**: 130 SPU kódových bloků z původní oblasti `0xb00000–0xbf4480` (první instrukce `ila`,
  nulový padding, trailer `ffffffff…`) jako overlaye na LS `0x100` + 2 vložené SPU ELF úlohy
  (`0xbfdc00`, `0xc0c300`, LS `0x4000`); každý s SHA-256 původních bajtů. LS `0x100` je pozorovaná báze jobů;
  ostatní báze jsou **hypotéza**, za běhu se ověřuje každé slovo (jinak explicitní stop).
  Log `local/ratchet/profile-spu-jobs-255.log`, záloha `local/ratchet/profile-before-spu-jobs-255.json`.

## Ukončení (2026-10-14 ~20:30 UTC)

255: LTO kompilace obalu přerušena po 15,5 min na pokyn uživatele ukončit práci; jedna LTRANS
partition běžela >7 min sama (hypotéza: obří `spu_image_0_page_*` z 130 overlayů na LS `0x100`).
Nespuštěno. Hlavní doporučení: rozdělit generovaný překlad do více TU a SPU stránky na menší funkce.
