# PRX import references (attribute 0x2000) and the stop `0x1b8`

Date: 2026-10-14, Claude Opus 5.5. The goal remains the original ACIT menu and gamepad navigation.

## Verified cause of the stop of build 198

- **Verified (static, original image):** `libsysutil_np_trophy` (analysis ELF SHA-256 `8fc9fd14…b0b38d`) has in data segment 1 at offsets `0x470`/`0x474` the values `0x168`/`0x1b8` with no entry in PT_SCE_PPURELA (422 relocations; the neighboring slots `0x46c`, `0x478` have relocations). The module's OPDs lie in seg1 from `0xac` at 8-byte intervals; `0x168`/`0x1b8` are not OPD addresses.
- **Verified (disassembly):** helper `0x20221a24` stores the TOC slot `r2-0x7eec` into the packet at `+0x6c` and the user data at `+0x70`; libsysutil `0x201104d0` (export cellSysutil `0x6ba1d72f`) calls `+0x6c` as an OPD when the buffer grows. The packet init `0x20110808` (`0xcfad36de`) zeroes the buffer `+0x3c` and the capacity `+0x40`, so the first addition of a parameter always calls the callback — it is not an unreachable branch.
- **Verified (import structure):** the `cellSysutil` import of trophy has attribute `0x2009`. The 29 NIDs are followed by a second array of the same length with pointers to lists of `(type, address, addend)` terminated by `type=0`. For NID `0xb59872ef` the list is `[(1, 0x20230474, 0)]`, for `0x7fc8f72c` `[(1, 0x20230470, 0)]`. `libsysutil_userinfo`, `libnetctl` and `libsysutil_np` have the same (12 targets in total, all type 1). The game's main ELF does not have attribute `0x2000`.
- **Reference (RPCS3, `local/reference/rpcs3/rpcs3/Emu/Cell/PPUModule.cpp:607-667` and `:1034-1046`):** `ppu_patch_refs` writes the address of the linked export (`faddr + addend`) into the ref targets, the condition being `lib.attributes & 0x2000` and the pointer `nids[i + num_func]`. RPCS3 also knows types 4/6/57; they do not occur in our modules.
- **Refuted:** a wrong TOC register (diagnostic 198), a PRX relocator bug (the slot intentionally has no relocation) and the need to map the zero page.

## Change

- `anyps3/prx.py`: for imports with `0x2000`, `linkage()` reads the ref lists (`import_references`); only the verified type 1 is supported, any other type, an unaligned address or an unterminated list fails explicitly. `link_references()` writes the export's OPD address `+ addend` into the targets; an unlinked import fails.
- `anyps3/build_game.py`: after relocating all modules it links the ref targets according to `exported_bindings`, adds the export's code among the static entries and records the writes in `build.json` → `firmware_modules[].import_references`.
- **Derived limitation:** the write happens statically at build time. It corresponds to the loader because the module data are in the combined image in advance and the slot is read only by the module's code after it is loaded; the caller is libsysutil itself, so the exporter is loaded. The order of lv2 lazy linking is not emulated.
- Test: `tests/test_prx.py::test_import_references_receive_linked_export_descriptor` (literal expectation `0x20130bb4`/`+8`, errors for an unlinked import and type 4, refs are not read without the attribute). Log `local/ratchet/tests-prx-199.log`.

## Runtime

Build `local/ratchet/native-bundle-199`: trophy slot `0x20230474` → OPD `0x2013093c` (code `0x2011446c`, a dispatcher with a 12-entry jump table, all targets translated), `0x20230470` → `0x20130954` (code `0x2011439c`). For the result of the run see [CURRENT](../CURRENT.md).

## Continuation 2026-10-14 (builds 199–204)

- **199:** the stop `0x1b8` removed; trophy sends the `NPTR` packet, command `f`.
- **200–203 (verified runtime):** the native VSH side of the trophy service in `anyps3/sysutil_transport.py`: `f` CreateContext (ID 1–4, signature check `0xB9DDE13B`, TROPDIR), `8` CreateHandle (leaf `mem_context`, ID 1–4), `o` GetRequiredDiskSpace (TRP v1: size − 0x40 − n·0x40 = 2,997,008 B), `1` DestroyContext, `9` DestroyHandle; further `h` (status 1/3) and `i` (installing the TRP into `hdd0/home/00000001/trophy/<conf>`, VSH events of type 299 via the sysutil queue, a reply with the struct node `platinumId`) — `h`/`i` so far only as a native test, the game did not call them in this run. The tree format and keys derived from the disassembly of libsysutil_np_trophy/libsysutil (see CURRENT). Tests `tests/test_trophy.py`.
- **204:** `sys_timer_usleep` now runs the running SPU during the sleep (3200 instructions/µs); previously the main thread exhausted the 5 s diagnostic budget in the loop `0x4e4168` waiting for an SPU task. SPU `0x100` ran to completion (110 M instructions). Test `tests/test_spu.py::test_ppu_sleep_grants_running_spu_the_slept_interval`.
- **Stop 204:** the virtual call `0x669b28` via vtable `0xc3c428` slot 2 → OPD `0xce3178` → `0x3304b4` (TOC `0xd34a88`) was not statically translated. The vtable (10 OPDs before the first null) was added to the profile after verification in the original ELF; **the new build 205 has not run yet**.

## Continuation (205–215)

- 205–207: NPTR `term` (sceNpTrophyTerm) and `sys_lwmutex_destroy` (RPCS3 `sys_lwmutex_.cpp`, owner `0xfffffffe`).
- 208: the game's statusCb OPD `0xcd3b38` → `0x29db84` in the profile; the trophy installation to the HDD took place (55 files, 37 icons).
- 209–210: `fsqrt` (exact integer), single-precision arithmetic from full double operands with a single rounding
  (Book I: undefined — **hypothesis** of the implementation's behavior), `stfs` = Power ISA SINGLE (QEMU `helper_tosingle`).
- 211: handlers of sysutil events 299/310 in `firmware_static_seeds`.
- 212–215: vtable `0xc3c378`, static translation budget 1,000,000, verified jump tables, `stwbrx/lwbrx/sthbrx/lhbrx`.
- 229–231: automatic verified detection of GCC switch tables `anyps3/jump_tables.py` (the base proven by data TOC → pointer →
  the address after `bctr`), evidence in `build.json` → `verified_jump_tables`. Test `tests/test_jump_tables.py`.
