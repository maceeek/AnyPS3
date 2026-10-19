"""Minimal single-main-thread process entry and TLS service for native probes."""

from .elf import ElfImage
from .errors import AnyPS3Error
import struct


def process_setup(image: ElfImage) -> str:
    params = [s for s in image.segments if s.type == 0x60000001 and s.file_size]
    sdk, page_size = 0, 0x100000
    if len(params)>1:
        raise AnyPS3Error('Multiple process parameter segments.')
    if params:
        if params[0].file_size < 32:
            raise AnyPS3Error('Truncated process parameter segment.')
        fields=struct.unpack('>8I',image.read_virtual(params[0].virtual_address,32))
        if fields[0]<32 or fields[1]!=0x13bcc5f6:
            raise AnyPS3Error('Invalid process parameter header.')
        sdk,page_size=fields[3],fields[6]
    tls = [s for s in image.segments if s.type == 7]
    if len(tls) > 1:
        raise AnyPS3Error("Multiple PT_TLS segments are unsupported.")
    address = file_size = memory_size = 0
    if tls:
        s = tls[0]
        address, file_size, memory_size = s.virtual_address, s.file_size, s.memory_size
        if file_size > memory_size or memory_size > 16 * 1024 * 1024:
            raise AnyPS3Error("Invalid/oversized process TLS segment.")
        if file_size:
            image.read_virtual(address, file_size)
    return f'''
    // argv/envp use big-endian 64-bit pointers at the PPU process entry.
    state.r[1] -= 0x400;
    const auto args = state.r[1] + 0x100;
    const auto text = args + 0x40;
    const char executable[] = "/dev_bdvd/PS3_GAME/USRDIR/EBOOT.BIN";
    for (unsigned i = 0; i < sizeof(executable); ++i) state.memory.store(text + i, 1, executable[i]);
    state.memory.store(args, 8, text);
    state.memory.store(args + 8, 8, 0);
    state.memory.store(args + 16, 8, 0);
    state.r[3] = 1; state.r[4] = args; state.r[5] = args + 16; state.r[6] = 0;
    state.r[7] = 1; state.r[8] = 0x{address:x}; state.r[9] = 0x{file_size:x}; state.r[10] = 0x{memory_size:x};
    state.r[11] = 0x{image.entry:x}; state.r[12] = 0x{page_size:x}; state.sdk_version = 0x{sdk:x};
'''


TLS_CPP = r'''
[[maybe_unused]] static void initialize_tls(PpuState& state) {
    auto& r = state.r;
    if (r[3] != 1) throw std::runtime_error("TLS service supports only the main thread");
    if (r[13]) return;
    if (r[5] > r[6] || r[6] > 16 * 1024 * 1024)
        throw std::runtime_error("invalid TLS sizes");
    const auto block = state.memory.allocate(r[6] + 0x30);
    for (std::uint64_t i = 0; i < r[5]; ++i)
        state.memory.store(block + 0x30 + i, 1, state.memory.load(r[4] + i, 1));
    r[13] = block + 0x7030;
    state.tls_address=r[4];state.tls_file_size=r[5];state.tls_memory_size=r[6];
    std::fprintf(stderr, "TLS main-thread r13=0x%08" PRIx64 " initialized=%" PRIu64 " zeroed=%" PRIu64 "\n",
                 r[13], r[5], r[6] - r[5]);
}
'''
