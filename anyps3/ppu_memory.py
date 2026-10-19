"""PPUFAST guest memory access helpers for translated PPU code.

The inline path reads one blocker byte for the 4 KiB guest page and then
accesses the flat guest view (GuestMemory::InlineFlat, par_memory.py). Any
blocker, page crossing or address outside the 32-bit space calls the
out-of-line path, which first tries the in-section medium path and otherwise
leaves the section and performs the unchanged admitted GuestMemory operation.
"""

# Appended to MEMORY_CPP. Templates on the CPU state type: translated pages use
# PpuState, isolated lifter harnesses use any struct with `memory`/`reservation`.
CPP = r'''
// ---- PPUFAST guest memory helpers (header only; slow paths are COMDAT) ----
template<class S> [[gnu::noinline,gnu::cold]] inline std::uint64_t ppu_load_slow(S& s,std::uint64_t ea,unsigned size){
    auto& m=s.memory;
    if(m.inline_view.active){std::uint8_t bytes[8];
        if(m.inline_medium_load(ea,size,bytes)){std::uint64_t v=0;for(unsigned i=0;i<size;++i)v=(v<<8)|bytes[i];return v;}}
    ++m.inline_counters.slow_loads;
    m.inline_suspend();const auto value=m.load(ea,size);m.inline_resume();return value;
}
template<class S> [[gnu::noinline,gnu::cold]] inline void ppu_store_slow(S& s,std::uint64_t ea,unsigned size,std::uint64_t value){
    auto& m=s.memory;
    if(m.inline_view.active){std::uint8_t bytes[8];auto v=value;for(unsigned i=size;i--;){bytes[i]=static_cast<std::uint8_t>(v);v>>=8;}
        if(m.inline_medium_store(ea,size,bytes))return;}
    ++m.inline_counters.slow_stores;
    m.inline_suspend();m.store(ea,size,value);m.inline_resume();
}
template<unsigned N,class S> [[gnu::always_inline]] inline std::uint64_t ppu_load(S& s,std::uint64_t ea){
    auto& m=s.memory;
    if(__builtin_expect((ea>>32)==0 && (N==1 || (ea&4095)<=4096-N) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::load_block),1))
        return GuestMemory::inline_read<N>(m.inline_view.base+ea);
    return ppu_load_slow(s,ea,N);
}
template<unsigned N,class S> [[gnu::always_inline]] inline void ppu_store(S& s,std::uint64_t ea,std::uint64_t value){
    auto& m=s.memory;
    if(__builtin_expect((ea>>32)==0 && (N==1 || (ea&4095)<=4096-N) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::store_block),1)){
        GuestMemory::inline_write<N>(m.inline_view.base+ea,value);return;}
    ppu_store_slow(s,ea,N,value);
}
// Byte ranges (VMX, dcbz, string forms): same admission, payload copied with
// the byte API's atomic word/byte rules.
template<class S> [[gnu::noinline,gnu::cold]] inline void ppu_read_bytes_slow(S& s,std::uint64_t ea,std::uint8_t* out,unsigned n){
    auto& m=s.memory;
    if(m.inline_view.active && m.inline_medium_load(ea,n,out))return;
    ++m.inline_counters.slow_loads;
    m.inline_suspend();
    if(m.concurrent())m.transfer(ea,out,n,false);else std::copy_n(m.access(ea,n,false),n,out);
    m.inline_resume();
}
template<class S> [[gnu::noinline,gnu::cold]] inline void ppu_write_bytes_slow(S& s,std::uint64_t ea,const std::uint8_t* in,unsigned n){
    auto& m=s.memory;
    if(m.inline_view.active && m.inline_medium_store(ea,n,in))return;
    ++m.inline_counters.slow_stores;
    m.inline_suspend();
    if(m.concurrent())m.transfer(ea,const_cast<std::uint8_t*>(in),n,true);else std::copy_n(in,n,m.access(ea,n,true));
    m.inline_resume();
}
template<class S> [[gnu::always_inline]] inline void ppu_read_bytes(S& s,std::uint64_t ea,std::uint8_t* out,unsigned n){
    auto& m=s.memory;
    if(__builtin_expect(GuestMemory::inline_span(ea,n) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::load_block),1)){
        GuestMemory::atomic_read(m.inline_view.base+ea,out,n);return;}
    ppu_read_bytes_slow(s,ea,out,n);
}
template<class S> [[gnu::always_inline]] inline void ppu_write_bytes(S& s,std::uint64_t ea,const std::uint8_t* in,unsigned n){
    auto& m=s.memory;
    if(__builtin_expect(GuestMemory::inline_span(ea,n) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::store_block),1)){
        GuestMemory::atomic_write(m.inline_view.base+ea,in,n);return;}
    ppu_write_bytes_slow(s,ea,in,n);
}
// Translated pages (ppu_emit.py) test the inline path and, only when it is
// not admitted, publish PC/steps/modified registers before the slow path.
template<unsigned N,class S> [[gnu::always_inline]] inline bool ppu_try_load(S& s,std::uint64_t ea,std::uint64_t& value){
    auto& m=s.memory;
    if(__builtin_expect((ea>>32)==0 && (N==1 || (ea&4095)<=4096-N) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::load_block),1)){
        value=GuestMemory::inline_read<N>(m.inline_view.base+ea);return true;}
    return false;
}
template<unsigned N,class S> [[gnu::always_inline]] inline bool ppu_try_store(S& s,std::uint64_t ea,std::uint64_t value){
    auto& m=s.memory;
    if(__builtin_expect((ea>>32)==0 && (N==1 || (ea&4095)<=4096-N) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::store_block),1)){
        GuestMemory::inline_write<N>(m.inline_view.base+ea,value);return true;}
    return false;
}
template<class S> [[gnu::always_inline]] inline bool ppu_try_read_bytes(S& s,std::uint64_t ea,std::uint8_t* out,unsigned n){
    auto& m=s.memory;
    if(__builtin_expect(GuestMemory::inline_span(ea,n) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::load_block),1)){
        // lvx: aligned quadword into a vector register (16-byte destination).
        if(n==16 && !(ea&7))GuestMemory::atomic_read16(m.inline_view.base+ea,*reinterpret_cast<std::uint8_t(*)[16]>(out));
        else GuestMemory::atomic_read(m.inline_view.base+ea,out,n);
        return true;}
    return false;
}
template<class S> [[gnu::always_inline]] inline bool ppu_try_write_bytes(S& s,std::uint64_t ea,const std::uint8_t* in,unsigned n){
    auto& m=s.memory;
    if(__builtin_expect(GuestMemory::inline_span(ea,n) &&
        !(m.inline_view.table[ea>>12].load(std::memory_order_relaxed)&GuestMemory::InlineFlat::store_block),1)){
        GuestMemory::atomic_write(m.inline_view.base+ea,in,n);return true;}
    return false;
}
// Reservations keep their admitted protocol; the section resumes afterwards.
template<class S> [[gnu::noinline]] inline std::uint64_t ppu_load_reserved(S& s,std::uint64_t ea,unsigned size){
    auto& m=s.memory;m.inline_suspend();const auto value=m.load_reserved(ea,size,s.reservation);m.inline_resume();return value;
}
template<class S> [[gnu::noinline]] inline bool ppu_store_conditional(S& s,std::uint64_t ea,unsigned size,std::uint64_t value){
    auto& m=s.memory;m.inline_suspend();const bool stored=m.store_conditional(ea,size,value,s.reservation);m.inline_resume();return stored;
}
'''
