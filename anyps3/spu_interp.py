"""Runtime-field adapter for the shared SPU lifter (no instruction semantics).

Opcode selection stays in Python. Only operand/PC expressions become C++;
identical bodies are coalesced, including the 128 encodings of RRR operations.
The interpreter is emitted once in program.cpp, never in every SPU unit.
"""
from collections import defaultdict
from .errors import AnyPS3Error


class Expr:
    def __init__(self, text):
        self.text = str(text)

    def __str__(self):
        return self.text

    def __format__(self, spec):
        if spec:
            raise TypeError(f'constant formatting applied to runtime expression: {spec}')
        return str(self)

    def binary(self, other, op):
        return Expr(f'({self}{op}{other})')

    def __add__(self, other): return self.binary(other, '+')
    def __radd__(self, other): return Expr(other).binary(self, '+')
    def __sub__(self, other): return self.binary(other, '-')
    def __rsub__(self, other): return Expr(other).binary(self, '-')
    def __mul__(self, other): return self.binary(other, '*')
    def __and__(self, other): return self.binary(other, '&')
    def __or__(self, other): return self.binary(other, '|')
    def __lshift__(self, other): return self.binary(other, '<<')
    def __rshift__(self, other): return self.binary(other, '>>')
    def __neg__(self): return Expr(f'(-{self})')

    def __bool__(self):
        raise TypeError('runtime expression used as a Python condition')


def hex_value(value):
    return str(value) if isinstance(value, Expr) else f'0x{value:x}'


def uint32(value):
    return f'std::uint32_t({value})' if isinstance(value, Expr) else f'UINT32_C({value})'


def generate(lift):
    groups = defaultdict(list)
    for op in range(2048):
        try:
            body = lift(Expr('pc'), op << 21, runtime=True)
        except AnyPS3Error:
            continue
        groups[body].append(op)
    lines = [r'''static void spu_interpret_one(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){
    SpuBlockScope block(t);
    if(!block.limit)return;
    if(t.pc>0x3fffc || t.pc%4)spu_dynamic_fault(t,ls,"invalid SPU fetch address");
    const auto pc=t.pc,word=spu_load_word(ls+pc);
    if(t.block_trace)spu_trace_fetch(t,ls,t.steps);
    auto& g=t.gpr;[[maybe_unused]] SpuRegister out{};
    try{switch(word>>21){''']
    for body, ops in groups.items():
        lines.extend(f'case {op}:' for op in ops)
        lines.append('{'+body+'++block.executed;t.interpreted_instructions.value.fetch_add(1,std::memory_order_relaxed);return;}')
    lines.append('default:spu_dynamic_fault(t,ls,"unsupported SPU instruction");}}catch(const SpuSliceEnd&){t.interpreted_instructions.value.fetch_add(1,std::memory_order_relaxed);throw;}\n}')
    lines.append(r'''
static void spu_interpret_logged(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){
    if(!t.block_budget || t.steps>=t.block_limit)return;
    if(t.pc>0x3fffc || t.pc%4)spu_dynamic_fault(t,ls,"invalid SPU fetch address");
    spu_dynamic_record(t,ls);
    spu_interpret_one(s,t,ls);
}
void spu_interpret(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){
    if(spu_provenance_enabled())spu_interpret_logged(s,t,ls);
    else spu_interpret_one(s,t,ls);
}
template<bool Log> static void spu_interpret_many(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){
    // Bounded fallback slice. Every word is fetched again, including after a
    // store/DMA; no decoded-word validity can outlive an LS mutation. Return
    // at control transfers to try the native content dispatcher immediately.
    const auto budget=std::min(t.block_budget,16u);
    for(unsigned i=0;i<budget && t.block_budget && t.steps<t.block_limit;++i){
        if(i)spu_take_interrupt(s,t);
        const auto pc=t.pc;
        if constexpr(Log)spu_interpret_logged(s,t,ls);
        else spu_interpret_one(s,t,ls);
        if(t.block_control_exit || t.pc!=((pc+4)&0x3fffc))return;
    }
}
static void spu_interpret_block(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){
    // Exactly one logging switch per bounded slice; the false specialization
    // contains no logger calls. The public one-instruction oracle stays exact.
    if(spu_provenance_enabled())spu_interpret_many<true>(s,t,ls);
    else spu_interpret_many<false>(s,t,ls);
}
''')
    return '\n'.join(lines)
