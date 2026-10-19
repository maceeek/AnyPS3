# Importy sestavy 250: ABI, sémantika a pořadí implementace

Datum: 2026-10-14. Rozsah: dokumentační rešerše; žádná runtime/profil/test změna, žádný build ani herní běh.
Výstupy pouze tento soubor a `local/ratchet/codex/imports-250.json`. CURRENT/HANDOFF/INDEX a historie nejsou měněny kvůli výslovnému vlastnictví cest v zadání.
Původní menu a navigace gamepadem nejsou tímto úkolem ověřeny.

## Rozsah a původ důkazů

**Ověřené staticky:** všech 63 řádků `local/ratchet/codex/imports250.txt` přesně odpovídá importovým trapům v build.json 250: 41 různých NID, 15 herních a 48 firmware stubů.
40 NID má jméno ověřené přes REG_FUNC + přepočet NID. Jeden zůstává nepojmenovaný (`0x9fb6228e`).
NID se počítá SHA-1(name + suffix), první 4 digest bytes jako LE u32; zdroj `local/reference/rpcs3/rpcs3/Emu/Cell/PPUModule.cpp:58`.
Tento LE hash **nemění** big-endian guest data ABI. Všechny uvedené importy chybí v současném `anyps3/services.py:9` IMPORTS.

Reference ověřeny na HEAD:

- RPCS3 `ffba2bf09ef3980638f9327b25240d81414034ab`, kořen `local/reference/rpcs3`.
- PSL1GHT `f649a08fd536a9e27c08c7db2d93a2d7ee4c3bbe`, kořen `local/reference/PSL1GHT`.

V tabulkách **M/** znamená `local/reference/rpcs3/rpcs3/Emu/Cell/Modules/`, **L/** znamená `local/reference/rpcs3/rpcs3/Emu/Cell/lv2/`; skutečné úplné file:line jsou i v JSON.
`verified`/ověřené znamená konkrétní zdrojový nebo statický fakt; **neznamená hardware test nebo úplnou shodu firmware 3.01**.
`derived`/odvozené označuje rekonstrukci ABI nebo neúplný kontrakt; `hypothesis`/hypotéza nedoložené jméno/ABI/chování.
Priority jsou vždy **hypotéza o budoucí dosažitelnosti menu**, nikoli runtime trace: 1 běžný init/datová cesta, 2 pomocná služba/cleanup, 3 exit/debug/neznámý protokol. Status JSON je konzervativní pro celý kontrakt; jméno může být ověřené i při hypothesis ABI.

**Aktuální omezení převzetí:** Git workspace není dostupný (`git status` hlásí not a git repository), nic nebylo inicializováno. `ps` v dostupném PID namespace ukázal pouze vlastní krátké nástroje; nemůže potvrdit nepřítomnost cizích host procesů. Není převzat žádný živý tool/session handle.
250 při čtení nemá verification.json; nelze jej prohlásit za ověřený runtime. Čtené 247 verification má rc=1 a stop na nepřeloženém bloku, menu/nav=false; jde o starší důkaz, ne aktuální živý proces. Znalostní stránky ještě popisují předání 246 a worktree drift; runtime práce se zde neobnovuje.

## Identita vstupů (SHA-256)

- `local/ratchet/EBOOT.analysis.elf`: `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`.
- `local/ratchet/native-bundle-250/build.json`: `cb17e737cb084674bd4a5e52a327c35cf81057eb775e786e12eaf55e57ab5eb2`.
- Snapshot `local/ratchet/native-bundle-250/profile.json`: `1295ea542d9f930ae6279d0f9105611cc563808824ae9ab4f59edbac08e07bef`; aktuální obsah snapshotu ověřen.
- `anyps3/services.py`: `00ac45a738cf83e9c2833056ce8720b1d45e62e41726f9f54e11216b9b6a4dad`; shoduje se s build source_sha256.
- `local/ratchet/firmware-verified/extraction.json`: version **3.01**, HMAC verified, ECDSA **neověřeno**.
- `local/ratchet/liblv2-relocated.elf`: `843006f84aa0f195ae85119e891238f896ae8f9faf339904709a91bd3be9bdfc`; hash proti `liblv2-relocated.json` ověřen.
- `local/ratchet/libsre-relocated.elf`: `9cdcc1242d8703c9f3f72f8dd6ce986837593a73f7ca78ec128f9fbde22434fa`; hash proti `libsre-relocated.json` ověřen.

Nezaměňovat diagnostické relokace liblv2 `0x28000000` / libsre `0x29000000` s relokacemi firmware modulu libsre sestavy 250 (`0x202...`). OPD/kódové adresy níže označují konkrétní obraz; profil se nemění.

## Společné ABI a chyby

**Odvozené z PPU binding a původních callerů:** pevné integer/pointer argumenty v pořadí r3..r10; guest pointer je 32bitová EA i při 64bit GPR. `ppu_thread&` v referenci je host context, **není** další guest argument. Integer/error výsledek v r3, s32 chybu zachovat jako signed word; `void` nedává oprávnění vymýšlet návratový error. Callback je guest OPD (kód + TOC), ne host address.
PS3 guest u16/u32/u64 a float bit patterny ukládat BE. Byte strings/PCM pořadí a padding mají vlastní kontrakt. U `const char*` validovat guest mapping, nečíst přes host libc pointer cast.

Číselné CELL chyby ověřené v `local/reference/rpcs3/rpcs3/Emu/Cell/ErrorCodes.h:103`: EINVAL `0x80010002`, ENOMEM `0x80010004`, ESRCH `0x80010005`, EDEADLK `0x80010008`, EPERM `0x80010009`, EBUSY `0x8001000a`, ESTAT `0x8001000f`, EKRESOURCE `0x80010011`. Nejsou univerzální náhradou za chyby každé služby; níže uváděny jen relevantní větve. Invalid pointer u libc není automaticky CELL_EINVAL.

Číselné Sysmodule chyby: `M/cellSysmodule.h:4` DUPLICATED `0x80012001`, UNKNOWN `0x80012002`, UNLOADED `0x80012003`, INVALID_MEMCONTAINER `0x80012004`, FATAL `0x800120ff`. **Ověřené v referenci:** unload helper `M/cellSysmodule.cpp:461` na count=0 vrací 0; samotný enum UNLOADED proto není důkaz chyby této konkrétní větve. Load helper incrementuje refcount, na prvním load provede module_start a rollback při jeho chybě (`M/cellSysmodule.cpp:408`); default modules chrání SDK-dependent větev `M/cellSysmodule.cpp:897`. Přesný FW 3.01 policy ještě ověřit v původním libsysmodule obrazu.

## Katalog všech 41 NID

Každá položka zahrnuje prototyp s register mapping, zdroj definice + registrace, prioritu, sémantiku a meze chybového kontraktu. Duplicitní stuby mají samostatné objekty v JSON.

### cellAudioInit — 0x0b168f92 (cellAudio, P1, verified)

`s32 cellAudioInit(void); -> r3=s32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2521`; definice `M/cellAudio.cpp:1520`. Založit audio stav a guest indexovou sdílenou paměť; duplicitní init ALREADY_INIT, selhání paměti/transportu musí být skutečná chyba. SDK-dependent 4/8 ports a detaily lifecycle níže.

### _sys_strrchr — 0x191f0c4a (sysPrxForUser, P1, verified)

`const char *_sys_strrchr(const char *s,char c); r3=s,r4=low8(c) -> r3=pointer32 or 0`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:511`; definice `M/sys_libc_.cpp:353`. Poslední výskyt, včetně terminátoru pro c=0; nenalezeno NULL. Byte semantics bez allocations; firmware behavior invalid pointer není ověřeno.

### _sys_strncasecmp — 0x1ca525a2 (sysPrxForUser, P1, verified)

`s32 _sys_strncasecmp(const char *a,const char *b,u32 n); r3=a,r4=b,r5=n -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:510`; definice `M/sys_libc_.cpp:336`. Porovnat nejvýše n znaků s ASCII lowercase, skončit po NUL; n=0 bez čtení, reference -1/0/+1. Nepřenášet host locale. Reference _sys_tolower:98 omezuje input -1..127; high-bit bajty/FW locale dosud neověřeny.

### _sys_memmove — 0x27427742 (sysPrxForUser, P1, verified)

`void *_sys_memmove(void *dst,const void *src,u32 n); r3=dst,r4=src,r5=n -> r3=dst32`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:500`; definice `M/sys_libc_.cpp:180`. Kopírovat s podporou obou směrů překryvu; n=0 bez access. Validovat oba guest rozsahy před zápisem; neimplementovat jako memcpy. Bez samostatného guest error code.

### _sys_heap_malloc — 0x35168520 (sysPrxForUser, P1, derived)

`u32 _sys_heap_malloc(u32 heap,u32 size); r3=heap,r4=size -> r3=pointer32 (0 on allocation failure, derived)`

**Ověřené jméno/registrace:** `M/sys_heap.cpp:89`; definice `M/sys_heap.cpp:38`. Přidělit nealiasující guest paměť a evidovat ji v daném heapu. RPCS3 heap ID ignoruje a volá vm::alloc; správný handle validation, alignment a size=0 kontrakt FW 3.01 neověřeny; nejde o úspěšný návrat nulového error code.

### sys_ppu_thread_register_atexit — 0x3dd4a957 (sysPrxForUser, P1, verified)

`s32 sys_ppu_thread_register_atexit(void (*guest_opd)(void)); r3=OPD32 -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_ppu_thread_.cpp:284`; definice `M/sys_ppu_thread_.cpp:192`. Zapsat callback do sdílené osmipoložkové tabulky chráněné lwmutex; duplicate EPERM, full ENOMEM. sys_ppu_thread_exit:168 je skutečně vykonává před TLS free; nejde o procesní atexit ani nutně thread-local seznam.

### _sys_strchr — 0x7498887b (sysPrxForUser, P1, verified)

`const char *_sys_strchr(const char *s, char c); r3=s,r4=low8(c) -> r3=guest pointer32 or 0`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:506`; definice `M/sys_libc_.cpp:261`. První výskyt včetně terminátoru při c=0; při nenalezení NULL. Přístup ke guest bajtům, žádné nové allocation; neplést s vracením indexu. Reference nedefinuje error code pro invalid pointer.

### cellAudioGetPortConfig — 0x74a66af0 (cellAudio, P1, verified)

`s32 cellAudioGetPortConfig(u32 port, CellAudioPortConfig *out); r3=port,r4=out -> r3=s32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2524`; definice `M/cellAudio.cpp:1778`. Vyplnit všech 32 B guest struktury, zejména platnou adresu be64 readIndex a PCM portAddr; NOT_INIT / PARAM. Pro zavřený port reference vrací status CLOSE a zachované buffer fields, nikoli PORT_NOT_OPEN. Layout níže.

### cellAudioPortStart — 0x89be28f2 (cellAudio, P1, verified)

`s32 cellAudioPortStart(u32 port); r3=port -> r3=s32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2525`; definice `M/cellAudio.cpp:1823`. READY -> RUN, nulovat ring, spustit periodickou spotřebu a readIndex. NOT_INIT / PARAM / PORT_NOT_OPEN / PORT_ALREADY_RUN.

### _sys_heap_free — 0x8a561d92 (sysPrxForUser, P1, derived)

`s32 _sys_heap_free(u32 heap,u32 address); r3=heap,r4=address -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_heap.cpp:91`; definice `M/sys_heap.cpp:52`. Uvolnit allocation v příslušném heapu, zachovat ostatní paměť. RPCS3 heap ignoruje a vrací 0 i bez ověření vm::dealloc; chybové kódy invalid/double/free foreign allocation FW 3.01 neověřeny.

### sys_lwmutex_trylock — 0xaeb78725 (sysPrxForUser, P1, verified)

`s32 sys_lwmutex_trylock(sys_lwmutex32 *m); r3=m -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_lwmutex_.cpp:407`; definice `M/sys_lwmutex_.cpp:273`. Atomicky získat vlastnictví aktuálním guest TID; nezablokovat při obsazení (EBUSY). Rekurze podle attr; EDEADLK pro nerekurzivní vlastníka, EKRESOURCE při overflow recursive_count, EINVAL při dead markeru. reserved owner vyžaduje kernel handoff. Sdílet stav s existující lock/unlock implementací.

### _sys_heap_create_heap — 0xb2fcf2c8 (sysPrxForUser, P1, derived)

`u32 _sys_heap_create_heap(const char *name,u32 arg2,u32 arg3,u32 arg4); r3=name,r4=arg2,r5=arg3,r6=arg4 -> r3=heap handle32`

**Ověřené jméno/registrace:** `M/sys_heap.cpp:87`; definice `M/sys_heap.cpp:22`. RPCS3 vytváří HeapInfo pouze s name; význam arg2..4 ignoruje. Vytvořit reálný účetní/allocator stav až po ověření původních FW argumentů; rozsah, kapacita, flags a error ABI neověřeny. Název a 4-register signature jsou ověřené v referenci.

### _sys_malloc — 0xbdb18f83 (sysPrxForUser, P1, verified)

`void *_sys_malloc(u32 size); r3=size -> r3=guest pointer32 or 0`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:513`; definice `M/sys_libc_.cpp:371`. Přidělit skutečnou guest allocation, nesmí přepsat jiné objekty; NULL při nedostatku (allocator), ne error_code. Reference VM alignment není důkaz FW granularity; size=0 a limitové detaily dosud neověřeny.

### cellAudioPortOpen — 0xcd7bc431 (cellAudio, P1, verified)

`s32 cellAudioPortOpen(const CellAudioPortParam *param,u32 *outPort); r3=param,r4=outPort -> r3=s32; *outPort=be32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2527`; definice `M/cellAudio.cpp:1652`. Validovat parametry, rezervovat port, přidělit skutečnou guest PCM paměť, init stav READY a index; NOT_INIT / PARAM / PORT_FULL / SHAREDMEMORY. Oba původní callsite parametry i layout níže.

### _sys_strncpy — 0xd3039d4d (sysPrxForUser, P1, verified)

`char *_sys_strncpy(char *dst,const char *src,s32 len); r3=dst,r4=src,r5=len -> r3=dst32 or 0`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:509`; definice `M/sys_libc_.cpp:311`. Kopírovat nejvýše len bajtů; po NUL doplnit zbytek nulami, při přesném/truncated limitu nepřidat terminátor. RPCS3 null src/dst ->NULL, len<=0 bez zápisu. Overlap není memmove, invalid mapped ranges musí být explicitní chyba runtime.

### _sys_free — 0xf7f7fb20 (sysPrxForUser, P1, verified)

`void _sys_free(void *address); r3=address; return void`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:515`; definice `M/sys_libc_.cpp:393`. Uvolnit odpovídající guest allocation, nepřijímat host pointer. Reference deleguje vm::dealloc bez error návratu; NULL/double/foreign free policy FW 3.01 nutno ověřit, nepřidávat vymyšlené guest errno.

### _sys_memcmp — 0xfb5db080 (sysPrxForUser, P1, verified)

`s32 _sys_memcmp(const u8 *a,const u8 *b,u32 n); r3=a,r4=b,r5=n -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:498`; definice `M/sys_libc_.cpp:135`. Unsigned-byte lexikografické porovnání přesně n bajtů, ne řetězce; n=0 rovnost a žádné čtení. Reference vrací -1/0/+1, obecný C kontrakt pouze znaménko. Nezapisuje nic, žádné errno definované.

### _sys_vsnprintf — 0x0618936b (sysPrxForUser, P2, derived)

`s32 _sys_vsnprintf(char *dst,u32 capacity,const char *fmt,guest_va_list ap); r3=dst,r4=capacity,r5=fmt,r6=ap -> r3=s32 (derived ABI)`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:521`; definice `M/sys_libc_.cpp:465`. RPCS3 () je TODO. FW 3.01 export 0x28005030 zachovává dst/capacity, předává formatteru fmt/ap, ukládá limit capacity-1, a při capacity!=0 doplní NUL na dst+result. Přesný truncation/count==0 kontrakt a všechny formáty neověřeny. Guest va_list není host va_list; nezaměnit s variadic snprintf.

### _sys_snprintf — 0x06574237 (sysPrxForUser, P2, derived)

`s32 _sys_snprintf(char *dst,u32 capacity,const char *fmt,...); r3=dst,r4=capacity,r5=fmt,varargs r6..r10/stack -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:517`; definice `M/sys_libc_.cpp:417`. Skutečně formátovat a NUL-terminovat v mezích kapacity, žádné host dereference guest ukazatelů. RPCS3 vrací počet skutečně zapsaných znaků (capacity==0 ->0 s komentářem ???), ne C99 required length; FW 3.01 návratové detaily je třeba ověřit přes původní formatter. Truncation, %s/%p/šířky a guest varargs musí mít regrese.

### cellSysmoduleUnloadModule — 0x112a5ee9 (cellSysmodule, P2, verified)

`s32 cellSysmoduleUnloadModule(u16 id); r3=low16(id) -> r3=s32`

**Ověřené jméno/registrace:** `M/cellSysmodule.cpp:1384`; definice `M/cellSysmodule.cpp:873`. Snížit skutečný loaded_count; na posledním odkazu provést module_stop a unload, vyřadit exporty, respektovat dependencies a chráněné default moduly. Katalog UNKNOWN/UNLOADED/FATAL viz níže; reference má i SDK <3.30 větev.

### sys_prx_load_module — 0x26090058 (sysPrxForUser, P2, verified)

`s32 sys_prx_load_module(const char *path, u64 flags, const sys_prx_load_module_option32 *opt); r3=path,r4=flags,r5=opt -> r3=positive module ID or negative error`

**Ověřené jméno/registrace:** `M/sys_prx_.cpp:264`; definice `M/sys_prx_.cpp:36`. Načíst původní modul, relokovat/linkovat a vrátit skutečné kladné ID; module_start je samostatná operace. EINVAL pro nepovolené flags, ENOSYS pro nepodporovaný fixed address, ENOENT při chybějícím souboru, ILLEGAL_LIBRARY při neplatném PRX (lv2/sys_prx.cpp:184). Statický katalog musí ověřit identitu a rozsah, ne přijmout libovolnou cestu.

### __sys_look_ctype_table — 0x3ef17f8c (sysPrxForUser, P2, verified)

`s16 __sys_look_ctype_table(s32 ch); r3=ch -> r3=signed mask16`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:492`; definice `M/sys_libc_.cpp:90`. Vrací hodnotu masky, nikoli ukazatel na tabulku! Index ch+1 pro -1..127; EOF ->0, upper bit1/lower bit2/digit bit4/space bit8/punct bit0x10/control bit0x20/hex bit0x40 dle sys_libc_.cpp:72. Input mimo rozsah reference zastaví ensure; rozšířený FW charset neověřen.

### cellAudioPortClose — 0x4129fe2d (cellAudio, P2, verified)

`s32 cellAudioPortClose(u32 port); r3=port -> r3=s32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2522`; definice `M/cellAudio.cpp:1863`. Odpojit a uvolnit otevřený port (i běžící), zrušit jeho spotřebu, vrátit slot do poolu. NOT_INIT / PARAM / PORT_NOT_OPEN; úspěch teprve po provedení.

### sys_prx_register_library — 0x42b23552 (sysPrxForUser, P2, verified)

`s32 sys_prx_register_library(const prx_library32 *entry); r3=entry -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_prx_.cpp:273`; definice `M/sys_prx_.cpp:165`. Zaregistrovat knihovnu a propojit NID/exportní OPD s importy, ne pouze potvrdit přijetí. RPCS3 lv2/sys_prx.cpp:917 ověřuje adresu (EFAULT) a provádí loader linking. Formát záznamu viz níže; úplné firmware chyby LIBRARY_FOUND/ILLEGAL_LIBRARY jsou katalog, ne ověřené všechny větve této reference.

### cellAudioPortStop — 0x5b1e2c73 (cellAudio, P2, verified)

`s32 cellAudioPortStop(u32 port); r3=port -> r3=s32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2523`; definice `M/cellAudio.cpp:1900`. RUN -> READY a zastavit spotřebu. NOT_INIT / PARAM / PORT_NOT_RUN včetně zavřeného portu. Nezaměnit s close.

### _sys_spu_printf_detach_group — 0x5fdfb2fe (sysPrxForUser, P2, verified)

`s32 _sys_spu_printf_detach_group(u32 group); r3=group -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_spu_.cpp:498`; definice `M/sys_spu_.cpp:449`. Bez registrovaného detach callbacku vrátit ESTAT, jinak vykonat skutečný guest callback s group a vrátit jeho výsledek. Inicializace callbacků je sys_spu_.cpp:412; samotné detach nemá simulovat stdout.

### _sys_printf — 0x9f04f7af (sysPrxForUser, P2, derived)

`s32 _sys_printf(const char *fmt,...); r3=fmt,varargs r4..r10/stack -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:518`; definice `M/sys_libc_.cpp:437`. Formátovat skutečný guest obsah do TTY/logu. RPCS3 vrací CELL_OK po sys_tty_write, nenaplňuje obvyklý C printf byte-count kontrakt; pro FW 3.01 návrat i error-path ověřit zvlášť. Vypuštění výpisu s falešným success není implementace.

### sys_prx_load_module_on_memcontainer — 0xaa6d9bff (sysPrxForUser, P2, derived)

`s32 sys_prx_load_module_on_memcontainer(const char *path,u32 container,u64 flags,const sys_prx_load_module_option32 *opt); r3=path,r4=container,r5=flags,r6=opt -> r3=positive ID or negative error`

**Ověřené jméno/registrace:** `M/sys_prx_.cpp:266`; definice `M/sys_prx_.cpp:54`. Totéž se skutečným účtováním/vazbou na memory container. RPCS3 lv2/sys_prx.cpp:534 container ignoruje: nelze převzít jako ověřenou firmware sémantiku. Ověřené jsou název a pořadí argumentů, chyby containeru jsou otevřené.

### sys_ppu_thread_unregister_atexit — 0xac6fc404 (sysPrxForUser, P2, verified)

`s32 sys_ppu_thread_unregister_atexit(void (*guest_opd)(void)); r3=OPD32 -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_ppu_thread_.cpp:285`; definice `M/sys_ppu_thread_.cpp:218`. Vymazat matching callback v téže chráněné tabulce; ESRCH když není registrován; nevolat callback při unregister.

### _sys_heap_delete_heap — 0xaede4b03 (sysPrxForUser, P2, derived)

`s32 _sys_heap_delete_heap(u32 heap); r3=heap -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_heap.cpp:88`; definice `M/sys_heap.cpp:29`. RPCS3 jen odstraní HeapInfo ID bez validace. Požadovaná služba musí zrušit skutečný heap lifecycle/vlastnictví allocations; chybové kódy a outstanding allocation policy FW 3.01 nejsou z této reference zjistitelné.

### _sys_qsort — 0xc4fd6121 (sysPrxForUser, P2, verified)

`void _sys_qsort(void *base,u32 count,u32 size,s32 (*guest_opd)(const void *,const void *)); r3=base,r4=count,r5=size,r6=OPD32; return void`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:524`; definice `M/sys_libc_.cpp:477`. Uspořádat count prvků velikosti size pomocí skutečně vykonaného guest comparatoru s guest adresami v r3/r4 a správným OPD TOC; ne host function-pointer cast. Zabránit count*size overflow a out-of-range paměti; size/count==0 bezpečně bez comparatoru. Reference nenabízí errno.

### cellAudioQuit — 0xca5ac370 (cellAudio, P2, verified)

`s32 cellAudioQuit(void); -> r3=s32`

**Ověřené jméno/registrace:** `M/cellAudio.cpp:2526`; definice `M/cellAudio.cpp:1617`. Uzavřít otevřené porty, uvolnit audio sdílenou paměť a resetovat init; bez init NOT_INIT. RPCS3 ponechává event queues samostatně, automaticky je neničí.

### _sys_spu_printf_attach_group — 0xdd0c1e09 (sysPrxForUser, P2, verified)

`s32 _sys_spu_printf_attach_group(u32 group); r3=group -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_spu_.cpp:497`; definice `M/sys_spu_.cpp:437`. Bez attach callbacku ESTAT, jinak skutečně zavolat příslušný guest OPD callback s group. Vrátit jeho výsledek; neoznamovat připojení neexistující event služby.

### sys_spu_image_close — 0xe0da8efd (sysPrxForUser, P2, verified)

`s32 sys_spu_image_close(sys_spu_image32 *image); r3=image -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_spu_.cpp:490`; definice `M/sys_spu_.cpp:356`. USER type=0 uvolní segment descriptor allocation; KERNEL type=1 zavře kernel image ID přes syscall. Jiný type EINVAL; při běžícím SPU zachovat správné ownership LS/data. Layout 16 B níže; nevymazat náhodnou guest paměť.

### _sys_vprintf — 0xfa7f693d (sysPrxForUser, P2, derived)

`s32 _sys_vprintf(const char *fmt,guest_va_list ap); r3=fmt,r4=ap -> r3=s32 (derived ABI)`

**Ověřené jméno/registrace:** `M/sys_libc_.cpp:520`; definice `M/sys_libc_.cpp:459`. RPCS3 () je TODO; původní 3.01 export 0x28002b90 zachovává r3=fmt/r4=ap a předává formatteru. Formátovat guest arguments do skutečného TTY; přesný byte-count a error-path nejsou tímto krátkým disassembly dokázány.

### cellUserTraceRegister — 0x05893e7c (cellLibprof, P3, hypothesis)

`s32 cellUserTraceRegister(trace_descriptor32 *arg1,void *arg2,u32 arg3,u32 *outHandle); r3=arg1,r4=arg2,r5=arg3,r6=outHandle -> r3=s32 (derived caller ABI, names provisional)`

**Ověřené jméno/registrace:** `M/cellLibprof.cpp:33`; definice `M/cellLibprof.cpp:12`. Ověřené jméno/REG_FUNC, ale RPCS3 UNIMPLEMENTED_FUNC +0 není kontrakt. Původní libsre caller dává čtyři arguments a testuje návrat proti 0; částečné ABI/descriptor layout odvozené níže. Význam arg2/arg3, celý trace protokol a chyby neověřeny. Vyžaduje skutečný registr, ne success TODO.

### cellUserTraceUnregister — 0x6d045c2e (cellLibprof, P3, hypothesis)

`cellUserTraceUnregister(u32 handle); r3=handle (derived); return type/error ABI unknown (RPCS3 placeholder s32)`

**Ověřené jméno/registrace:** `M/cellLibprof.cpp:34`; definice `M/cellLibprof.cpp:18`. Ověřené jméno/REG_FUNC; původní libsre caller načítá u32 handle, -1 přeskočí a jinak volá unregister. Návrat ignoruje, takže jeho typ ani error ABI nejsou doloženy. Hypotéza odstranění matching trace record se musí ověřit z FW; nepřevzít prázdný návrat 0.

### cellSysmoduleFinalize — 0x96c07adf (cellSysmodule, P3, verified)

`s32 cellSysmoduleFinalize(void); -> r3=s32`

**Ověřené jméno/registrace:** `M/cellSysmodule.cpp:1381`; definice `M/cellSysmodule.cpp:741`. RPCS3 implementace je dokumentované no-op CELL_OK, nikoli unload všech modulů. Pro skutečný FW 3.01 ještě ověřit původní export před přijetím no-op; firmware kompatibilita pouze odvozená.

### unknown_0x9fb6228e — 0x9fb6228e (sysPrxForUser, P3, hypothesis)

`unknown sysPrxForUser export; derived shape (const char *fmt,...), r3=fmt,r4..r10/stack=args; return/complete ABI unknown`

**Ověřené:** knihovna a původní export; oficiální jméno nezjištěno. Oficiální jméno nenalezeno v RPCS3 ani PSL1GHT (ani web primary source). Firmware 3.01 OPD 0x2802066c ->0x28005e08, TOC 0x28028a80, volá _sys_vprintf a podle flag bit0x10 provádí tw nebo další call. Hypotéza diagnostická fatal/assert funkce; nesmí být ztotožněna se spinlockem ani implementována jako printf-only success. Viz samostatná původní evidence níže.

### sys_prx_exitspawn_with_level — 0xa2c7ba64 (sysPrxForUser, P3, derived)

`s32 sys_prx_exitspawn_with_level(s32 level, /* dalsi ABI nezjisteno */); r3=level (derived) -> r3=s32`

**Ověřené jméno/registrace:** `M/sys_prx_.cpp:279`; definice `M/sys_prx_.cpp:248`. RPCS3 prototyp () a návrat 0 jsou TODO, nejsou kontrakt. Firmware export 0x280018a8 ukládá r3 a pro nulový interní registry vrací 0; nenulová větev enumeruje moduly (syscall 494) a volá callbacky. Hra 0x102c8 nastaví r3=0 před 0x102d8. Další argumenty, chyby a úplný exitspawn lifecycle nejsou ověřeny; nevytvářet obecný success stub.

### sys_process_exit — 0xe6f2c1e7 (sysPrxForUser, P3, verified)

`void sys_process_exit(s32 status); r3=status; successful path does not return`

**Ověřené jméno/registrace:** `M/sysPrxForUser.cpp:239`; definice `M/sysPrxForUser.cpp:26`. Ukončit guest proces včetně vláken a zachovat exit status; úspěšná cesta nevrací do hry. Reference sysPrxForUser.cpp:26 deleguje _sys_process_exit; procesní atexit má TODO, není důkaz kompletního lifecycle.

## Přímí volající v původním herním ELF

**Ověřené staticky:** scan pouze file-backed executable PT_LOAD, po 4 B BE words; major opcode=18 a LK=1, signed LI displacement, rozlišeno AA. Každý nalezený target ověřen i `anyps3.flow.lift`. Celkem všech 15 herních stubů má přímé volající, dohromady 42 přímých bl callsite. Tyto adresy jsou **callsite**, ne začátek funkce ani runtime hit.
Nepřímé `bctrl`, tail `b` ani skutečná dosažitelnost nejsou pokryty. `callers:[]` u firmware stubů znamená **neprováděný scan firmware callerů pro JSON**, nikoli důkaz nulových volání.

| Stub | Funkce | Počet bl | Callsite EA |
| --- | --- | ---: | --- |
| `0x00a4a3dc` | sys_prx_register_library | 3 | `0x00010510`, `0x0001057c`, `0x0001058c` |
| `0x00a4a47c` | sys_prx_exitspawn_with_level | 1 | `0x000102d8` |
| `0x00a4a4fc` | sys_process_exit | 2 | `0x00010394`, `0x00911fb8` |
| `0x00a4ab3c` | cellAudioInit | 1 | `0x006ddbb4` |
| `0x00a4ab5c` | cellAudioQuit | 3 | `0x006ddab0`, `0x0099259c`, `0x009925b4` |
| `0x00a4ab7c` | cellSysmoduleUnloadModule | 13 | `0x0032a624`, `0x0032a630`, `0x0032a63c`, `0x0032a648`, `0x0032a654`, `0x0032a660`, `0x0032a66c`, `0x0032a678`, `0x0032a684`, `0x0032a690`, `0x0032a69c`, `0x0032a6a8`, `0x0032a6b4` |
| `0x00a4abdc` | cellSysmoduleFinalize | 1 | `0x0032b65c` |
| `0x00a4b07c` | sys_prx_load_module | 1 | `0x007119a8` |
| `0x00a4b0dc` | sys_prx_load_module_on_memcontainer | 1 | `0x00711858` |
| `0x00a4b0fc` | sys_lwmutex_trylock | 2 | `0x0098ab20`, `0x00993088` |
| `0x00a4b25c` | cellAudioPortClose | 3 | `0x009924ac`, `0x009a3a50`, `0x009a3fa4` |
| `0x00a4b27c` | cellAudioPortStop | 5 | `0x00992660`, `0x009a3b24`, `0x009a4120`, `0x009a4358`, `0x009a43b4` |
| `0x00a4b29c` | cellAudioGetPortConfig | 2 | `0x00992548`, `0x009a3ea0` |
| `0x00a4b2bc` | cellAudioPortStart | 2 | `0x00992760`, `0x009a4220` |
| `0x00a4b2dc` | cellAudioPortOpen | 2 | `0x0099252c`, `0x009a3e40` |

## Vlastníci firmware stubů

**Ověřené z** `local/ratchet/native-bundle-250/build.json`, `firmware_modules[].relocation.imports[].functions[]`. Všechny jsou importy z uvedené knihovny, owner je importující obraz (není totéž co poskytovatel sysPrxForUser).

| Owner | Stuby v tomto inventáři |
| --- | --- |
| libnet | `0x2001ed90`, `0x2001ee30`, `0x2001eeb0`, `0x2001ef30`, `0x2001ef70`, `0x2001ef90` |
| libssl | `0x20086260`, `0x20086400`, `0x20086420`, `0x20086480`, `0x200864c0`, `0x200864e0` |
| libhttp | `0x200f6658`, `0x200f6698`, `0x200f66b8`, `0x200f6738`, `0x200f6758`, `0x200f67b8`, `0x200f67d8`, `0x200f67f8`, `0x200f6878`, `0x200f6898`, `0x200f68b8` |
| libsysutil | `0x2012d248`, `0x2012d2a8`, `0x2012d2c8` |
| libsysutil_userinfo | `0x20141a58`, `0x20141a78` |
| libfs | `0x2018f154`, `0x2018f194` |
| libsysutil_np | `0x201e74e4`, `0x201e7584`, `0x201e75a4`, `0x201e75e4`, `0x201e7624` |
| libsre | `0x2025b994`, `0x2025b9b4`, `0x2025baf4`, `0x2025bb74`, `0x2025bb94`, `0x2025bbf4`, `0x2025bc14`, `0x2025bc54`, `0x2025bcd4` |
| libgcm_sys | `0x2028845c` |
| libio | `0x202aae54`, `0x202aae94`, `0x202aaed4` |

## Audio: minimální skutečný lifecycle

**Ověřené v referencích**, kompatibilita přesného starého FW/Sdk je **odvozená** s níže vyznačenými rozdíly. Toto je implementační podklad, ne provedená implementace.
Základní veřejný postup: `local/reference/PSL1GHT/ppu/include/audio/audio.h:1`; PCM jsou interleaved IEEE-754 float32, typicky -1..1, BE v guest paměti. Rate 48000 Hz a 256 samples/block: `local/reference/rpcs3/rpcs3/Emu/Audio/AudioBackend.h:12`.

### Guest struktury a layout

`CellAudioPortParam` (`M/cellAudio.h:88`; PSL1GHT `local/reference/PSL1GHT/ppu/include/audio/audio.h:77`): align 8, data 28 B, sizeof 32 B s posledními 4 B padding. Pole:

| Offset | Typ | Význam |
| ---: | --- | --- |
| 0 | be64 | nChannel |
| 8 | be64 | nBlock |
| 16 | be64 | attr |
| 24 | BE IEEE float32 | level |
| 28 | 4 B | padding, bez významu |

`CellAudioPortConfig` (`M/cellAudio.h:96`): align 8, sizeof 32 B:

| Offset | Typ | Význam |
| ---: | --- | --- |
| 0 | be32 EA | readIndexAddr, **adresa na be64 index**, nikoli index value |
| 4 | be32 | status READY=1 / RUN=2 / CLOSE=0x1010 |
| 8 | be64 | nChannel |
| 16 | be64 | nBlock |
| 24 | be32 | portSize, bytes |
| 28 | be32 EA | portAddr, začátek guest PCM ring |

Offsety jsou **odvozené** z pořadí C polí a jejich alignment; nezávisle potvrzené původními callsite load/store offsets níže. PSL1GHT field `readIndex` je pojmenován matoucím způsobem: RPCS3 pointer type i původní dereference potvrzují, že je to adresa.

### Init, open, config, start

1. `cellAudioInit` (NID `0x0b168f92`, r3=result): vytvořit audio manager, stabilní guest index memory, port pool. Duplicate init `0x80310701`. RPCS3 `M/cellAudio.cpp:1595` pro sdk_ver <= `0x35ffff` dovoluje **4** porty, novější 8; nevynucovat automaticky 8 jen podle array capacity. SDK version konkrétní ACIT procesu je před implementací třeba ověřit (firmware verze není sdk_ver).
2. `cellAudioPortOpen` (`0xcd7bc431`, r3=Param*, r4=be32 outPort*): provést validaci a rezervaci, nastavit READY. Veřejné parametry 2/8 channels, 8/16/32 blocks. Současné RPCS3 `M/cellAudio.cpp:1680` přijímá i channels=0 a blocks=2/4; tyto hodnoty **nepovažovat za ověřený 3.01 kontrakt**. INITLEVEL `0x1000` znamená použít level, jinak 1.0; BGM/routing attributes mají TODO, nepřevzít jejich ignorování. Přidělit skutečnou mapovanou paměť a vrátit port ID, inicializovat index=0; size=`max(1,nChannel)*nBlock*256*4`. Reference allocation rounding min/align `0x10000` a sdílené mmapper účetnictví: `M/cellAudio.cpp:1274`; není to samotná veřejná portSize.
3. `cellAudioGetPortConfig` (`0x74a66af0`, r3=port,r4=Config*): vyplnit všechny výše uvedené fields BE. `readIndexAddr` drží platnou be64 buňku po celý otevřený port lifecycle; reference používá 16B server slot a index v jeho prvních 8 B (`M/cellAudio.cpp:1317`). Nevkládat host pointer. Pro zavřený port reference vrací CLOSE a uchované fields, nikoli error PORT_NOT_OPEN.
4. `cellAudioPortStart` (`0x89be28f2`, r3=port): READY ->RUN; reference před startem nuluje portSize (`M/cellAudio.cpp:1856`). Spustit skutečnou spotřebu a synchronizaci; při opakovaném startu PORT_ALREADY_RUN. Úspěch nesmí ponechat readIndex navždy 0.

### Ring, index, mix a čas

**Ověřené:** `M/cellAudio.cpp:706` při advance spotřebuje/nuluje starý block (`reset_ports:680`), posune `cur_pos` modulo nBlock a uloží ho jako **be64** do sdílené index memory; update probíhá jen u started ports. Perioda 256/48000 s = přibližně 5,333 ms je **odvozená** z ověřených konstant.
Blok i začíná na `portAddr + i*nChannel*256*4`; v rámci stereo bloku L,R,L,R. Port volume se aplikuje na skutečné sample, porty se mixují; 8ch mapping/downmix z `M/cellAudio.cpp:1359` nepřenést slepě z host konfigurace. Minimální runtime potřebuje spotřebovat původně vyprodukované PCM, převést BE floats a předat skutečnému audio sinku; tichý explicitně označený diagnostický sink může ověřit ring/counters, ale není důkaz slyšitelného audia nebo menu.
Pořadí publikace: přečíst/mixnout old block, uvolnit/clear old block, BE update readIndex, potom aftermix notify a probudit guest. Vyhnout se deadlocku tím, že čas pokračuje i při čekání guest audio threadu. Nejlepší integrační test má guest producer, wrap ringu a event wait, nikoli pouze host timer.
**Omezení:** veřejný PSL1GHT příklad po každém eventu produkuje následující blok od 0, RPCS3 index označuje právě server position po advance; vzdálenost write/read a startup pořadí musí odpovídat původnímu produceru. Nelze tvrdit, že se bezpečně píše právě do čteného bloku.

### Event queue a notify (nejsou mezi sedmi herními cellAudio importy)

Pro kompletní API lifecycle mají význam i tyto služby (REG_FUNC `M/cellAudio.cpp:2529` a další):

- `s32 cellAudioCreateNotifyEventQueue(u32 *id,u64 *key)` — r3=id*,r4=key*; write BE32/BE64, vytvořit skutečnou FIFO `SYS_PPU_QUEUE=1`. Interní helper `M/cellAudio.cpp:2043`: key hledá v `0x80004d494f323221 + [0..63]`; sdk<=0x35ffff po init queue depth=2, jinak 8. Exhaustion `CELL_AUDIO_ERROR_EVENT_QUEUE`; přenáší skutečné kernel create chyby. `Ex(...,u32 flags)` r5 flags: jen bit0 (maska `0x1`) povolen, přepne na SYS_SPU_QUEUE=2.
- `s32 cellAudioSetNotifyEventQueue(u64 key)` — r3=key; připojit existující queue, chybějící/duplicate ->TRANS_EVENT, bez init NOT_INIT. Registry musí spojovat skutečný queue objekt a unikátní source, ne vždy vracet 0. `M/cellAudio.cpp:2096`. Source je `(pid<<32)+event_port_id` v referenci. Special key=0/_mxr000 retry je explicitně RPCS3 workaround a **není** ověřený FW protokol.
- Běžný aftermix event v `M/cellAudio.cpp:791`: `(source,data1=0 /*CELL_AUDIO_EVENT_MIX*/,data2=0,data3=0)` bez Ex flags. `sys_event_t` 32 B s be64 fields offsets 0,8,16,24 (`L/sys_event.h:70`). Queue receive musí skutečně blokovat/probudit PPU/SPU a napsat guest event; dráha syscall 128/129/130/131/133 je ověřená v `L/lv2.cpp:264`. Drain odstraní pending events, nevytváří audio progress.
- Ex BEFOREMIX=`0x80000000`, DECIMATE_2=`0x08000000`, DECIMATE_4=`0x10000000`, HEADPHONE=`0x20000000`, NOMIX=`0x40000000` (`M/cellAudio.h:50`). Validace Set/RemoveEx odmítá spodních 27 bits, `M/cellAudio.cpp:2215`; decimate společně dává x8. BEFOREMIX mění event semantics/data3 a ack (viz `M/cellAudio.cpp:2439`); není součást minimální flags=0 cesty a musí explicitně zastavit, pokud není implementováno.
- `s32 cellAudioRemoveNotifyEventQueue(u64 key)` — r3=key; vymazat matching registraci (Ex také matching flags), jinak TRANS_EVENT. `M/cellAudio.cpp:2229`. Queue životnost je samostatná; disconnect nepředstírá destroy.

Zadání žádá event lifecycle jako implementační podklad; **hra nemá tyto Notify importy v původní cellAudio import tabulce** (`anyps3.analysis.imports`, sedm NID uvedených v katalogu). Je proto nutné ověřit její skutečný producer a zda polluje readIndex, používá syscall events nebo další firmware/mixer; nepřidávat tuto queue cestu svévolně do hry. `cellSurMixer*` není v seznamu 63 importů.

### Stop, close, quit a audio chyby

Stop (`0x5b1e2c73`) RUN ->READY, zrušit další spotřebu; bez běhu PORT_NOT_RUN. Close (`0x4129fe2d`) uzavře a uvolní buffer i běžícího portu; již zavřený PORT_NOT_OPEN. Remove notify ->destroy queue je nezávislé cleanup pořadí podle PSL1GHT. Quit (`0xca5ac370`) reference zavře všechny porty a uvolní shared audio memory, event queues nechá samostatně. Pozdní timer/event nesmí přistupovat k uvolněným bufferům.

Chyby `M/cellAudio.h:18`: ALREADY_INIT `0x80310701`, AUDIOSYSTEM `...02`, NOT_INIT `...03`, PARAM `...04`, PORT_FULL `...05`, PORT_ALREADY_RUN `...06`, PORT_NOT_OPEN `...07`, PORT_NOT_RUN `...08`, TRANS_EVENT `...09`, PORT_OPEN `...0a`, SHAREDMEMORY `...0b`, MUTEX `...0c`, EVENT_QUEUE `...0d`, AUDIOSYSTEM_NOT_FOUND `...0e`, TAG_NOT_FOUND `...0f`. Prefix `...` je vždy `0x803107`. Samotný enum není důkaz, že každá služba vrací všechny tyto chyby; konkrétní větve uvedené v katalogu a zdroji.

### Původní ACIT audio parametry a readIndex důkaz

**Ověřené staticky v EBOOT.analysis.elf (hash výše), nikoli za běhu:**

- `0x99250c` r3=sp+0x70; stores `0x992520/0x992528` dávají nBlock=8/nChannel=8; `0x992524` attr=0; r4 načten z guest global. `bl 0x99252c ->0xa4b2dc`. Následné `bl 0x992548 ->GetPortConfig`, config začíná sp+0x90; `lwz 0x992564` čte Config+0, `lwz 0x992568` čte Config+28 (portAddr).
- Druhý PortOpen `0x9a3e40`: nChannel vypočten **2 nebo 8** (r27 je mask 6, pak +2), nBlock=16 (`0x9a3e34`), attr=0x1000 (`0x9a3e38`), level float 1.0 (`0x9a3e18/0x9a3e3c`), outPort=r28. `GetPortConfig 0x9a3ea0` do sp+0x90, Config+0 načte `0x9a3eac`, Config+28 `0x9a3eb0`.
- `0x9a3eb8` ukládá ukazatel readIndexAddr+4 do objektu+0x28: to je nízký BE32 word uvnitř **BE64** buňky. Runtime musí mít 8 B, nikoli v Config+0 přímo index nebo host pointer. Zda větev a producer běží cestou do menu je dosud hypotéza.

## Další guest struktury a sporné protokoly

**Odvozené offsety, ověřené field types:**

- `sys_lwmutex_t` (`L/sys_lwmutex.h:26`): size24 align8, BE32 owner+0/waiter+4/attribute+8/recursive_count+12/sleep_queue+16/pad+20. owner free=`0xffffffff`, dead=`0xfffffffe`, reserved=`0xfffffffd` (`L/sys_lwmutex.h:19`). Nevytvářet druhý nespojený lock state; respektovat stávající `anyps3/lightweight_sync.py`. Trylock native regrese: cizí owner EBUSY bez blokování/zápisu, rekurze/boundary/dead, BE bytes, kernel reserved handoff.
- `sys_prx_load_module_option_t` (`L/sys_prx.h:49`): be64 size+0, be32 base_addr+8, padding+12, size16 align8. Moderní RPCS3 pOpt ignoruje v load helperu; tento fakt **neověřuje** podporu options ve 3.01. Vyžaduje vlastní lookup/validation.
- Export library record: běžný header 0x1c (`L/sys_prx.cpp:928`), fields z `local/reference/rpcs3/rpcs3/Emu/Cell/PPUModule.cpp:678`: size u8+0, unk u8+1, version be16+2, attributes be16+4, num_func be16+6, num_var be16+8, num_tlsvar be16+10, hash/unk bytes+12..15, name EA32+16, NIDs EA32+20, address table EA32+24. Delší records mohou mít další fields +28..; ověřit size/bounds, NID počty, export OPD code/TOC a původní obraz před registrací. Registrace neznačí module_start. Library errors katalog v `L/sys_prx.h:12`, zejména LIBRARY_FOUND `0x8001113b`, ILLEGAL_LIBRARY `0x8001113d`, UNKNOWN_MODULE `0x8001112e`; nelze je automaticky přiřadit neověřeným větvím.
- `sys_spu_image` (`L/sys_spu.h:145`): 16 B, BE32 type+0, entry_point/ID+4, segs EA32+8, signed nsegs+12. USER/KERNEL ownership při close viz katalog; původní import allocations nesmí být uvolněny dvakrát.
- Formatter: `M/sys_libc_.cpp:13` má GPR varargs + stack fallback; guest sizes char1/short2/int4/long4/longlong8/size_t4/ptrdiff4. `ps3_fmt_src::get<f64>:61` interpretuje vararg bits jako double, nikoli další integer argument. Nelze poslat guest va_list do host vsnprintf. FW 3.01 variadic wrapper nepojmenovaného exportu ukládá r4..r10 do 8B slots a předává **32bitovou adresu** slotů do _sys_vprintf; omezeně doložený simple integer varargs layout, nikoli univerzální SysV host va_list.
- Thread atexit (`M/sys_ppu_thread_.cpp:12/192/168`): sdílených osm OPD callbacks, registry pod lwmutex; exit callback skutečně vykonat se správným TOC, pak free TLS. Výsledný pořádek je z reference, kompletní FW error/null policy dosud neověřena.
- Heaps: `M/sys_heap.cpp:22` ignoruje create arg2..4 a alloc/free ignorují heap handle. Není to dostatečný podklad pro kapacitu, flags ani firmware errors. Nejdřív rozlišit původní FW allocator a existující AnyPS3 memory model; testovat ownership, exhaustion, alignment, aliases a guest bytes.
- Spinlock **není žádný z 63 NID**. REG_FUNC `M/sys_spinlock.cpp:54` / PSL1GHT `ppu/sprx/liblv2/exports.h:72`: initialize `0x8c2bb498`, lock `0xa285139d`, trylock `0x722a0254`, unlock `0x5267cb35`; BE32 lock word, reference held=`0xabadcafe`, trylock EBUSY, init/unlock stores0. Pokud se objeví nový skutečný stop, sdílet scheduling a atomické memory ordering; nezaměnit unknown `0x9fb6228e` za spinlock.


## cellLibprof: ABI odvozené z původního libsre calleru

**Ověřené staticky v** `local/ratchet/libsre-relocated.elf` (hash výše): Register callsite `0x2900c6d4` a `0x2900c730` míří na stub `0x2901b994`, NID `0x05893e7c`. První nastaví r3=sp+0x78, r4=sp+0x70, r5=4, r6=object+0x988; druhý stejné stack pointers a r6=object+0x98c. Návrat r3 se porovnává s 0; pak pokračuje druhou registrací nebo failure return.

**Odvozené:** minimální signature čtyři argumenty `descriptor*`, `arg2*`, `u32 arg3`, `u32 *outHandle`, výsledek s32. Směr/přesný význam arg2 a jednotka arg3 nejsou doloženy; nepovažovat r5=4 za ověřený byte count. Pozorovaných 40 B descriptoru: BE32 code+0 (`0x20001803` nebo `0x20001804`), BE32 value1+4, BE64 objectEA+8, BE64 zero+16, čtyři BE32 hodnoty z TOC+24/+28/+32/+36. Není dokázáno, že poslední fields jsou všechny pointers; trace schema a případný větší rozsah jsou otevřené.

Unregister callsite `0x2900c47c` a `0x2900c49c` míří na stub `0x2901b9b4`, NID `0x6d045c2e`; r3 se načte z object+0x988/+0x98c a při sentinel `0xffffffff` se call přeskočí. To podporuje handle-output interpretaci r6 v Register, ale nejde o runtime proof ani úplnou specifikaci. Výsledek Unregister se ignoruje; nesprávné handle/error chování nelze nahradit vymyšleným CELL_OK. JSON status obou zůstává hypothesis pro neznámý celý protokol, přestože uvedené call instructions jsou ověřené.

## Neznámý NID 0x9fb6228e: původní evidence

**Ověřené staticky:** `local/ratchet/liblv2-relocated.json`, export library=sysPrxForUser, OPD `0x2802066c`, bytes odpovídají kódu `0x28005e08`/TOC `0x28028a80` ve hashově ověřeném `liblv2-relocated.elf`.
Kód `0x28005e10..0x28005e38` uloží r4..r10 do 8B argument slots; r3 zůstává prvním argumentem. `0x28005e3c` volá `0x28002b90`, jehož ověřené export NID=`0xfa7f693d` (_sys_vprintf). Když interní flag bit0x10 platí, `0x28005e58` nebo `0x28005e84` vykoná tw; následná větev `0x28005e90` volá `0x28005ea8`, není zdokumentována jako pouhý návrat success.
Importuje ho **libsre**, 250 stub `0x2025bb94`. Ve starší hashově ověřené relokaci libsre je stub `0x2901bb94`; typické callsite `0x29000714/0x29000748` následují _sys_printf stub a čtou r3 z konstantní TOC položky. To podporuje **hypotézu diagnostické fatal/assert služby**. Oficiální jméno, všechny argumenty, konečný stop policy a chyby nejsou doloženy; JSON proto `unknown_0x9fb6228e`, status=hypothesis.

**Vyvrácené:** tento NID není některý z veřejných sys_spinlock_*; ověřené vypočtené hashe mají výše uvedené odlišné hodnoty. Také jednoduché kandidáty _sys_assert/_sys_abort/_sys_panic neodpovídají hashi. Není důkazem, že je služba bezpečně přeskočitelná. Pokud ji skutečný běh zavolá, zachovat r3/varargs, message, callsite a původní flag stav; vykonat nebo přesně vysvětlit originální fail path.

## Pořadí implementace a ověření

1. **P1 audio** Init ->PortOpen ->GetPortConfig ->Start společně s ring consumer/readIndex a reálným schedulerem. Obě původní konfigurace (8ch/8block attr0, 2-or-8ch/16block level1) dát do native regrese s nezávislými očekávanými byte layouts, wrap, start/stop/error transitions. Pokud další skutečný stop vyžaduje unload/load dříve, pořadí přizpůsobit stopu; seznam není důkaz runtime dosažitelnosti.
2. **P1 sdílené prostředky** lwmutex_trylock, callback registry a libc byte primitives/malloc; použít existující memory/sync model, implementovat skutečné writes a ownership. Heaps nejprve doplnit FW argument evidence, nepřenést ignorované arguments z reference. Qsort vyžaduje staticky přeložený comparator se skutečným OPD/TOC evidence.
3. **P2 module lifecycle** PRX load/on_memcontainer/register a Sysmodule unload s dependencies, refcount, skutečným stop/unload, chybami. `cellSysmoduleLoadModule` **není** v tomto inventáři; 250 obsahuje native module catalog a používá vlastní cestu. Reference load flow `M/cellSysmodule.cpp:408/788` je podklad pro lifecycle, ne důvod duplikovat jiné implementace. Audio ID=`0x11` (`M/cellSysmodule.h:32`), ale libaudio není v 250 native_modules; importy mohou jít do vlastní služby, ne tvrdit načtení neexistujícího modulu. Finalize není automatické unload-all.
4. **P2 cleanup a výpisy** audio Stop/Close/Quit, spu image close, SPU printf attach/detach callbacks, printf/snprintf/v* se skutečným guest formatterem. Registry queue/create/receive musí být funkční, pokud skutečný producer tuto cestu použije; success-only notify neřeší deadlock.
5. **P3 otevřené protokoly** cellLibprof register/unregister ABI, unknown NID a exitspawn. U neznámého ponechat explicitní trap; známá služba může vrátit skutečnou doloženou chybu (např. chybějící SPU printf callback ESTAT). RPCS3 TODO=0 neopravňuje sukces.

Hypotéza: audio init/port cesta se aktivuje při front_end loading/menu, zatímco sysmodule unload a část libc heap se může zavolat i při přechodu assetů; alternativa: jde o cleanup nebo nepoužité větve. Rozlišující důkaz je další **autorizovaný skutečný herní běh** s LR/r3.. a potvrzeným import stopem. Tento dokument žádný takový běh neprovedl.

## Validace a omezení

Dokumentační kontroly: přesná shoda 63 vstupních importů s 250 traps, 41 unique NID, 40 REG_FUNC name hashes, v JSON všech 63 unikátních stubů a povinná pole, platné priority/status (33×P1, 24×P2, 6×P3; 40×verified, 20×derived, 3×hypothesis), existence file:line, ELF identity a callsite target kontrola přes lift. Původní herní data ani firmware nebyly měněny. Žádná kompilace/hra/regresní sada spuštěna; pro pouhou dokumentaci nepředstíráme runtime testy.

`verified` reference není certifikace všech chyb firmware 3.x. Zejména cellLibprof, unnamed export, heap arg2..4/memcontainer a formatter error/truncation zůstávají otevřené a jsou explicitně označené. Není vloženo náhradní menu ani tvrzení o audio/menu/gamepad úspěchu. Při implementaci nejprve ověřit aktuální sources/profile/živé procesy a nový skutečný stop; sestavu 250 ani staré logy nepřepisovat.
