# Imports of build 250: ABI, semantics and implementation order

Date: 2026-10-14. Scope: documentation research; no runtime/profile/test change, no build or game run.
Outputs are only this file and `local/ratchet/codex/imports-250.json`. CURRENT/HANDOFF/INDEX and the history are not changed because of the explicit path ownership in the assignment.
The original menu and gamepad navigation are not verified by this task.

## Scope and provenance of evidence

**Statically verified:** all 63 lines of `local/ratchet/codex/imports250.txt` exactly match the import traps in build.json 250: 41 distinct NIDs, 15 game stubs and 48 firmware stubs.
40 NIDs have a name verified via REG_FUNC + NID recomputation. One remains unnamed (`0x9fb6228e`).
The NID is computed as SHA-1(name + suffix), the first 4 digest bytes as an LE u32; source `local/reference/rpcs3/rpcs3/Emu/Cell/PPUModule.cpp:58`.
This LE hash **does not change** the big-endian guest data ABI. All listed imports are missing from the current `anyps3/services.py:9` IMPORTS.

References verified at HEAD:

- RPCS3 `ffba2bf09ef3980638f9327b25240d81414034ab`, root `local/reference/rpcs3`.
- PSL1GHT `f649a08fd536a9e27c08c7db2d93a2d7ee4c3bbe`, root `local/reference/PSL1GHT`.

In the tables, **M/** stands for `local/reference/rpcs3/rpcs3/Emu/Cell/Modules/` and **L/** stands for `local/reference/rpcs3/rpcs3/Emu/Cell/lv2/`; the actual full file:line references are also in the JSON.
`verified` means a specific source or static fact; **it does not mean a hardware test or a full match with firmware 3.01**.
`derived` marks a reconstruction of the ABI or an incomplete contract; `hypothesis` marks an unsupported name/ABI/behavior.
Priorities are always a **hypothesis about future reachability of the menu**, not a runtime trace: 1 common init/data path, 2 helper service/cleanup, 3 exit/debug/unknown protocol. The JSON status is conservative for the whole contract; a name can be verified even with a hypothesis ABI.

**Current limitation of the takeover:** the Git workspace is not available (`git status` reports not a git repository), nothing was initialized. `ps` in the available PID namespace showed only its own short-running tools; it cannot confirm the absence of foreign host processes. No live tool/session handle was taken over.
250 had no verification.json when read; it cannot be declared a verified runtime. The 247 verification read has rc=1 and a stop on an untranslated block, menu/nav=false; this is older evidence, not a current live process. The knowledge base pages still describe handoff 246 and worktree drift; runtime work is not resumed here.

## Identity of inputs (SHA-256)

- `local/ratchet/EBOOT.analysis.elf`: `6debdca0f181f35d98ebe23faabe2531b697c558b4e3384b06630173400674c1`.
- `local/ratchet/native-bundle-250/build.json`: `cb17e737cb084674bd4a5e52a327c35cf81057eb775e786e12eaf55e57ab5eb2`.
- Snapshot `local/ratchet/native-bundle-250/profile.json`: `1295ea542d9f930ae6279d0f9105611cc563808824ae9ab4f59edbac08e07bef`; current snapshot content verified.
- `anyps3/services.py`: `00ac45a738cf83e9c2833056ce8720b1d45e62e41726f9f54e11216b9b6a4dad`; matches the build's source_sha256.
- `local/ratchet/firmware-verified/extraction.json`: version **3.01**, HMAC verified, ECDSA **unverified**.
- `local/ratchet/liblv2-relocated.elf`: `843006f84aa0f195ae85119e891238f896ae8f9faf339904709a91bd3be9bdfc`; hash verified against `liblv2-relocated.json`.
- `local/ratchet/libsre-relocated.elf`: `9cdcc1242d8703c9f3f72f8dd6ce986837593a73f7ca78ec128f9fbde22434fa`; hash verified against `libsre-relocated.json`.

Do not confuse the diagnostic relocations of liblv2 `0x28000000` / libsre `0x29000000` with the relocations of the libsre firmware module of build 250 (`0x202...`). The OPD/code addresses below refer to a specific image; the profile is not changed.

## Common ABI and errors

**Derived from the PPU binding and the original callers:** fixed integer/pointer arguments in the order r3..r10; a guest pointer is a 32-bit EA even with a 64-bit GPR. `ppu_thread&` in the reference is the host context, it is **not** an additional guest argument. The integer/error result is in r3; preserve an s32 error as a signed word; `void` gives no licence to invent a return error. A callback is a guest OPD (code + TOC), not a host address.
Store PS3 guest u16/u32/u64 and float bit patterns as BE. Byte strings/PCM order and padding have their own contract. For `const char*`, validate the guest mapping; do not read via a host libc pointer cast.

Numeric CELL errors verified in `local/reference/rpcs3/rpcs3/Emu/Cell/ErrorCodes.h:103`: EINVAL `0x80010002`, ENOMEM `0x80010004`, ESRCH `0x80010005`, EDEADLK `0x80010008`, EPERM `0x80010009`, EBUSY `0x8001000a`, ESTAT `0x8001000f`, EKRESOURCE `0x80010011`. They are not a universal substitute for the errors of every service; only the relevant branches are listed below. An invalid pointer in libc is not automatically CELL_EINVAL.

Numeric Sysmodule errors: `M/cellSysmodule.h:4` DUPLICATED `0x80012001`, UNKNOWN `0x80012002`, UNLOADED `0x80012003`, INVALID_MEMCONTAINER `0x80012004`, FATAL `0x800120ff`. **Verified in the reference:** the unload helper `M/cellSysmodule.cpp:461` returns 0 at count=0; the UNLOADED enum alone is therefore not evidence of an error for this specific branch. The load helper increments the refcount, performs module_start on the first load and a rollback if it fails (`M/cellSysmodule.cpp:408`); default modules guard the SDK-dependent branch `M/cellSysmodule.cpp:897`. The exact FW 3.01 policy still has to be verified in the original libsysmodule image.

## Catalog of all 41 NIDs

Each entry includes the prototype with register mapping, the source of the definition + registration, the priority, the semantics and the limits of the error contract. Duplicate stubs have separate objects in the JSON.

### cellAudioInit — 0x0b168f92 (cellAudio, P1, verified)

`s32 cellAudioInit(void); -> r3=s32`

**Verified name/registration:** `M/cellAudio.cpp:2521`; definition `M/cellAudio.cpp:1520`. Create the audio state and the guest index shared memory; a duplicate init gives ALREADY_INIT, a memory/transport failure must be a real error. SDK-dependent 4/8 ports and lifecycle details below.

### _sys_strrchr — 0x191f0c4a (sysPrxForUser, P1, verified)

`const char *_sys_strrchr(const char *s,char c); r3=s,r4=low8(c) -> r3=pointer32 or 0`

**Verified name/registration:** `M/sys_libc_.cpp:511`; definition `M/sys_libc_.cpp:353`. Last occurrence, including the terminator for c=0; not found gives NULL. Byte semantics without allocations; firmware behavior for an invalid pointer is not verified.

### _sys_strncasecmp — 0x1ca525a2 (sysPrxForUser, P1, verified)

`s32 _sys_strncasecmp(const char *a,const char *b,u32 n); r3=a,r4=b,r5=n -> r3=s32`

**Verified name/registration:** `M/sys_libc_.cpp:510`; definition `M/sys_libc_.cpp:336`. Compare at most n characters with ASCII lowercase, stop after NUL; n=0 without reading, the reference gives -1/0/+1. Do not carry over the host locale. The reference _sys_tolower:98 limits the input to -1..127; high-bit bytes/FW locale are not yet verified.

### _sys_memmove — 0x27427742 (sysPrxForUser, P1, verified)

`void *_sys_memmove(void *dst,const void *src,u32 n); r3=dst,r4=src,r5=n -> r3=dst32`

**Verified name/registration:** `M/sys_libc_.cpp:500`; definition `M/sys_libc_.cpp:180`. Copy with support for both overlap directions; n=0 without access. Validate both guest ranges before writing; do not implement as memcpy. No separate guest error code.

### _sys_heap_malloc — 0x35168520 (sysPrxForUser, P1, derived)

`u32 _sys_heap_malloc(u32 heap,u32 size); r3=heap,r4=size -> r3=pointer32 (0 on allocation failure, derived)`

**Verified name/registration:** `M/sys_heap.cpp:89`; definition `M/sys_heap.cpp:38`. Allocate non-aliasing guest memory and record it in the given heap. RPCS3 ignores the heap ID and calls vm::alloc; the correct handle validation, alignment and size=0 contract of FW 3.01 are not verified; this is not a successful return of a zero error code.

### sys_ppu_thread_register_atexit — 0x3dd4a957 (sysPrxForUser, P1, verified)

`s32 sys_ppu_thread_register_atexit(void (*guest_opd)(void)); r3=OPD32 -> r3=s32`

**Verified name/registration:** `M/sys_ppu_thread_.cpp:284`; definition `M/sys_ppu_thread_.cpp:192`. Write the callback into a shared eight-entry table guarded by an lwmutex; a duplicate gives EPERM, a full table ENOMEM. sys_ppu_thread_exit:168 really executes them before the TLS free; it is neither a process atexit nor necessarily a thread-local list.

### _sys_strchr — 0x7498887b (sysPrxForUser, P1, verified)

`const char *_sys_strchr(const char *s, char c); r3=s,r4=low8(c) -> r3=guest pointer32 or 0`

**Verified name/registration:** `M/sys_libc_.cpp:506`; definition `M/sys_libc_.cpp:261`. First occurrence including the terminator at c=0; NULL if not found. Access to guest bytes, no new allocation; do not confuse with returning an index. The reference does not define an error code for an invalid pointer.

### cellAudioGetPortConfig — 0x74a66af0 (cellAudio, P1, verified)

`s32 cellAudioGetPortConfig(u32 port, CellAudioPortConfig *out); r3=port,r4=out -> r3=s32`

**Verified name/registration:** `M/cellAudio.cpp:2524`; definition `M/cellAudio.cpp:1778`. Fill all 32 B of the guest structure, especially a valid address of the be64 readIndex and the PCM portAddr; NOT_INIT / PARAM. For a closed port the reference returns status CLOSE and preserved buffer fields, not PORT_NOT_OPEN. Layout below.

### cellAudioPortStart — 0x89be28f2 (cellAudio, P1, verified)

`s32 cellAudioPortStart(u32 port); r3=port -> r3=s32`

**Verified name/registration:** `M/cellAudio.cpp:2525`; definition `M/cellAudio.cpp:1823`. READY -> RUN, zero the ring, start periodic consumption and the readIndex. NOT_INIT / PARAM / PORT_NOT_OPEN / PORT_ALREADY_RUN.

### _sys_heap_free — 0x8a561d92 (sysPrxForUser, P1, derived)

`s32 _sys_heap_free(u32 heap,u32 address); r3=heap,r4=address -> r3=s32`

**Verified name/registration:** `M/sys_heap.cpp:91`; definition `M/sys_heap.cpp:52`. Free the allocation in the respective heap, preserve other memory. RPCS3 ignores the heap and returns 0 even without checking vm::dealloc; the error codes for invalid/double/free of a foreign allocation in FW 3.01 are not verified.

### sys_lwmutex_trylock — 0xaeb78725 (sysPrxForUser, P1, verified)

`s32 sys_lwmutex_trylock(sys_lwmutex32 *m); r3=m -> r3=s32`

**Verified name/registration:** `M/sys_lwmutex_.cpp:407`; definition `M/sys_lwmutex_.cpp:273`. Atomically acquire ownership with the current guest TID; do not block when occupied (EBUSY). Recursion per attr; EDEADLK for a non-recursive owner, EKRESOURCE on recursive_count overflow, EINVAL on a dead marker. A reserved owner requires a kernel handoff. Share state with the existing lock/unlock implementation.

### _sys_heap_create_heap — 0xb2fcf2c8 (sysPrxForUser, P1, derived)

`u32 _sys_heap_create_heap(const char *name,u32 arg2,u32 arg3,u32 arg4); r3=name,r4=arg2,r5=arg3,r6=arg4 -> r3=heap handle32`

**Verified name/registration:** `M/sys_heap.cpp:87`; definition `M/sys_heap.cpp:22`. RPCS3 creates a HeapInfo with the name only; it ignores the meaning of arg2..4. Create real accounting/allocator state only after verifying the original FW arguments; the range, capacity, flags and error ABI are not verified. The name and the 4-register signature are verified in the reference.

### _sys_malloc — 0xbdb18f83 (sysPrxForUser, P1, verified)

`void *_sys_malloc(u32 size); r3=size -> r3=guest pointer32 or 0`

**Verified name/registration:** `M/sys_libc_.cpp:513`; definition `M/sys_libc_.cpp:371`. Allocate a real guest allocation, it must not overwrite other objects; NULL on shortage (allocator), not an error_code. The reference VM alignment is not evidence of FW granularity; size=0 and limit details are not yet verified.

### cellAudioPortOpen — 0xcd7bc431 (cellAudio, P1, verified)

`s32 cellAudioPortOpen(const CellAudioPortParam *param,u32 *outPort); r3=param,r4=outPort -> r3=s32; *outPort=be32`

**Verified name/registration:** `M/cellAudio.cpp:2527`; definition `M/cellAudio.cpp:1652`. Validate the parameters, reserve the port, allocate real guest PCM memory, initialize state READY and the index; NOT_INIT / PARAM / PORT_FULL / SHAREDMEMORY. The parameters of both original callsites and the layout are below.

### _sys_strncpy — 0xd3039d4d (sysPrxForUser, P1, verified)

`char *_sys_strncpy(char *dst,const char *src,s32 len); r3=dst,r4=src,r5=len -> r3=dst32 or 0`

**Verified name/registration:** `M/sys_libc_.cpp:509`; definition `M/sys_libc_.cpp:311`. Copy at most len bytes; after NUL pad the remainder with zeros, at an exact/truncated limit do not add a terminator. RPCS3: null src/dst ->NULL, len<=0 without a write. Overlap is not memmove, invalid mapped ranges must be an explicit runtime error.

### _sys_free — 0xf7f7fb20 (sysPrxForUser, P1, verified)

`void _sys_free(void *address); r3=address; return void`

**Verified name/registration:** `M/sys_libc_.cpp:515`; definition `M/sys_libc_.cpp:393`. Free the corresponding guest allocation, do not accept a host pointer. The reference delegates to vm::dealloc without an error return; the NULL/double/foreign free policy of FW 3.01 must be verified, do not add invented guest errno.

### _sys_memcmp — 0xfb5db080 (sysPrxForUser, P1, verified)

`s32 _sys_memcmp(const u8 *a,const u8 *b,u32 n); r3=a,r4=b,r5=n -> r3=s32`

**Verified name/registration:** `M/sys_libc_.cpp:498`; definition `M/sys_libc_.cpp:135`. Unsigned-byte lexicographic comparison of exactly n bytes, not strings; n=0 gives equality and no reading. The reference returns -1/0/+1, the general C contract only the sign. Writes nothing, no errno defined.

### _sys_vsnprintf — 0x0618936b (sysPrxForUser, P2, derived)

`s32 _sys_vsnprintf(char *dst,u32 capacity,const char *fmt,guest_va_list ap); r3=dst,r4=capacity,r5=fmt,r6=ap -> r3=s32 (derived ABI)`

**Verified name/registration:** `M/sys_libc_.cpp:521`; definition `M/sys_libc_.cpp:465`. RPCS3 () is a TODO. FW 3.01 export 0x28005030 preserves dst/capacity, passes fmt/ap to the formatter, stores a limit of capacity-1, and when capacity!=0 appends NUL at dst+result. The exact truncation/count==0 contract and all formats are not verified. A guest va_list is not a host va_list; do not confuse with variadic snprintf.

### _sys_snprintf — 0x06574237 (sysPrxForUser, P2, derived)

`s32 _sys_snprintf(char *dst,u32 capacity,const char *fmt,...); r3=dst,r4=capacity,r5=fmt,varargs r6..r10/stack -> r3=s32`

**Verified name/registration:** `M/sys_libc_.cpp:517`; definition `M/sys_libc_.cpp:417`. Really format and NUL-terminate within the capacity, no host dereference of guest pointers. RPCS3 returns the number of characters actually written (capacity==0 ->0 with the comment ???), not the C99 required length; the FW 3.01 return details must be verified via the original formatter. Truncation, %s/%p/widths and guest varargs must have regression tests.

### cellSysmoduleUnloadModule — 0x112a5ee9 (cellSysmodule, P2, verified)

`s32 cellSysmoduleUnloadModule(u16 id); r3=low16(id) -> r3=s32`

**Verified name/registration:** `M/cellSysmodule.cpp:1384`; definition `M/cellSysmodule.cpp:873`. Decrease the real loaded_count; at the last reference perform module_stop and unload, remove the exports, respect dependencies and protected default modules. For the UNKNOWN/UNLOADED/FATAL catalog see below; the reference also has an SDK <3.30 branch.

### sys_prx_load_module — 0x26090058 (sysPrxForUser, P2, verified)

`s32 sys_prx_load_module(const char *path, u64 flags, const sys_prx_load_module_option32 *opt); r3=path,r4=flags,r5=opt -> r3=positive module ID or negative error`

**Verified name/registration:** `M/sys_prx_.cpp:264`; definition `M/sys_prx_.cpp:36`. Load the original module, relocate/link and return a real positive ID; module_start is a separate operation. EINVAL for disallowed flags, ENOSYS for an unsupported fixed address, ENOENT for a missing file, ILLEGAL_LIBRARY for an invalid PRX (lv2/sys_prx.cpp:184). The static catalog must verify identity and range, not accept an arbitrary path.

### __sys_look_ctype_table — 0x3ef17f8c (sysPrxForUser, P2, verified)

`s16 __sys_look_ctype_table(s32 ch); r3=ch -> r3=signed mask16`

**Verified name/registration:** `M/sys_libc_.cpp:492`; definition `M/sys_libc_.cpp:90`. Returns the mask value, not a pointer to a table! Index ch+1 for -1..127; EOF ->0, upper bit1/lower bit2/digit bit4/space bit8/punct bit0x10/control bit0x20/hex bit0x40 per sys_libc_.cpp:72. For input out of range the reference stops on ensure; the extended FW charset is not verified.

### cellAudioPortClose — 0x4129fe2d (cellAudio, P2, verified)

`s32 cellAudioPortClose(u32 port); r3=port -> r3=s32`

**Verified name/registration:** `M/cellAudio.cpp:2522`; definition `M/cellAudio.cpp:1863`. Detach and free the open port (even a running one), cancel its consumption, return the slot to the pool. NOT_INIT / PARAM / PORT_NOT_OPEN; success only after it has been performed.

### sys_prx_register_library — 0x42b23552 (sysPrxForUser, P2, verified)

`s32 sys_prx_register_library(const prx_library32 *entry); r3=entry -> r3=s32`

**Verified name/registration:** `M/sys_prx_.cpp:273`; definition `M/sys_prx_.cpp:165`. Register the library and connect the NID/export OPDs with the imports, not merely acknowledge acceptance. RPCS3 lv2/sys_prx.cpp:917 verifies the address (EFAULT) and performs loader linking. Entry format below; the full firmware errors LIBRARY_FOUND/ILLEGAL_LIBRARY are a catalog, not all branches of this reference are verified.

### cellAudioPortStop — 0x5b1e2c73 (cellAudio, P2, verified)

`s32 cellAudioPortStop(u32 port); r3=port -> r3=s32`

**Verified name/registration:** `M/cellAudio.cpp:2523`; definition `M/cellAudio.cpp:1900`. RUN -> READY and stop consumption. NOT_INIT / PARAM / PORT_NOT_RUN including a closed port. Do not confuse with close.

### _sys_spu_printf_detach_group — 0x5fdfb2fe (sysPrxForUser, P2, verified)

`s32 _sys_spu_printf_detach_group(u32 group); r3=group -> r3=s32`

**Verified name/registration:** `M/sys_spu_.cpp:498`; definition `M/sys_spu_.cpp:449`. Without a registered detach callback return ESTAT, otherwise execute the real guest callback with group and return its result. Callback initialization is sys_spu_.cpp:412; detach itself must not simulate stdout.

### _sys_printf — 0x9f04f7af (sysPrxForUser, P2, derived)

`s32 _sys_printf(const char *fmt,...); r3=fmt,varargs r4..r10/stack -> r3=s32`

**Verified name/registration:** `M/sys_libc_.cpp:518`; definition `M/sys_libc_.cpp:437`. Format the real guest content into the TTY/log. RPCS3 returns CELL_OK after sys_tty_write, it does not fulfil the usual C printf byte-count contract; for FW 3.01 verify the return and the error path separately. Dropping the output with a false success is not an implementation.

### sys_prx_load_module_on_memcontainer — 0xaa6d9bff (sysPrxForUser, P2, derived)

`s32 sys_prx_load_module_on_memcontainer(const char *path,u32 container,u64 flags,const sys_prx_load_module_option32 *opt); r3=path,r4=container,r5=flags,r6=opt -> r3=positive ID or negative error`

**Verified name/registration:** `M/sys_prx_.cpp:266`; definition `M/sys_prx_.cpp:54`. The same with real accounting/binding to the memory container. RPCS3 lv2/sys_prx.cpp:534 ignores the container: it cannot be taken over as verified firmware semantics. The name and argument order are verified, the container errors are open.

### sys_ppu_thread_unregister_atexit — 0xac6fc404 (sysPrxForUser, P2, verified)

`s32 sys_ppu_thread_unregister_atexit(void (*guest_opd)(void)); r3=OPD32 -> r3=s32`

**Verified name/registration:** `M/sys_ppu_thread_.cpp:285`; definition `M/sys_ppu_thread_.cpp:218`. Erase the matching callback in the same guarded table; ESRCH when it is not registered; do not call the callback on unregister.

### _sys_heap_delete_heap — 0xaede4b03 (sysPrxForUser, P2, derived)

`s32 _sys_heap_delete_heap(u32 heap); r3=heap -> r3=s32`

**Verified name/registration:** `M/sys_heap.cpp:88`; definition `M/sys_heap.cpp:29`. RPCS3 only removes the HeapInfo ID without validation. The required service must cancel the real heap lifecycle/ownership of allocations; the error codes and the outstanding allocation policy of FW 3.01 cannot be determined from this reference.

### _sys_qsort — 0xc4fd6121 (sysPrxForUser, P2, verified)

`void _sys_qsort(void *base,u32 count,u32 size,s32 (*guest_opd)(const void *,const void *)); r3=base,r4=count,r5=size,r6=OPD32; return void`

**Verified name/registration:** `M/sys_libc_.cpp:524`; definition `M/sys_libc_.cpp:477`. Sort count elements of size size using a really executed guest comparator with guest addresses in r3/r4 and the correct OPD TOC; not a host function-pointer cast. Prevent count*size overflow and out-of-range memory; size/count==0 safely without the comparator. The reference offers no errno.

### cellAudioQuit — 0xca5ac370 (cellAudio, P2, verified)

`s32 cellAudioQuit(void); -> r3=s32`

**Verified name/registration:** `M/cellAudio.cpp:2526`; definition `M/cellAudio.cpp:1617`. Close open ports, free the audio shared memory and reset init; without init NOT_INIT. RPCS3 leaves the event queues separate, it does not destroy them automatically.

### _sys_spu_printf_attach_group — 0xdd0c1e09 (sysPrxForUser, P2, verified)

`s32 _sys_spu_printf_attach_group(u32 group); r3=group -> r3=s32`

**Verified name/registration:** `M/sys_spu_.cpp:497`; definition `M/sys_spu_.cpp:437`. Without an attach callback ESTAT, otherwise really call the respective guest OPD callback with group. Return its result; do not report attachment of a nonexistent event service.

### sys_spu_image_close — 0xe0da8efd (sysPrxForUser, P2, verified)

`s32 sys_spu_image_close(sys_spu_image32 *image); r3=image -> r3=s32`

**Verified name/registration:** `M/sys_spu_.cpp:490`; definition `M/sys_spu_.cpp:356`. USER type=0 frees the segment descriptor allocation; KERNEL type=1 closes the kernel image ID via a syscall. Any other type gives EINVAL; with a running SPU preserve the correct LS/data ownership. 16 B layout below; do not erase random guest memory.

### _sys_vprintf — 0xfa7f693d (sysPrxForUser, P2, derived)

`s32 _sys_vprintf(const char *fmt,guest_va_list ap); r3=fmt,r4=ap -> r3=s32 (derived ABI)`

**Verified name/registration:** `M/sys_libc_.cpp:520`; definition `M/sys_libc_.cpp:459`. RPCS3 () is a TODO; the original 3.01 export 0x28002b90 preserves r3=fmt/r4=ap and passes them to the formatter. Format the guest arguments into a real TTY; the exact byte count and error path are not proven by this short disassembly.

### cellUserTraceRegister — 0x05893e7c (cellLibprof, P3, hypothesis)

`s32 cellUserTraceRegister(trace_descriptor32 *arg1,void *arg2,u32 arg3,u32 *outHandle); r3=arg1,r4=arg2,r5=arg3,r6=outHandle -> r3=s32 (derived caller ABI, names provisional)`

**Verified name/registration:** `M/cellLibprof.cpp:33`; definition `M/cellLibprof.cpp:12`. Verified name/REG_FUNC, but RPCS3 UNIMPLEMENTED_FUNC +0 is not a contract. The original libsre caller supplies four arguments and tests the return against 0; the partial ABI/descriptor layout is derived below. The meaning of arg2/arg3, the whole trace protocol and errors are not verified. Requires a real registry, not a success TODO.

### cellUserTraceUnregister — 0x6d045c2e (cellLibprof, P3, hypothesis)

`cellUserTraceUnregister(u32 handle); r3=handle (derived); return type/error ABI unknown (RPCS3 placeholder s32)`

**Verified name/registration:** `M/cellLibprof.cpp:34`; definition `M/cellLibprof.cpp:18`. Verified name/REG_FUNC; the original libsre caller loads a u32 handle, skips -1 and otherwise calls unregister. It ignores the return, so its type and error ABI are not documented. The hypothesis of removing the matching trace record must be verified from FW; do not take over an empty return of 0.

### cellSysmoduleFinalize — 0x96c07adf (cellSysmodule, P3, verified)

`s32 cellSysmoduleFinalize(void); -> r3=s32`

**Verified name/registration:** `M/cellSysmodule.cpp:1381`; definition `M/cellSysmodule.cpp:741`. The RPCS3 implementation is a documented no-op CELL_OK, not an unload of all modules. For the real FW 3.01, verify the original export before accepting the no-op; firmware compatibility is only derived.

### unknown_0x9fb6228e — 0x9fb6228e (sysPrxForUser, P3, hypothesis)

`unknown sysPrxForUser export; derived shape (const char *fmt,...), r3=fmt,r4..r10/stack=args; return/complete ABI unknown`

**Verified:** the library and the original export; the official name was not determined. The official name was not found in RPCS3 or PSL1GHT (nor in a web primary source). Firmware 3.01 OPD 0x2802066c ->0x28005e08, TOC 0x28028a80, calls _sys_vprintf and depending on flag bit 0x10 performs tw or another call. Hypothesis: a diagnostic fatal/assert function; it must not be equated with a spinlock nor implemented as a printf-only success. See the separate original evidence below.

### sys_prx_exitspawn_with_level — 0xa2c7ba64 (sysPrxForUser, P3, derived)

`s32 sys_prx_exitspawn_with_level(s32 level, /* further ABI not determined */); r3=level (derived) -> r3=s32`

**Verified name/registration:** `M/sys_prx_.cpp:279`; definition `M/sys_prx_.cpp:248`. The RPCS3 prototype () and the return of 0 are a TODO, not a contract. The firmware export 0x280018a8 stores r3 and for a zero internal registry returns 0; the nonzero branch enumerates modules (syscall 494) and calls callbacks. The game at 0x102c8 sets r3=0 before 0x102d8. Further arguments, errors and the full exitspawn lifecycle are not verified; do not create a generic success stub.

### sys_process_exit — 0xe6f2c1e7 (sysPrxForUser, P3, verified)

`void sys_process_exit(s32 status); r3=status; successful path does not return`

**Verified name/registration:** `M/sysPrxForUser.cpp:239`; definition `M/sysPrxForUser.cpp:26`. Terminate the guest process including threads and preserve the exit status; the successful path does not return to the game. The reference sysPrxForUser.cpp:26 delegates to _sys_process_exit; the process atexit has a TODO, which is not evidence of a complete lifecycle.

## Direct callers in the original game ELF

**Statically verified:** scan of file-backed executable PT_LOAD only, in 4 B BE words; major opcode=18 and LK=1, signed LI displacement, AA distinguished. Each target found was also verified with `anyps3.flow.lift`. In total all 15 game stubs have direct callers, 42 direct bl callsites altogether. These addresses are **callsites**, not the start of a function nor a runtime hit.
Indirect `bctrl`, tail `b` and real reachability are not covered. `callers:[]` for firmware stubs means **a firmware-caller scan that was not performed for the JSON**, not evidence of zero calls.

| Stub | Function | bl count | Callsite EA |
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

## Owners of firmware stubs

**Verified from** `local/ratchet/native-bundle-250/build.json`, `firmware_modules[].relocation.imports[].functions[]`. All are imports from the listed library, the owner is the importing image (which is not the same as the provider of sysPrxForUser).

| Owner | Stubs in this inventory |
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

## Audio: minimal real lifecycle

**Verified in the references**, compatibility with the exact old FW/Sdk is **derived** with the differences marked below. This is an implementation basis, not a performed implementation.
Basic public procedure: `local/reference/PSL1GHT/ppu/include/audio/audio.h:1`; PCM is interleaved IEEE-754 float32, typically -1..1, BE in guest memory. Rate 48000 Hz and 256 samples/block: `local/reference/rpcs3/rpcs3/Emu/Audio/AudioBackend.h:12`.

### Guest structures and layout

`CellAudioPortParam` (`M/cellAudio.h:88`; PSL1GHT `local/reference/PSL1GHT/ppu/include/audio/audio.h:77`): align 8, data 28 B, sizeof 32 B with the last 4 B padding. Fields:

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | be64 | nChannel |
| 8 | be64 | nBlock |
| 16 | be64 | attr |
| 24 | BE IEEE float32 | level |
| 28 | 4 B | padding, no meaning |

`CellAudioPortConfig` (`M/cellAudio.h:96`): align 8, sizeof 32 B:

| Offset | Type | Meaning |
| ---: | --- | --- |
| 0 | be32 EA | readIndexAddr, **address of the be64 index**, not the index value |
| 4 | be32 | status READY=1 / RUN=2 / CLOSE=0x1010 |
| 8 | be64 | nChannel |
| 16 | be64 | nBlock |
| 24 | be32 | portSize, bytes |
| 28 | be32 EA | portAddr, start of the guest PCM ring |

The offsets are **derived** from the order of the C fields and their alignment; independently confirmed by the original callsite load/store offsets below. The PSL1GHT field `readIndex` is named in a misleading way: both the RPCS3 pointer type and the original dereference confirm that it is an address.

### Init, open, config, start

1. `cellAudioInit` (NID `0x0b168f92`, r3=result): create the audio manager, stable guest index memory, port pool. Duplicate init `0x80310701`. RPCS3 `M/cellAudio.cpp:1595` allows **4** ports for sdk_ver <= `0x35ffff`, 8 for newer ones; do not automatically enforce 8 merely from the array capacity. The SDK version of the concrete ACIT process needs to be verified before implementation (the firmware version is not sdk_ver).
2. `cellAudioPortOpen` (`0xcd7bc431`, r3=Param*, r4=be32 outPort*): perform validation and reservation, set READY. Public parameters 2/8 channels, 8/16/32 blocks. Current RPCS3 `M/cellAudio.cpp:1680` also accepts channels=0 and blocks=2/4; **do not consider these values a verified 3.01 contract**. INITLEVEL `0x1000` means use the level, otherwise 1.0; the BGM/routing attributes have a TODO, do not take over their ignoring. Allocate real mapped memory and return the port ID, initialize index=0; size=`max(1,nChannel)*nBlock*256*4`. Reference allocation rounding min/align `0x10000` and shared mmapper accounting: `M/cellAudio.cpp:1274`; that is not the public portSize itself.
3. `cellAudioGetPortConfig` (`0x74a66af0`, r3=port,r4=Config*): fill all the fields above as BE. `readIndexAddr` holds a valid be64 cell for the whole lifecycle of an open port; the reference uses a 16B server slot and the index in its first 8 B (`M/cellAudio.cpp:1317`). Do not insert a host pointer. For a closed port the reference returns CLOSE and preserved fields, not the error PORT_NOT_OPEN.
4. `cellAudioPortStart` (`0x89be28f2`, r3=port): READY ->RUN; the reference zeroes portSize before start (`M/cellAudio.cpp:1856`). Start real consumption and synchronization; on a repeated start PORT_ALREADY_RUN. Success must not leave readIndex 0 forever.

### Ring, index, mix and time

**Verified:** `M/cellAudio.cpp:706` on advance consumes/zeroes the old block (`reset_ports:680`), moves `cur_pos` modulo nBlock and stores it as **be64** into the shared index memory; the update happens only for started ports. The period 256/48000 s = approximately 5.333 ms is **derived** from verified constants.
Block i starts at `portAddr + i*nChannel*256*4`; within a stereo block L,R,L,R. Port volume is applied to the actual samples, ports are mixed; do not blindly carry over the 8ch mapping/downmix from `M/cellAudio.cpp:1359` from the host configuration. The minimal runtime needs to consume the originally produced PCM, convert the BE floats and hand them to a real audio sink; a silent, explicitly labeled diagnostic sink can verify the ring/counters, but it is not evidence of audible audio or the menu.
Publication order: read/mix the old block, release/clear the old block, BE update of readIndex, then aftermix notify and wake the guest. Avoid a deadlock by letting time continue even while the guest audio thread is waiting. The best integration test has a guest producer, ring wrap and event wait, not merely a host timer.
**Limitations:** the public PSL1GHT example produces the next block from 0 after each event, RPCS3's index denotes precisely the server position after advance; the write/read distance and startup order must match the original producer. It cannot be claimed that it is safe to write into the very block being read.

### Event queue and notify (not among the seven game cellAudio imports)

These services also matter for a complete API lifecycle (REG_FUNC `M/cellAudio.cpp:2529` and further):

- `s32 cellAudioCreateNotifyEventQueue(u32 *id,u64 *key)` — r3=id*,r4=key*; write BE32/BE64, create a real FIFO `SYS_PPU_QUEUE=1`. Internal helper `M/cellAudio.cpp:2043`: looks for the key in `0x80004d494f323221 + [0..63]`; sdk<=0x35ffff after init queue depth=2, otherwise 8. Exhaustion `CELL_AUDIO_ERROR_EVENT_QUEUE`; passes on real kernel create errors. `Ex(...,u32 flags)` r5 flags: only bit0 (mask `0x1`) allowed, switches to SYS_SPU_QUEUE=2.
- `s32 cellAudioSetNotifyEventQueue(u64 key)` — r3=key; attach an existing queue, missing/duplicate ->TRANS_EVENT, without init NOT_INIT. The registry must connect a real queue object and a unique source, not always return 0. `M/cellAudio.cpp:2096`. The source is `(pid<<32)+event_port_id` in the reference. The special key=0/_mxr000 retry is explicitly an RPCS3 workaround and is **not** a verified FW protocol.
- Common aftermix event in `M/cellAudio.cpp:791`: `(source,data1=0 /*CELL_AUDIO_EVENT_MIX*/,data2=0,data3=0)` without Ex flags. `sys_event_t` 32 B with be64 fields at offsets 0,8,16,24 (`L/sys_event.h:70`). Queue receive must really block/wake the PPU/SPU and write the guest event; the syscall path 128/129/130/131/133 is verified in `L/lv2.cpp:264`. Drain removes pending events, it does not create audio progress.
- Ex BEFOREMIX=`0x80000000`, DECIMATE_2=`0x08000000`, DECIMATE_4=`0x10000000`, HEADPHONE=`0x20000000`, NOMIX=`0x40000000` (`M/cellAudio.h:50`). The Set/RemoveEx validation rejects the low 27 bits, `M/cellAudio.cpp:2215`; decimate together gives x8. BEFOREMIX changes the event semantics/data3 and the ack (see `M/cellAudio.cpp:2439`); it is not part of the minimal flags=0 path and must explicitly stop if it is not implemented.
- `s32 cellAudioRemoveNotifyEventQueue(u64 key)` — r3=key; erase the matching registration (Ex also matching flags), otherwise TRANS_EVENT. `M/cellAudio.cpp:2229`. Queue lifetime is separate; disconnect does not pretend to be destroy.

The assignment asks for the event lifecycle as an implementation basis; **the game does not have these Notify imports in the original cellAudio import table** (`anyps3.analysis.imports`, the seven NIDs listed in the catalog). It is therefore necessary to verify its real producer and whether it polls readIndex, uses syscall events or another firmware/mixer; do not arbitrarily add this queue path to the game. `cellSurMixer*` is not in the list of 63 imports.

### Stop, close, quit and audio errors

Stop (`0x5b1e2c73`) RUN ->READY, cancel further consumption; when not running PORT_NOT_RUN. Close (`0x4129fe2d`) closes and frees the buffer even of a running port; an already closed port gives PORT_NOT_OPEN. Remove notify ->destroy queue is an independent cleanup order according to PSL1GHT. Quit (`0xca5ac370`): the reference closes all ports and frees the shared audio memory, leaving the event queues separate. A late timer/event must not access freed buffers.

Errors `M/cellAudio.h:18`: ALREADY_INIT `0x80310701`, AUDIOSYSTEM `...02`, NOT_INIT `...03`, PARAM `...04`, PORT_FULL `...05`, PORT_ALREADY_RUN `...06`, PORT_NOT_OPEN `...07`, PORT_NOT_RUN `...08`, TRANS_EVENT `...09`, PORT_OPEN `...0a`, SHAREDMEMORY `...0b`, MUTEX `...0c`, EVENT_QUEUE `...0d`, AUDIOSYSTEM_NOT_FOUND `...0e`, TAG_NOT_FOUND `...0f`. The prefix `...` is always `0x803107`. The enum alone is not evidence that every service returns all of these errors; the specific branches are given in the catalog and the source.

### Original ACIT audio parameters and readIndex evidence

**Statically verified in EBOOT.analysis.elf (hash above), not at runtime:**

- `0x99250c` r3=sp+0x70; stores `0x992520/0x992528` give nBlock=8/nChannel=8; `0x992524` attr=0; r4 loaded from a guest global. `bl 0x99252c ->0xa4b2dc`. The following `bl 0x992548 ->GetPortConfig`, config starts at sp+0x90; `lwz 0x992564` reads Config+0, `lwz 0x992568` reads Config+28 (portAddr).
- Second PortOpen `0x9a3e40`: nChannel computed as **2 or 8** (r27 is mask 6, then +2), nBlock=16 (`0x9a3e34`), attr=0x1000 (`0x9a3e38`), level float 1.0 (`0x9a3e18/0x9a3e3c`), outPort=r28. `GetPortConfig 0x9a3ea0` into sp+0x90, Config+0 loaded at `0x9a3eac`, Config+28 at `0x9a3eb0`.
- `0x9a3eb8` stores the pointer readIndexAddr+4 into object+0x28: that is the low BE32 word inside the **BE64** cell. The runtime must provide 8 B, not an index or a host pointer directly in Config+0. Whether this branch and producer run on the way to the menu is still a hypothesis.

## Other guest structures and disputed protocols

**Derived offsets, verified field types:**

- `sys_lwmutex_t` (`L/sys_lwmutex.h:26`): size24 align8, BE32 owner+0/waiter+4/attribute+8/recursive_count+12/sleep_queue+16/pad+20. owner free=`0xffffffff`, dead=`0xfffffffe`, reserved=`0xfffffffd` (`L/sys_lwmutex.h:19`). Do not create a second unconnected lock state; respect the existing `anyps3/lightweight_sync.py`. Trylock native regression: foreign owner EBUSY without blocking/write, recursion/boundary/dead, BE bytes, kernel reserved handoff.
- `sys_prx_load_module_option_t` (`L/sys_prx.h:49`): be64 size+0, be32 base_addr+8, padding+12, size16 align8. Modern RPCS3 ignores pOpt in the load helper; this fact **does not verify** support for options in 3.01. Requires its own lookup/validation.
- Export library record: common header 0x1c (`L/sys_prx.cpp:928`), fields from `local/reference/rpcs3/rpcs3/Emu/Cell/PPUModule.cpp:678`: size u8+0, unk u8+1, version be16+2, attributes be16+4, num_func be16+6, num_var be16+8, num_tlsvar be16+10, hash/unk bytes+12..15, name EA32+16, NIDs EA32+20, address table EA32+24. Longer records may have further fields +28..; verify size/bounds, NID counts, export OPD code/TOC and the original image before registration. Registration does not imply module_start. Library errors catalog in `L/sys_prx.h:12`, especially LIBRARY_FOUND `0x8001113b`, ILLEGAL_LIBRARY `0x8001113d`, UNKNOWN_MODULE `0x8001112e`; they cannot be automatically assigned to unverified branches.
- `sys_spu_image` (`L/sys_spu.h:145`): 16 B, BE32 type+0, entry_point/ID+4, segs EA32+8, signed nsegs+12. For USER/KERNEL ownership on close see the catalog; original import allocations must not be freed twice.
- Formatter: `M/sys_libc_.cpp:13` has GPR varargs + stack fallback; guest sizes char1/short2/int4/long4/longlong8/size_t4/ptrdiff4. `ps3_fmt_src::get<f64>:61` interprets the vararg bits as a double, not as another integer argument. A guest va_list cannot be sent to the host vsnprintf. The FW 3.01 variadic wrapper of the unnamed export stores r4..r10 into 8B slots and passes the **32-bit address** of the slots to _sys_vprintf; a simple integer varargs layout with limited evidence, not a universal SysV host va_list.
- Thread atexit (`M/sys_ppu_thread_.cpp:12/192/168`): shared eight OPD callbacks, registry under an lwmutex; really execute the exit callback with the correct TOC, then free the TLS. The resulting order is from the reference, the complete FW error/null policy is not yet verified.
- Heaps: `M/sys_heap.cpp:22` ignores create arg2..4 and alloc/free ignore the heap handle. That is not a sufficient basis for capacity, flags or firmware errors. First distinguish the original FW allocator and the existing AnyPS3 memory model; test ownership, exhaustion, alignment, aliases and guest bytes.
- Spinlock is **none of the 63 NIDs**. REG_FUNC `M/sys_spinlock.cpp:54` / PSL1GHT `ppu/sprx/liblv2/exports.h:72`: initialize `0x8c2bb498`, lock `0xa285139d`, trylock `0x722a0254`, unlock `0x5267cb35`; BE32 lock word, reference held=`0xabadcafe`, trylock EBUSY, init/unlock store 0. If a new real stop appears, share scheduling and atomic memory ordering; do not mistake the unknown `0x9fb6228e` for a spinlock.


## cellLibprof: ABI derived from the original libsre caller

**Statically verified in** `local/ratchet/libsre-relocated.elf` (hash above): the Register callsites `0x2900c6d4` and `0x2900c730` target the stub `0x2901b994`, NID `0x05893e7c`. The first sets r3=sp+0x78, r4=sp+0x70, r5=4, r6=object+0x988; the second the same stack pointers and r6=object+0x98c. The return r3 is compared with 0; then it continues with the second registration or a failure return.

**Derived:** minimal signature of four arguments `descriptor*`, `arg2*`, `u32 arg3`, `u32 *outHandle`, result s32. The direction/exact meaning of arg2 and the unit of arg3 are not documented; do not consider r5=4 a verified byte count. Observed 40 B of the descriptor: BE32 code+0 (`0x20001803` or `0x20001804`), BE32 value1+4, BE64 objectEA+8, BE64 zero+16, four BE32 values from TOC+24/+28/+32/+36. It is not proven that the last fields are all pointers; the trace schema and a possible larger extent are open.

The Unregister callsites `0x2900c47c` and `0x2900c49c` target the stub `0x2901b9b4`, NID `0x6d045c2e`; r3 is loaded from object+0x988/+0x98c and on the sentinel `0xffffffff` the call is skipped. This supports the handle-output interpretation of r6 in Register, but it is not runtime proof nor a complete specification. The Unregister result is ignored; incorrect handle/error behavior cannot be replaced by an invented CELL_OK. The JSON status of both remains hypothesis for the unknown whole protocol, even though the call instructions listed are verified.

## Unknown NID 0x9fb6228e: original evidence

**Statically verified:** `local/ratchet/liblv2-relocated.json`, export library=sysPrxForUser, OPD `0x2802066c`, the bytes correspond to code `0x28005e08`/TOC `0x28028a80` in the hash-verified `liblv2-relocated.elf`.
The code `0x28005e10..0x28005e38` stores r4..r10 into 8B argument slots; r3 remains the first argument. `0x28005e3c` calls `0x28002b90`, whose verified export NID=`0xfa7f693d` (_sys_vprintf). When the internal flag bit 0x10 is set, `0x28005e58` or `0x28005e84` executes tw; the subsequent branch `0x28005e90` calls `0x28005ea8`, not documented as a mere return of success.
It is imported by **libsre**, 250 stub `0x2025bb94`. In the older hash-verified relocation of libsre the stub is `0x2901bb94`; the typical callsites `0x29000714/0x29000748` follow the _sys_printf stub and read r3 from a constant TOC entry. This supports the **hypothesis of a diagnostic fatal/assert service**. The official name, all arguments, the final stop policy and errors are not documented; the JSON therefore has `unknown_0x9fb6228e`, status=hypothesis.

**Refuted:** this NID is not one of the public sys_spinlock_*; the verified computed hashes have the different values given above. Likewise the simple candidates _sys_assert/_sys_abort/_sys_panic do not match the hash. That is not evidence that the service can be safely skipped. If a real run calls it, preserve r3/varargs, message, callsite and the original flag state; execute or precisely explain the original fail path.

## Implementation and verification order

1. **P1 audio** Init ->PortOpen ->GetPortConfig ->Start together with the ring consumer/readIndex and a real scheduler. Put both original configurations (8ch/8block attr0, 2-or-8ch/16block level1) into native regression tests with independent expected byte layouts, wrap, start/stop/error transitions. If the next real stop requires unload/load earlier, adapt the order to the stop; the list is not evidence of runtime reachability.
2. **P1 shared resources** lwmutex_trylock, callback registry and libc byte primitives/malloc; use the existing memory/sync model, implement real writes and ownership. For heaps first add FW argument evidence, do not carry over the ignored arguments from the reference. Qsort requires a statically translated comparator with real OPD/TOC evidence.
3. **P2 module lifecycle** PRX load/on_memcontainer/register and Sysmodule unload with dependencies, refcount, real stop/unload, errors. `cellSysmoduleLoadModule` is **not** in this inventory; 250 contains a native module catalog and uses its own path. The reference load flow `M/cellSysmodule.cpp:408/788` is a basis for the lifecycle, not a reason to duplicate other implementations. Audio ID=`0x11` (`M/cellSysmodule.h:32`), but libaudio is not in the 250 native_modules; imports may go to a separate service, do not claim the loading of a nonexistent module. Finalize is not an automatic unload-all.
4. **P2 cleanup and output** audio Stop/Close/Quit, spu image close, SPU printf attach/detach callbacks, printf/snprintf/v* with a real guest formatter. Registry queue/create/receive must be functional if the real producer uses this path; a success-only notify does not solve a deadlock.
5. **P3 open protocols** cellLibprof register/unregister ABI, unknown NID and exitspawn. For the unknown one keep an explicit trap; a known service may return a real documented error (e.g. a missing SPU printf callback ESTAT). RPCS3 TODO=0 does not justify success.

Hypothesis: the audio init/port path is activated during front_end loading/menu, while sysmodule unload and part of the libc heap may also be called during an asset transition; alternative: it is cleanup or unused branches. The distinguishing evidence is another **authorized real game run** with LR/r3.. and a confirmed import stop. This document performed no such run.

## Validation and limitations

Documentation checks: exact match of the 63 input imports with the 250 traps, 41 unique NIDs, 40 REG_FUNC name hashes, all 63 unique stubs and the required fields in the JSON, valid priorities/status (33×P1, 24×P2, 6×P3; 40×verified, 20×derived, 3×hypothesis), existence of file:line, ELF identity and callsite target check via lift. The original game data and firmware were not modified. No compilation/game/regression suite was run; for mere documentation we do not pretend to have runtime tests.

A `verified` reference is not a certification of all firmware 3.x errors. In particular cellLibprof, the unnamed export, heap arg2..4/memcontainer and formatter error/truncation remain open and are explicitly marked. No substitute menu was inserted, nor any claim of audio/menu/gamepad success. When implementing, first verify the current sources/profile/live processes and a new real stop; do not overwrite build 250 or the old logs.
