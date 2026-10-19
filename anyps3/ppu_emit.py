"""PPUFAST page emitter: block-granular accounting and registers in locals.

Each 4 KiB guest page becomes one C++ function. The step counter and the
next PC are page-function locals; architectural registers (GPR, FPR, CR,
LR, CTR, XER) are locals of each straight-line block:

* a block loads the registers it references at its head; every register an
  instruction writes is stored back right after that instruction
  (write-through). Reads never reload from PpuState inside a block, so guest
  stores and out-of-line calls cannot force reloads, and the CPU state is
  architecturally current after each instruction;
* steps are added once per block; the block head performs the existing
  scheduling-slice check, so the preemption cut stays block granular;
* every point where an exception may leave the block first publishes the
  exact PC and counted step number of that instruction ("sync"). For guest
  memory this happens only on the out-of-line path; for other throwing
  helpers (FP, VMX modes, reservations, indirect targets, traps) it precedes
  the instruction. A STOP therefore still reports the faulting instruction,
  its counted steps and the architectural registers before it, without any
  exception landing pad in the page function;
* runtime services (sc, HLE imports) get PC/steps published before the call,
  exactly like the per-instruction accounting; their PpuBlocked retry
  therefore repeats the same work;
* the hard step budget is checked at every block head: a block that would
  cross it is not entered and the stop reports that block's head PC with the
  exact executed step count (never more than the budget);
* per-instruction trace (and the exact mid-block budget cut) is a generator
  option for diagnostic builds (`trace=True`). Ordinary builds reject a
  runtime trace request explicitly instead of silently not tracing.

Bodies come from flow.lift. Their textual register accesses are rewritten to
locals; any form that would touch a localized field through `state` and is
not recognised here is rejected (fail closed), never silently mis-synced.
"""
from dataclasses import dataclass
import json
import re

from .errors import AnyPS3Error


@dataclass(frozen=True)
class EmitOptions:
    max_steps: int
    gpr_locals: bool = True
    trace: bool = False


GPR = re.compile(r'(?<![\w.])r\[(\d+)\]')
FPR = re.compile(r'state\.fpr\[(\d+)\]')
COMPARE = re.compile(r'\bcompare\(state, (\d), ([^;]*)\);')
RECORD = re.compile(r'\brecord\(state, ([^;]*)\);')
BRANCH = re.compile(r'\bbranch_test\(state, (\d+), (\d+)\)')
SHIFT = re.compile(r'\barithmetic_shift\(state, ')
STATE_CALL = re.compile(r'\b([A-Za-z_]\w*)(?:<[^<>()]*>)?\(state\s*[,)]')
CALL = re.compile(r'\b([A-Za-z_][\w:]*)\s*(?:<[^<>()]*>)?\s*\(')
MEMORY_HELPERS = ('ppu_load', 'ppu_store', 'ppu_read_bytes', 'ppu_write_bytes')
# Helpers that receive `state` but touch only FPSCR/VSCR/VR, guest memory or
# the reservation; fp_compare/fp_move_field also write CR and are synchronised.
STATE_SAFE = re.compile(r'^(fp_\w+|vmx_\w+|scope|ppu_load|ppu_store|ppu_read_bytes|ppu_write_bytes|ppu_load_reserved|ppu_store_conditional)$')
CR_SYNC_CALLS = ('fp_compare(', 'fp_move_field(')
# Calls that can neither throw nor observe PpuState.
NOTHROW = {'UINT64_C', 'UINT32_C', 'INT64_C', 'INT32_C', 'Wide', 'rotl64', 'rotl32', 'sign_word', 'signed_word',
           'leading_zeroes', 'divide_integer', 'ppu_cmp', 'ppu_crset', 'ppu_bt', 'ppu_sra', 'static_cast',
           '__builtin_bswap16', '__builtin_bswap32', '__builtin_bswap64', 'for', 'if', 'while', 'switch',
           'sizeof', 'std::atomic_thread_fence', 'timebase_ticks', 'fp_widen', 'vmx_word', 'fp_subtract_operand', 'data'}
TRANSFER_PRIMARY = (16, 18)

HELPERS_CPP = r'''
// PPUFAST local-register forms of the flow.py CR/XER/CTR helpers.
[[maybe_unused,gnu::always_inline]] static inline unsigned ppu_cmp(std::uint64_t a,std::uint64_t b,bool wide,bool signed_compare,std::uint64_t xer){
    if(!wide){a&=UINT32_MAX;b&=UINT32_MAX;}
    if(signed_compare){const auto sign=wide?UINT64_C(0x8000000000000000):UINT64_C(0x80000000);a^=sign;b^=sign;}
    return (a<b?8u:a>b?4u:2u)|unsigned((xer>>31)&1);
}
[[maybe_unused,gnu::always_inline]] static inline std::uint32_t ppu_crset(std::uint32_t cr,unsigned field,unsigned nibble){
    const unsigned shift=(7-field)*4;return (cr&~(UINT32_C(15)<<shift))|(nibble<<shift);
}
[[maybe_unused,gnu::always_inline]] static inline bool ppu_bt(std::uint64_t& ctr,std::uint32_t cr,unsigned bo,unsigned bi){
    if(!(bo&4))--ctr;
    const bool count_ok=(bo&4) || ((ctr!=0)!=bool(bo&2));
    const bool condition_ok=(bo&16) || (((cr>>(31-bi))&1)==unsigned(bool(bo&8)));
    return count_ok && condition_ok;
}
[[maybe_unused,gnu::always_inline]] static inline std::uint64_t ppu_sra(std::uint64_t& xer,std::uint64_t value,unsigned amount,unsigned width){
    if(width==32)value&=UINT32_MAX;
    const bool negative=(value>>(width-1))&1;
    const auto discarded=amount>=width?value:amount?value&((UINT64_C(1)<<amount)-1):0;
    xer=(xer&~UINT64_C(0x20000000))|((negative && discarded)?UINT64_C(0x20000000):0);
    if(amount>=width)return negative?UINT64_MAX:0;
    if(width==32)value=((value&UINT32_MAX)^UINT64_C(0x80000000))-UINT64_C(0x80000000);
    return amount?(value>>amount)|(negative?UINT64_MAX<<(64-amount):0):value;
}
'''


def step_slow_cpp(max_steps: int) -> str:
    """Exact per-instruction accounting for trace builds; block-head budget stop."""
    return '\n'.join([
        '[[noreturn,gnu::noinline,gnu::cold]] inline void ppu_step_limit(){throw std::runtime_error("execution step limit reached");}',
        '[[gnu::noinline,gnu::cold]] inline void ppu_step_slow(PpuState& state,std::uint32_t pc,std::uint64_t steps,std::uint64_t lr,std::uint64_t r1,std::uint64_t r3){',
        f'    if(steps>=UINT64_C({max_steps})){{state.pc=pc;state.steps=steps;throw std::runtime_error("execution step limit reached");}}',
        '    if(state.ppu_trace)std::fprintf(stderr,"trace pc=0x%08" PRIx32 " lr=0x%08" PRIx64 " r1=0x%08" PRIx64 " r3=0x%016" PRIx64 "\\n",pc,lr,r1,r3);',
        '}'])


def field(name):
    """Local name -> its PpuState field."""
    if name.startswith('gpr'):
        return f'state.r[{name[3:]}]'
    if name.startswith('fpr'):
        return f'state.fpr[{name[3:]}]'
    return {'cr_l': 'state.cr', 'lr_l': 'state.lr', 'ctr_l': 'state.ctr', 'xer_l': 'state.xer'}[name]


def declaration(name):
    return f'std::uint32_t {name}=state.cr;' if name == 'cr_l' else f'std::uint64_t {name}={field(name)};'


LOCAL = re.compile(r'\b(gpr\d+|fpr\d+|cr_l|lr_l|ctr_l|xer_l)\b')


def written_locals(body):
    names = set(re.findall(r'(?<![\w.])(gpr\d+|fpr\d+|cr_l|lr_l|ctr_l|xer_l)\s*(?:=(?!=)|\+=|-=|\|=|&=|\^=)', body))
    if any(not int(bo) & 4 for bo in re.findall(r'ppu_bt\(ctr_l,cr_l,(\d+),', body)):
        names.add('ctr_l')
    if 'ppu_sra(xer_l' in body:
        names.add('xer_l')
    return names


def call_arguments(text, start):
    """Split the argument list beginning after text[start-1]=='(' -> (args, end)."""
    depth, args, current, i = 0, [], [], start
    while i < len(text):
        c = text[i]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                args.append(''.join(current).strip())
                return args, i + 1
            depth -= 1
        elif c == ',' and depth == 0:
            args.append(''.join(current).strip())
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    raise AnyPS3Error(f"Unbalanced guest memory helper call: {text}")


MEMORY_CALL = re.compile(r'\b(ppu_load|ppu_store|ppu_read_bytes|ppu_write_bytes)(?:<(\d+)>)?\(state,')


def expand_memory(body, sync, address):
    """Inline fast path; publication only on the out-of-line path (GNU statement expressions)."""
    while True:
        m = MEMORY_CALL.search(body)
        if not m:
            return body
        args, end = call_arguments(body, m.end())
        helper, width = m[1], m[2]
        if helper == 'ppu_load' and len(args) == 1:
            code = (f'({{const std::uint64_t ppu_a=({args[0]});std::uint64_t ppu_v;if(__builtin_expect(!ppu_try_load<{width}>(state,ppu_a,ppu_v),0))'
                    f'{{{sync}ppu_v=ppu_load_slow(state,ppu_a,{width});}}ppu_v;}})')
        elif helper == 'ppu_store' and len(args) == 2:
            code = (f'({{const std::uint64_t ppu_a=({args[0]});const std::uint64_t ppu_s=({args[1]});if(__builtin_expect(!ppu_try_store<{width}>(state,ppu_a,ppu_s),0))'
                    f'{{{sync}ppu_store_slow(state,ppu_a,{width},ppu_s);}}}})')
        elif helper in ('ppu_read_bytes', 'ppu_write_bytes') and len(args) == 3 and not width:
            kind = 'read' if helper == 'ppu_read_bytes' else 'write'
            code = (f'({{const std::uint64_t ppu_a=({args[0]});const unsigned ppu_n=({args[2]});'
                    f'if(__builtin_expect(!ppu_try_{kind}_bytes(state,ppu_a,{args[1]},ppu_n),0)){{{sync}ppu_{kind}_bytes_slow(state,ppu_a,{args[1]},ppu_n);}}}})')
        else:
            raise AnyPS3Error(f"Unexpected guest memory helper form at 0x{address:08x}: {body}")
        body = body[:m.start()] + code + body[end:]


class PageEmitter:
    def __init__(self, options: EmitOptions, heads, by_address, runtime_operation):
        self.options = options
        self.heads = heads
        self.by_address = by_address
        self.runtime_operation = runtime_operation
        self.pages = {}

    def page(self, number):
        return self.pages.setdefault(number, dict(cases=[], body=[]))

    # ---- body rewriting -------------------------------------------------
    def rewrite(self, body: str, address: int) -> str:
        body = body.replace('state.pc', 'pc_v')
        if not self.options.gpr_locals:
            return body
        body = GPR.sub(lambda m: f'gpr{m[1]}', body)
        body = FPR.sub(lambda m: f'fpr{m[1]}', body)
        body = body.replace('state.lr', 'lr_l').replace('state.ctr', 'ctr_l').replace('state.xer', 'xer_l')
        body = COMPARE.sub(lambda m: f'cr_l=ppu_crset(cr_l,{m[1]},ppu_cmp({m[2]},xer_l));', body)
        body = RECORD.sub(lambda m: f'cr_l=ppu_crset(cr_l,0,ppu_cmp({m[1]},0,true,true,xer_l));', body)
        body = BRANCH.sub(lambda m: f'ppu_bt(ctr_l,cr_l,{m[1]},{m[2]})', body)
        body = SHIFT.sub('ppu_sra(xer_l,', body)
        if any(call in body for call in CR_SYNC_CALLS):
            body = f'state.cr=cr_l;{{{body}}}cr_l=state.cr;'
        else:
            body = body.replace('state.cr', 'cr_l')
        for name in STATE_CALL.findall(body):
            if not STATE_SAFE.match(name):
                raise AnyPS3Error(f"PPU local-register emitter cannot prove helper {name} at 0x{address:08x} preserves registers.")
        for forbidden in ('state.r[', 'state.r)', 'state.lr', 'state.ctr', 'state.xer', 'state.fpr'):
            if forbidden in body:
                raise AnyPS3Error(f"PPU local-register emitter left {forbidden} at 0x{address:08x}: {body}")
        if re.search(r'\b(compare|record|branch_test|arithmetic_shift)\(state', body):
            raise AnyPS3Error(f"PPU local-register emitter left a CR/XER helper on state at 0x{address:08x}: {body}")
        if re.search(r'(?<![\w.])r\[', body):
            raise AnyPS3Error(f"PPU local-register emitter left a GPR array access at 0x{address:08x}: {body}")
        if 'state.cr' in body.replace('state.cr=cr_l;', '').replace('cr_l=state.cr;', ''):
            raise AnyPS3Error(f"PPU local-register emitter left CR on state at 0x{address:08x}: {body}")
        return body

    @staticmethod
    def needs_sync(body):
        """True if the instruction may throw/observe state outside the memory helpers' slow path."""
        if 'throw' in body or 'checked_target' in body:
            return True
        return bool(set(CALL.findall(body)) - NOTHROW - set(MEMORY_HELPERS))

    def reg(self, name):
        if not self.options.gpr_locals:
            return {'lr': 'state.lr', 'r1': 'state.r[1]', 'r3': 'state.r[3]'}[name]
        return {'lr': 'lr_l', 'r1': 'gpr1', 'r3': 'gpr3'}[name]

    # ---- page assembly ----------------------------------------------------
    def emit_block(self, block):
        count = len(block)
        first = block[0]
        page = self.page(first.address >> 12)
        page['cases'].append(f"    case 0x{first.address:x}: goto ppu_{first.address:x};")
        limit = self.options.max_steps - count if count <= self.options.max_steps else -1
        code = [f"ppu_{first.address:x}:",
                f"    if(steps_v>=slice_v || state.reschedule){{pc_v=0x{first.address:x};goto out;}}"]
        if self.options.trace:
            code.append(f"    slow=trace_v || steps_v>UINT64_C({limit});" if limit >= 0 else "    slow=true;")
        else:
            code.append(f"    if(__builtin_expect(steps_v>UINT64_C({limit}),0)){{state.pc=0x{first.address:x};state.steps=steps_v;ppu_step_limit();}}"
                        if limit >= 0 else f"    state.pc=0x{first.address:x};state.steps=steps_v;ppu_step_limit();")
        locals_ = {'lr_l', 'gpr1', 'gpr3'} if self.options.trace and self.options.gpr_locals else set()
        out, tail = [], None

        def store(names):
            return ''.join(f'{field(n)}={n};' for n in sorted(names)) if self.options.gpr_locals else ''

        def publish(address, steps):
            return f'state.pc=0x{address:x};state.steps=steps_v+{steps};'
        for index, inst in enumerate(block):
            last = index == count - 1
            runtime_call = inst.word == 0 or inst.mnemonic == "sc"
            lines = []
            if self.options.trace:
                lines.append(f"if(__builtin_expect(slow,0)){{{publish(inst.address, index)}"
                             f"ppu_step_slow(state,0x{inst.address:x},steps_v+{index},{self.reg('lr')},{self.reg('r1')},{self.reg('r3')});}}")
            if runtime_call:
                if not last:
                    raise AnyPS3Error(f"Nonterminal runtime call in PPU block at 0x{inst.address:08x}.")
                call = self.runtime_operation(inst.address, inst.mnemonic, inst.cpp)
                lines.append(f"{publish(inst.address, index + 1)}{call}return;")
                out.append(f"    {{ /* {inst.word:08x} {inst.mnemonic} */ {' '.join(lines)} }}")
                tail = 'runtime'
                break
            body = inst.cpp.replace('if (taken && state.pc == RETURN_SENTINEL) return;',
                                    'if (taken && state.pc == RETURN_SENTINEL) { state.returned=true; STEPS_FINAL STORE_ALL goto out; }')
            body = self.rewrite(body, inst.address)
            locals_ |= set(LOCAL.findall(body))
            sync = publish(inst.address, index + 1)
            if self.needs_sync(body):
                lines.append(sync)
            body = expand_memory(body, sync, inst.address)
            written = store(written_locals(body))
            lines.append(body.replace('STORE_ALL', written) + written)
            out.append(f"    {{ /* {inst.word:08x} {inst.mnemonic} */ {' '.join(lines)} }}")
            primary, xo = inst.word >> 26, (inst.word >> 1) & 1023
            indirect = primary == 19 and xo in (16, 528)
            if not last and (indirect or primary in TRANSFER_PRIMARY):
                raise AnyPS3Error(f"Nonterminal transfer in PPU block at 0x{inst.address:08x}.")
            if last:
                tail = inst
        text = '\n'.join(out).replace('STEPS_FINAL', f'steps_v+={count};')
        decl = ' '.join(declaration(n) for n in sorted(locals_)) if self.options.gpr_locals else ''
        code.append(f"    {{ {decl}")
        code.append(text)
        if tail == 'runtime':
            code.append("    }")
            page['body'].extend(code)
            return
        code.append("    }")
        code.append(f"    steps_v+={count};")
        inst = tail
        primary, xo = inst.word >> 26, (inst.word >> 1) & 1023
        indirect = primary == 19 and xo in (16, 528)

        def transfer(target):
            if target in self.heads and target >> 12 == inst.address >> 12:
                return f"goto ppu_{target:x};"
            return f"@@XFER_{target:x}@@"
        if indirect:
            code.append("    {state.steps=steps_v;state.pc=pc_v;[[gnu::musttail]] return ppu_dispatch(state);}")
        elif primary == 18:
            code.append("    " + transfer(inst.successors[0]))
        elif primary == 16:
            target, fallthrough = inst.successors
            code.extend([f"    if(pc_v==0x{target:x}){transfer(target)}", "    " + transfer(fallthrough)])
        elif not inst.successors:
            code.append("    goto out;")
        else:
            code.append("    " + transfer((inst.address + 4) & 0xffffffff))
        page['body'].extend(code)

    def emit_trap(self, address, reason):
        page = self.page(address >> 12)
        page['cases'].append(f"    case 0x{address:x}: goto ppu_{address:x};")
        page['body'].append(
            f"ppu_{address:x}: state.pc=0x{address:x};state.steps=steps_v;"
            f"if(steps_v>=UINT64_C({self.options.max_steps}))throw std::runtime_error(\"execution step limit reached\");"
            f"state.steps=steps_v+1;throw std::runtime_error({json.dumps(reason)});")

    def emit_lifecycle(self, address, call):
        self.page(address >> 12)['cases'].append(f"    case 0x{address:x}: {call} return;")

    def functions(self):
        """(page, C++ text) for every page.

        A static transfer to another translated page tail-calls it directly
        (no dispatcher iteration); indirect transfers tail-call ppu_dispatch.
        The slice check at every block head still returns the whole chain to
        anyps3_entry, so scheduling, engines and exceptions are unchanged.
        """
        result = []
        def xfer(m):
            target = int(m[1], 16)
            if target >> 12 in self.pages:
                return f"{{state.steps=steps_v;state.pc=0x{target:x};[[gnu::musttail]] return page_{target >> 12:x}(state);}}"
            return f"{{pc_v=0x{target:x};goto out;}}"
        for number, code in sorted(self.pages.items()):
            code['body'] = [re.sub(r'@@XFER_([0-9a-f]+)@@', xfer, line) for line in code['body']]
            missing = (f"    default: state.steps=steps_v;"
                       f"if(steps_v>=UINT64_C({self.options.max_steps}))throw std::runtime_error(\"execution step limit reached\");"
                       f"state.steps=steps_v+1;throw std::runtime_error(\"target has no statically translated block\");")
            result.append((number, '\n'.join([
                f'void page_{number:x}(PpuState& state) {{',
                '    [[maybe_unused]] auto& r=state.r;',
                '    std::uint64_t steps_v=state.steps;[[maybe_unused]] const std::uint64_t slice_v=state.ppu_slice_end;[[maybe_unused]] const bool trace_v=state.ppu_trace;',
                '    std::uint32_t pc_v=state.pc;[[maybe_unused]] bool slow=false;',
                '    switch (pc_v) {', *code['cases'], missing, '    }',
                *code['body'],
                '    out: __attribute__((unused)); state.steps=steps_v;state.pc=pc_v;return;',
                '}'])))
        return result
