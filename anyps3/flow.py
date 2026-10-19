"""Static PPU control flow and integer operations used by the process entry.

All instructions become C++ before execution. Unknown operations raise;
there is no runtime PowerPC decoder or interpreter fallback.
"""

from dataclasses import dataclass
import re

from .errors import AnyPS3Error
from .ppu import decode
from .vector import lift_vector


@dataclass(frozen=True)
class Operation:
    address: int
    word: int
    mnemonic: str
    cpp: str
    successors: tuple[int, ...]

    def batch_body(self) -> str | None:
        """Pure register operations may retire together; unknown helpers may not.

        Keep this an explicit whitelist. In particular memory, FP arithmetic,
        timebase, traps and control transfers must publish their own PC/steps
        before calling anything that can observe the context or throw.
        """
        if self.mnemonic not in {
                'addi', 'addis', 'ori', 'oris', 'xori', 'xoris', 'add', 'subf',
                'and', 'xor', 'or', 'logic', 'arithmetic.', 'carry_arithmetic',
                'mulli', 'mulld', 'mullw', 'mulhw', 'mulhwu', 'mulhd', 'mulhdu',
                'divdu', 'divwu', 'divd', 'divw', 'neg', 'cmp', 'cmpl',
                'rldicl', 'rldicr', 'rldic', 'rldimi', 'rlwimi', 'rlwinm', 'rlwnm',
                'logical_shift', 'arithmetic_shift', 'cntlzd', 'cntlzw',
                'extsh', 'extsb', 'extsw', 'andi.', 'andis.', 'mfcr', 'mfocrf',
                'mtcrf', 'mcrf', 'crlogic', 'mfxer', 'mtxer', 'mflr', 'mtlr',
                'mfctr', 'mtctr', 'mfvrsave', 'mtvrsave',
                'fneg', 'fmr', 'fnabs', 'fabs', 'fsel', 'vector_logic',
                'cache prefetch hint'}:
            return None
        # All listed lifter paths end in the same immediate fallthrough store.
        # Match it exactly, so a future change cannot silently discard effects.
        following = (self.address + 4) & 0xffffffff
        for suffix in (f' state.pc = 0x{following:x};', f' state.pc=0x{following:x};'):
            if self.cpp.endswith(suffix):
                body = self.cpp[:-len(suffix)]
                calls = set(re.findall(r'\b([A-Za-z_]\w*)\s*\(', body))
                if calls - {'UINT64_C', 'UINT32_C', 'INT64_C', 'Wide', 'for', 'if',
                            'rotl64', 'rotl32', 'compare', 'record', 'sign_word',
                            'signed_word', 'arithmetic_shift', 'leading_zeroes',
                            'divide_integer'}:
                    return None
                if any(name in body for name in ('state.pc', 'state.steps', 'throw', 'return')):
                    return None
                return body
        return None


def sx(value: int, bits: int) -> int:
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def literal(value: int) -> str:
    return f"UINT64_C(0x{value & ((1 << 64) - 1):x})"


def mask(begin: int, end: int, bits: int) -> int:
    return sum(1 << (bits - 1 - i) for i in range(bits)
               if (begin <= i <= end if begin <= end else i >= begin or i <= end))


def signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def translated_memory(statement: str) -> str:
    """D-form loads/stores from ppu.decode use the inline guest view helpers."""
    for size in (1, 2, 4, 8):
        statement = statement.replace(f"state.memory.load(ea, {size})", f"ppu_load<{size}>(state, ea)")
        statement = re.sub(rf"state\.memory\.store\(ea, {size}, (r\[\d+\])\);", rf"ppu_store<{size}>(state, ea, \1);", statement)
    if 'state.memory.' in statement:
        raise AnyPS3Error(f"Untranslated guest memory form: {statement}")
    return statement


def branch_test(bo: int, bi: int) -> str:
    return (f"branch_test(state, {bo}, {bi})")


def lift(word: int, pc: int) -> Operation:
    op, rt, ra, rb = word >> 26, (word >> 21) & 31, (word >> 16) & 31, (word >> 11) & 31
    xo = (word >> 1) & 1023
    next_pc = (pc + 4) & 0xffffffff
    successors = (next_pc,)
    mnemonic, body = "", ""
    vector=lift_vector(word)
    if vector:
        mnemonic,body=vector
        return Operation(pc,word,mnemonic,body+f' state.pc=0x{next_pc:x};',successors)
    if op==31 and xo in (20,84,150,214):
        store=xo in (150,214);width=8 if xo in (84,214) else 4
        if bool(word&1)!=store:
            raise AnyPS3Error(f"Invalid reservation instruction Rc bit at 0x{pc:08x}.")
        base=f"r[{ra}]" if ra else "UINT64_C(0)"
        body=f'const auto ea={base}+r[{rb}];if(ea%{width})throw std::runtime_error("unaligned PPU reservation access");'
        if store:
            body+=f'const bool success=state.reservation_address==ea && state.reservation_size=={width} && ppu_store_conditional(state,ea,{width},r[{rt}]);'
            body+=f'state.reservation_address=UINT64_MAX;state.cr=(state.cr&0x0fffffff)|((success?2u:0u)|((state.xer>>31)&1))<<28;'
        else:
            body+=f'const auto value=ppu_load_reserved(state,ea,{width});state.reservation_address=ea;state.reservation_size={width};r[{rt}]=value;'
        return Operation(pc,word,('stdcx.' if width==8 else 'stwcx.') if store else ('ldarx' if width==8 else 'lwarx'),body+f' state.pc=0x{next_pc:x};',successors)
    vector_xo=word & 2047
    if op==4 and vector_xo in (1028,1092,1156,1220,1284):
        a,b=f"state.vr[{ra}][i]",f"state.vr[{rb}][i]"
        expression={1028:f"{a} & {b}",1092:f"{a} & ~{b}",1156:f"{a} | {b}",1220:f"{a} ^ {b}",1284:f"~({a} | {b})"}[vector_xo]
        body=f"for (unsigned i=0;i<16;++i) {{ state.vr[{rt}][i] = static_cast<std::uint8_t>({expression}); }}"
        return Operation(pc,word,'vector_logic',body+f" state.pc = 0x{next_pc:x};",successors)
    if op==31 and xo in (103,359,231,487) and not word & 1:
        base=f"r[{ra}]" if ra else "UINT64_C(0)"
        store=xo in (231,487)
        body=f"const auto ea = ({base} + r[{rb}]) & ~UINT64_C(15); "
        body+=(f"ppu_write_bytes(state,ea,state.vr[{rt}].data(),16);" if store else f"ppu_read_bytes(state,ea,state.vr[{rt}].data(),16);")
        return Operation(pc,word,'stvx' if store else 'lvx',body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 31 and xo in (597, 725) and not word & 1:
        # lswi/stswi (PowerPC Book I 3.3.11): NB=0 means 32 bytes, registers wrap r31->r0;
        # loads fill the low word high-to-low and clear the upper word (64-bit mode).
        store = xo == 725; count = rb or 32; regs = [(rt + i) % 32 for i in range((count + 3) // 4)]
        if not store and ra in regs and ra:
            raise AnyPS3Error(f"Invalid lswi form at 0x{pc:08x}: RA is loaded.")
        base = f"r[{ra}]" if ra else "UINT64_C(0)"
        body = f"const auto ea = {base}; "
        for i in range(count):
            reg, shift = regs[i // 4], 24 - 8 * (i % 4)
            if store:
                body += f"ppu_store<1>(state, ea + {i}, (r[{reg}] >> {shift}) & 0xff); "
            else:
                body += f"const auto v{i} = ppu_load<1>(state, ea + {i}); "
        if not store:
            # All bytes are read before any register changes (EA register is never loaded).
            for k, reg in enumerate(regs):
                parts = [f"(v{4*k+j} << {24-8*j})" for j in range(4) if 4*k+j < count]
                body += f"r[{reg}] = {'|'.join(parts)}; "
        return Operation(pc, word, 'stswi' if store else 'lswi', body + f"state.pc = 0x{next_pc:x};", successors)
    if (op == 31 and xo in (4, 68) and not word & 1) or op in (2, 3):
        # tw/td/twi/tdi: TO bits lt,gt,eq,ltu,gtu (Book I 3.3.10). A satisfied
        # condition raises the trap interrupt; this runtime stops explicitly.
        wide = (op == 31 and xo == 68) or op == 2
        bits = 64 if wide else 32
        a = f"static_cast<std::uint{bits}_t>(r[{ra}])"
        b = (f"static_cast<std::uint{bits}_t>(r[{rb}])" if op == 31 else
             f"static_cast<std::uint{bits}_t>(INT{bits}_C({signed16(word & 0xffff)}))")
        sa, sb = f"static_cast<std::int{bits}_t>({a})", f"static_cast<std::int{bits}_t>({b})"
        tests = [(16, f"{sa}<{sb}"), (8, f"{sa}>{sb}"), (4, f"{a}=={b}"), (2, f"{a}<{b}"), (1, f"{a}>{b}")]
        condition = "||".join(f"({t})" for bit, t in tests if rt & bit) or "false"
        name = {(31, 4): 'tw', (31, 68): 'td', (3, None): 'twi', (2, None): 'tdi'}[(op, xo if op == 31 else None)]
        body = f'if ({condition}) {{ throw std::runtime_error("PPU trap instruction taken ({name} TO={rt})"); }} state.pc = 0x{next_pc:x};'
        # TO=31 always traps: nothing follows it statically.
        return Operation(pc, word, name, body, () if rt == 31 else successors)
    reversed_ = {534:('lwbrx',4,False),790:('lhbrx',2,False),662:('stwbrx',4,True),918:('sthbrx',2,True)}
    if op == 31 and not word & 1 and xo in reversed_:
        mnemonic,width,store=reversed_[xo]
        base=f"r[{ra}]" if ra else "UINT64_C(0)"
        swap=f"__builtin_bswap{width*8}"
        cast=f"std::uint{width*8}_t"
        body=f"const auto ea = {base} + r[{rb}]; "
        body+=(f"ppu_store<{width}>(state, ea, {swap}(static_cast<{cast}>(r[{rt}])));" if store else
               f"r[{rt}] = {swap}(static_cast<{cast}>(ppu_load<{width}>(state, ea)));")
        return Operation(pc,word,mnemonic,body+f" state.pc = 0x{next_pc:x};",successors)
    indexed = {21:('ldx',8,False,False),23:('lwzx',4,False,False),87:('lbzx',1,False,False),
               279:('lhzx',2,False,False),343:('lhax',2,False,True),341:('lwax',4,False,True),
               149:('stdx',8,True,False),151:('stwx',4,True,False),215:('stbx',1,True,False),407:('sthx',2,True,False)}
    if op == 31 and not word & 1 and (xo & ~32) in indexed:
        mnemonic,width,store,extend=indexed[xo & ~32]
        update=bool(xo & 32)
        if update and (ra==0 or (not store and ra==rt)):
            raise AnyPS3Error(f"Invalid indexed update operands at 0x{pc:08x}.")
        base=f"r[{ra}]" if ra else "UINT64_C(0)"
        body=f"const auto ea = {base} + r[{rb}]; "
        if store: body+=f"ppu_store<{width}>(state, ea, r[{rt}]);"
        else:
            body+=f"const auto value = ppu_load<{width}>(state, ea); "
            sign=1 << (width*8-1)
            body+=f"r[{rt}] = " + (f"(value ^ {literal(sign)}) - {literal(sign)};" if extend else "value;")
        if update: body+=f" r[{ra}] = ea;"
        return Operation(pc,word,mnemonic,body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 31 and xo in (54,246,278) and rt==0 and not word & 1:
        # dcbst only writes a block back to coherent memory; host memory is the memory.
        return Operation(pc,word,'cache prefetch hint',f"/* Architectural hint: no functional effect. */ state.pc = 0x{next_pc:x};",successors)
    if word in (0x7C0004AC,0x7C2004AC,0x4C00012C,0x7C0006AC):
        return Operation(pc,word,'synchronization',f"std::atomic_thread_fence(std::memory_order_seq_cst); state.pc = 0x{next_pc:x};",successors)
    if op==31 and xo==1014 and rt==0 and not word & 1:
        base=f"r[{ra}]" if ra else "UINT64_C(0)"
        body=f"const auto ea = ({base} + r[{rb}]) & ~UINT64_C(127); const std::array<std::uint8_t,128> zero{{}}; ppu_write_bytes(state,ea,zero.data(),128);"
        return Operation(pc,word,'dcbz',body+f" state.pc = 0x{next_pc:x};",successors)
    if op in (48, 49, 50, 51, 52, 53, 54, 55):
        update, store = bool(op & 1), op >= 52
        if update and ra == 0:
            raise AnyPS3Error(f"Invalid floating-point update address at 0x{pc:08x}.")
        base = f"r[{ra}]" if ra else "UINT64_C(0)"
        body = f"const auto ea = {base} + {literal(sx(word & 0xffff, 16))}; "
        body += (f"ppu_store<4>(state, ea, fp_store_single(state.fpr[{rt}]));" if op in (52,53) else
                 f"ppu_store<8>(state, ea, state.fpr[{rt}]);" if store else
                 f"state.fpr[{rt}] = fp_widen(static_cast<std::uint32_t>(ppu_load<4>(state, ea)));" if op in (48,49) else
                 f"state.fpr[{rt}] = ppu_load<8>(state, ea);")
        if update:
            body += f" r[{ra}] = ea;"
        mnemonic = {48:'lfs',50:'lfd',52:'stfs',54:'stfd'}[op & ~1] + ('u' if update else '')
        return Operation(pc, word, mnemonic, body + f" state.pc = 0x{next_pc:x};", successors)
    fp_indexed = {535:'lfsx',599:'lfdx',663:'stfsx',727:'stfdx'}
    if op == 31 and not word & 1 and (xo & ~32) in fp_indexed:
        update, kind = bool(xo & 32), xo & ~32
        if update and ra == 0:
            raise AnyPS3Error(f"Invalid floating-point update address at 0x{pc:08x}.")
        base = f"r[{ra}]" if ra else "UINT64_C(0)"
        body = f"const auto ea = {base} + r[{rb}]; "
        body += {663: f"ppu_store<4>(state, ea, fp_store_single(state.fpr[{rt}]));",
                 727: f"ppu_store<8>(state, ea, state.fpr[{rt}]);",
                 535: f"state.fpr[{rt}] = fp_widen(static_cast<std::uint32_t>(ppu_load<4>(state, ea)));",
                 599: f"state.fpr[{rt}] = ppu_load<8>(state, ea);"}[kind]
        if update:
            body += f" r[{ra}] = ea;"
        mnemonic = fp_indexed[kind][:-1] + ('ux' if update else 'x')
        return Operation(pc, word, mnemonic, body + f" state.pc = 0x{next_pc:x};", successors)
    if op == 31 and xo == 983 and not word & 1:
        base=f"r[{ra}]" if ra else "UINT64_C(0)"
        return Operation(pc,word,'stfiwx',f"ppu_store<4>(state, {base} + r[{rb}], state.fpr[{rt}]); state.pc = 0x{next_pc:x};",successors)
    if op == 63 and xo in (40, 72, 136, 264) and ra == 0:
        body = f"state.fpr[{rt}] = state.fpr[{rb}]"
        body += {40: " ^ UINT64_C(0x8000000000000000);", 72: ";",
                 136: " | UINT64_C(0x8000000000000000);", 264: " & UINT64_C(0x7fffffffffffffff);"}[xo]
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc, word, {40:"fneg",72:"fmr",136:"fnabs",264:"fabs"}[xo],
                         body + f" state.pc = 0x{next_pc:x};", successors)
    if op == 63 and xo == 711 and not (word >> 25) & 1 and not (word >> 16) & 1:
        # mtfsf FLM,frB: field i follows FLM bit 7-i; FEX and VX stay summaries.
        field_mask = 0
        for field in range(8):
            if (word >> (24 - field)) & 1:
                field_mask |= 0xf << (28 - 4 * field)
        body = f"fp_write_fields(state, static_cast<std::uint32_t>(state.fpr[{rb}]), UINT32_C(0x{field_mask:08x}));"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc, word, 'mtfsf', body + f" state.pc = 0x{next_pc:x};", successors)
    if op == 63 and xo in (38,70) and not ra and not rb:
        body = f"fp_write_bit(state, {rt}, {'true' if xo==38 else 'false'});"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,'mtfsb1' if xo==38 else 'mtfsb0',body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 63 and xo == 134 and not (rt & 3) and not ra and not (rb & 1):
        field, immediate=rt>>2, rb>>1
        body=f"fp_write_fields(state, UINT32_C(0x{immediate << (28-4*field):08x}), UINT32_C(0x{15 << (28-4*field):08x}));"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,'mtfsfi',body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 63 and xo == 64 and not (word & 1) and not (rt & 3) and not (ra & 3) and not rb:
        body=f"fp_move_field(state, {rt>>2}, {ra>>2});"
        return Operation(pc,word,'mcrfs',body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 63 and xo in (846, 814, 815, 583, 14, 15) and not ra:
        if xo == 583 and rb:
            raise AnyPS3Error(f"Reserved mffs operand at 0x{pc:08x}.")
        name = {846:'fcfid',814:'fctid',815:'fctidz',583:'mffs',14:'fctiw',15:'fctiwz'}[xo]
        expression = (f"fp_from_integer(state, state.fpr[{rb}])" if xo == 846 else
                      f"fp_to_integer(state, state.fpr[{rb}], {32 if xo in (14,15) else 64}, {'true' if xo in (15,815) else 'false'})" if xo in (14,15,814,815) else "state.fpscr")
        body = f"state.fpr[{rt}] = {expression};"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,name,body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 63 and (xo & 31) == 23:
        rc = (word >> 6) & 31
        body = f"const auto condition = state.fpr[{ra}]; const auto magnitude = condition & UINT64_C(0x7fffffffffffffff); "
        body += f"state.fpr[{rt}] = (magnitude <= UINT64_C(0x7ff0000000000000) && (!(condition >> 63) || !magnitude)) ? state.fpr[{rc}] : state.fpr[{rb}];"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,'fsel',body+f" state.pc = 0x{next_pc:x};",successors)
    if (op, xo & 31) in ((59, 24), (63, 26)):
        if ra or (word >> 6) & 31:
            raise AnyPS3Error(f"Reserved floating estimate operand at 0x{pc:08x}.")
        body = f"state.fpr[{rt}] = fp_estimate(state, state.fpr[{rb}], {'true' if op == 63 else 'false'});"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,'frsqrte' if op == 63 else 'fres',body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 63 and xo == 12 and not ra:
        body = (f"fp_mode(state); std::uint64_t special; if (fp_round_single_special(state, state.fpr[{rb}], special)) state.fpr[{rt}] = special; "
                f"else {{ const auto value = fp_parts(state.fpr[{rb}]); state.fpr[{rt}] = fp_pack(state, value.sign, value.significand, value.exponent - 52, 24); }}")
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,'frsp',body+f" state.pc = 0x{next_pc:x};",successors)
    if op in (59, 63) and (xo & 31) == 22:
        if ra or (word >> 6) & 31:
            raise AnyPS3Error(f"Reserved fsqrt operand at 0x{pc:08x}.")
        body = f"state.fpr[{rt}] = fp_sqrt(state, state.fpr[{rb}], {24 if op == 59 else 53});"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,'fsqrts' if op == 59 else 'fsqrt',body+f" state.pc = 0x{next_pc:x};",successors)
    if op in (59,63) and (xo & 31) in (28,29,30,31):
        variant,rc=xo&31,(word>>6)&31
        addend=f"state.fpr[{rb}]"
        if variant in (28,30):addend=f"fp_subtract_operand({addend})"
        # Single-precision forms use the full binary64 operands and round
        # once to binary32 (implementation behaviour; Book I leaves
        # non-single-representable inputs undefined).
        body=''
        body+=f"state.fpr[{rt}]=fp_fused(state,state.fpr[{ra}],state.fpr[{rc}],{addend},{24 if op==59 else 53},{'true' if variant>=30 else 'false'});"
        if word&1:body+=" state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,{28:'fmsub',29:'fmadd',30:'fnmsub',31:'fnmadd'}[variant]+('s' if op==59 else ''),body+f" state.pc = 0x{next_pc:x};",successors)
    if op in (59,63) and (xo & 31) in (18, 20, 21, 25):
        variant, rc = xo & 31, (word >> 6) & 31
        if (variant == 25 and rb) or (variant != 25 and rc):
            raise AnyPS3Error(f"Reserved floating arithmetic operand at 0x{pc:08x}.")
        right = f"state.fpr[{rc if variant == 25 else rb}]"
        if variant == 20:
            right = f"fp_subtract_operand({right})"
        helper = {18:'fp_divide',20:'fp_add',21:'fp_add',25:'fp_multiply'}[variant]
        body = ''
        body += f"state.fpr[{rt}] = {helper}(state, state.fpr[{ra}], {right}, {24 if op==59 else 53});"
        if word & 1:
            body += " state.cr = (state.cr & ~UINT32_C(0x0f000000)) | ((state.fpscr >> 4) & 0x0f000000);"
        return Operation(pc,word,{18:'fdiv',20:'fsub',21:'fadd',25:'fmul'}[variant],body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 63 and xo in (0, 32) and not word & 1:
        if rt & 3:
            raise AnyPS3Error(f"Reserved floating comparison operand at 0x{pc:08x}.")
        return Operation(pc,word,'fcmpo' if xo else 'fcmpu',f"fp_compare(state, {rt >> 2}, state.fpr[{ra}], state.fpr[{rb}], {'true' if xo else 'false'}); state.pc = 0x{next_pc:x};",successors)
    if op==19 and xo==0:
        if rt&3 or ra&3 or rb or word&1:
            raise AnyPS3Error(f"Reserved mcrf operand at 0x{pc:08x}.")
        target_shift,source_shift=28-rt,28-ra
        body=f'const auto field=(state.cr>>{source_shift})&15u;state.cr=(state.cr&~UINT32_C(0x{15<<target_shift:08x}))|(field<<{target_shift});'
        return Operation(pc,word,'mcrf',body+f' state.pc=0x{next_pc:x};',successors)
    if op == 19 and xo in (33,129,193,225,257,289,417,449) and not word & 1:
        a, b = f"((state.cr >> {31-ra}) & 1)", f"((state.cr >> {31-rb}) & 1)"
        expression = {33:f"~({a} | {b})",129:f"({a} & ~{b})",193:f"({a} ^ {b})",225:f"~({a} & {b})",
                      257:f"({a} & {b})",289:f"~({a} ^ {b})",417:f"({a} | ~{b})",449:f"({a} | {b})"}[xo]
        body = f"state.cr = (state.cr & ~(UINT32_C(1) << {31-rt})) | (({expression} & 1) << {31-rt});"
        return Operation(pc,word,'crlogic',body+f" state.pc = 0x{next_pc:x};",successors)
    if op == 18:
        target = (sx(word & 0x3fffffc, 26) + (0 if word & 2 else pc)) & 0xffffffff
        link = bool(word & 1)
        body = (f"state.lr = 0x{next_pc:x}; " if link else "") + f"state.pc = 0x{target:x};"
        return Operation(pc, word, "bl" if link else "b", body,
                         (target, next_pc) if link else (target,))
    if op == 16:
        target = (sx(word & 0xfffc, 16) + (0 if word & 2 else pc)) & 0xffffffff
        bo, bi = rt, ra
        body = (f"state.lr = 0x{next_pc:x}; " if word & 1 else "")
        body += f"state.pc = {branch_test(bo, bi)} ? 0x{target:x} : 0x{next_pc:x};"
        return Operation(pc, word, "bc", body, (target, next_pc))
    if op == 19 and xo in (16, 528):
        # BH is a prediction hint. Other reserved bits must remain zero.
        if word & 0xE000 or (xo == 528 and not rt & 4):
            raise AnyPS3Error(f"Invalid branch-register form at 0x{pc:08x}.")
        target = "state.lr" if xo == 16 else "state.ctr"
        body = f"const auto target = {target} & ~UINT64_C(3); "
        body += f"const bool taken = {branch_test(rt, ra)}; "
        if word & 1:
            body += f"state.lr = 0x{next_pc:x}; "
        body += f"state.pc = taken ? checked_target(target) : 0x{next_pc:x}; "
        if xo == 16:
            body += "if (taken && state.pc == RETURN_SENTINEL) return;"
        # Conditional/link forms have a return/fallthrough continuation.
        successors = (next_pc,) if word & 1 or (rt & 20) != 20 else ()
        return Operation(pc, word, "bclr" if xo == 16 else "bcctr", body, successors)
    if op == 31 and xo == 19 and word & (1<<20) and not word & 0x801:
        fields=(word>>12)&255
        if not fields or fields&(fields-1):
            raise AnyPS3Error(f"Invalid mfocrf field mask at 0x{pc:08x}.")
        field_mask=15<<((fields.bit_length()-1)*4)
        body=f'r[{rt}]=state.cr&0x{field_mask:08x};';mnemonic='mfocrf'
    elif op == 31 and xo == 19 and not word & 0x1ff801:
        body, mnemonic = f"r[{rt}] = state.cr;", "mfcr"
    elif op == 31 and xo == 144 and not word & 0x801:
        fields = (word >> 12) & 255
        if word & 0x100000 and (not fields or fields & (fields - 1)):
            raise AnyPS3Error(f"mtocrf requires one field at 0x{pc:08x}.")
        selected = sum(15 << (n * 4) for n in range(8) if fields & (1 << n))
        body = f"state.cr = (state.cr & ~UINT32_C(0x{selected:x})) | (r[{rt}] & UINT32_C(0x{selected:x}));"
        mnemonic = "mtcrf"
    elif op == 31 and xo == 371 and not word & 1:
        tbr = ra | (rb << 5)
        if tbr not in (268, 269):
            raise AnyPS3Error(f"Unsupported timebase register {tbr} at 0x{pc:08x}.")
        body = f"r[{rt}] = timebase_ticks()" + (" >> 32;" if tbr == 269 else ";")
        mnemonic = "mftb"
    elif op == 31 and xo in (339, 467) and not word & 1:
        spr = ra | (rb << 5)
        if spr not in (1, 8, 9, 256):
            raise AnyPS3Error(f"Unsupported SPR {spr} at 0x{pc:08x}.")
        name = {1: "xer", 8: "lr", 9: "ctr", 256: "vrsave"}[spr]
        body = f"r[{rt}] = state.{name};" if xo == 339 else f"state.{name} = r[{rt}];"
        mnemonic = ("mf" if xo == 339 else "mt") + name
    elif op in (8,12,13) or (op == 31 and xo in (8,10,136,138,200,202,232,234)):
        carry="((state.xer >> 29) & 1)"
        if op in (8,12,13):
            a=f"~r[{ra}]" if op==8 else f"r[{ra}]"
            expression=f"Wide({a}) + {literal(sx(word & 0xffff,16))}" + (" + 1" if op==8 else "")
        else:
            if xo in (200,202,232,234) and rb:
                raise AnyPS3Error(f"Reserved carry arithmetic operand at 0x{pc:08x}.")
            a=f"~r[{ra}]" if xo in (8,136,200,232) else f"r[{ra}]"
            b=f"r[{rb}]" if xo in (8,10,136,138) else "UINT64_MAX" if xo in (232,234) else "0"
            c="1" if xo==8 else "0" if xo==10 else carry
            expression=f"Wide({a}) + {b} + {c}"
        body=f"const Wide total = {expression}; r[{rt}] = static_cast<std::uint64_t>(total); "
        body+="state.xer = (state.xer & ~UINT64_C(0x20000000)) | (static_cast<std::uint64_t>(total >> 64) << 29);"
        if op==13 or (op==31 and word & 1): body+=f" record(state, r[{rt}]);"
        mnemonic='carry_arithmetic'
    elif op == 7:
        body, mnemonic = f"r[{rt}] = r[{ra}] * {literal(sx(word & 0xffff,16))};", "mulli"
    elif op == 31 and (xo in (792,794,824) or ((word >> 2) & 511) == 413):
        wide = xo == 794 or ((word >> 2) & 511) == 413
        immediate = xo == 824 or ((word >> 2) & 511) == 413
        amount = str(rb | ((word & 2) << 4) if wide else rb) if immediate else f"(r[{rb}] & {127 if wide else 63})"
        body = f"r[{ra}] = arithmetic_shift(state, r[{rt}], {amount}, {64 if wide else 32});"
        if word & 1:
            body += f" record(state, r[{ra}]);"
        mnemonic = "arithmetic_shift"
    elif op == 31 and xo in (24,27,536,539):
        wide, left = xo in (27,539), xo in (24,27)
        width = 64 if wide else 32
        body = f"const unsigned amount = r[{rb}] & {width*2-1}; "
        source = f"r[{rt}]" if wide else f"(r[{rt}] & UINT32_MAX)"
        body += f"r[{ra}] = amount >= {width} ? 0 : ({source} {'<<' if left else '>>'} amount)"
        body += ";" if wide else " & UINT32_MAX;"
        if word & 1:
            body += f" record(state, r[{ra}]);"
        mnemonic = "logical_shift"
    elif op == 31 and xo in (26,58) and rb == 0:
        body = f"r[{ra}] = leading_zeroes(r[{rt}], {64 if xo == 58 else 32});"
        if word & 1:
            body += f" record(state, r[{ra}]);"
        mnemonic = "cntlzd" if xo == 58 else "cntlzw"
    elif op == 31 and xo in (457,459,489,491):
        body=f"r[{rt}] = divide_integer(r[{ra}], r[{rb}], {'true' if xo in (457,489) else 'false'}, {'true' if xo in (489,491) else 'false'});"
        if word & 1: body+=f" record(state, r[{rt}]);"
        mnemonic={457:'divdu',459:'divwu',489:'divd',491:'divw'}[xo]
    elif op == 31 and xo in (9,73):
        body=f"r[{rt}] = static_cast<std::uint64_t>((Wide(r[{ra}]) * r[{rb}]) >> 64)"
        if xo==73:
            body+=f" - ((r[{ra}] >> 63) ? r[{rb}] : 0) - ((r[{rb}] >> 63) ? r[{ra}] : 0)"
        body+=';'
        if word & 1: body+=f" record(state, r[{rt}]);"
        mnemonic='mulhd' if xo==73 else 'mulhdu'
    elif op == 31 and xo in (104,233,235,75,11):
        if xo == 104 and rb:
            raise AnyPS3Error(f"Reserved neg operand at 0x{pc:08x}.")
        expression = {104:f"UINT64_C(0) - r[{ra}]",233:f"r[{ra}] * r[{rb}]",
                      235:f"sign_word(r[{ra}] * r[{rb}])",
                      75:f"sign_word(static_cast<std::uint64_t>(signed_word(r[{ra}]) * signed_word(r[{rb}])) >> 32)",
                      11:f"((r[{ra}] & UINT32_MAX) * (r[{rb}] & UINT32_MAX)) >> 32"}[xo]
        body = f"r[{rt}] = {expression};"
        if word & 1:
            body += f" record(state, r[{rt}]);"
        mnemonic = {104:'neg',233:'mulld',235:'mullw',75:'mulhw',11:'mulhwu'}[xo]
    elif op == 31 and xo in (28,60,124,284,316,412,444,476):
        a,b=f"r[{rt}]",f"r[{rb}]"
        expression={28:f"{a} & {b}",60:f"{a} & ~{b}",124:f"~({a} | {b})",284:f"~({a} ^ {b})",
                    316:f"{a} ^ {b}",412:f"{a} | ~{b}",444:f"{a} | {b}",476:f"~({a} & {b})"}[xo]
        body=f"r[{ra}] = {expression};"
        if word & 1: body += f" record(state, r[{ra}]);"
        mnemonic='logic'
    elif op == 31 and xo in (40,266) and word & 1:
        body=decode(word & ~1,pc).statement+f" record(state, r[{rt}]);"
        mnemonic='arithmetic.'
    elif op in (10, 11) or (op == 31 and xo in (0, 32) and not word & 1):
        if word & (1 << 22):
            raise AnyPS3Error(f"Reserved comparison bit at 0x{pc:08x}.")
        wide = bool(word & (1 << 21))
        signed = op == 11 or (op == 31 and xo == 0)
        right = f"r[{rb}]" if op == 31 else literal(sx(word & 0xffff, 16) if signed else word & 0xffff)
        body = f"compare(state, {(word >> 23) & 7}, r[{ra}], {right}, {str(wide).lower()}, {str(signed).lower()});"
        mnemonic = "cmp" if signed else "cmpl"
    elif op == 30 and ((word >> 2) & 7) in (0, 1, 2, 3):
        variant = (word >> 2) & 7
        shift = rb | ((word & 2) << 4)
        boundary = ((word >> 6) & 31) | (word & 32)
        begin, end = ((boundary, 63) if variant == 0 else (0, boundary) if variant == 1 else (boundary, 63 - shift))
        m = mask(begin, end, 64)
        value = f"(rotl64(r[{rt}], {shift}) & {literal(m)})"
        if variant == 3:
            value = f"(r[{ra}] & {literal(~m)}) | {value}"
        body = f"r[{ra}] = {value};"
        if word & 1:
            body += f" record(state, r[{ra}]);"
        mnemonic = ("rldicl", "rldicr", "rldic", "rldimi")[variant]
    elif op in (20, 21, 23):
        m = mask((word >> 6) & 31, (word >> 1) & 31, 32)
        shift = f"(r[{rb}] & 31)" if op == 23 else str(rb)
        value = f"(rotl32(static_cast<std::uint32_t>(r[{rt}]), {shift}) & {literal(m)})"
        if op == 20:
            value = f"(r[{ra}] & {literal(~m)}) | {value}"
        body = f"r[{ra}] = {value};"
        if word & 1:
            body += f" record(state, r[{ra}]);"
        mnemonic = {20: "rlwimi", 21: "rlwinm", 23: "rlwnm"}[op]
    elif op == 31 and xo in (922, 954, 986):
        bits = {922: 16, 954: 8, 986: 32}[xo]
        sign = 1 << (bits - 1)
        body = f"r[{ra}] = ((r[{rt}] & {literal((1 << bits) - 1)}) ^ {literal(sign)}) - {literal(sign)};"
        if word & 1:
            body += f" record(state, r[{ra}]);"
        mnemonic = {922: "extsh", 954: "extsb", 986: "extsw"}[xo]
    elif op in (28, 29):
        body = f"r[{ra}] = r[{rt}] & {literal((word & 0xffff) << (16 if op == 29 else 0))}; record(state, r[{ra}]);"
        mnemonic = "andi." if op == 28 else "andis."
    else:
        try:
            old = decode(word, pc)
        except AnyPS3Error as exc:
            raise AnyPS3Error(str(exc).split(" No output generated;")[0]) from exc
        mnemonic, body = old.mnemonic, translated_memory(old.statement)
    body += f" state.pc = 0x{next_pc:x};"
    return Operation(pc, word, mnemonic, body, successors)


FLOW_CPP = r'''
constexpr std::uint64_t TIMEBASE_HZ = 79800000;
[[maybe_unused]] static std::uint64_t monotonic_microseconds() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
[[maybe_unused]] static std::uint64_t timebase_ticks() {
    // Configured 79.8 MHz timebase; syscall 147 returns this same frequency.
    const auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    return (ns / 1000000000) * TIMEBASE_HZ + (ns % 1000000000) * TIMEBASE_HZ / 1000000000;
}
[[maybe_unused]] static std::uint32_t checked_target(std::uint64_t target) {
    if (target > UINT32_MAX) throw std::runtime_error("branch target outside 32-bit space");
    return static_cast<std::uint32_t>(target);
}
[[maybe_unused]] static std::uint64_t rotl64(std::uint64_t x, unsigned n) {
    n &= 63; return (x << n) | (x >> ((64 - n) & 63));
}
[[maybe_unused]] static std::uint32_t rotl32(std::uint32_t x, unsigned n) {
    n &= 31; return (x << n) | (x >> ((32 - n) & 31));
}
[[maybe_unused,gnu::always_inline]] static inline void compare(PpuState& state, unsigned field,
        std::uint64_t a, std::uint64_t b, bool wide, bool signed_compare) {
    if (!wide) { a &= UINT32_MAX; b &= UINT32_MAX; }
    if (signed_compare) {
        const auto sign = wide ? UINT64_C(0x8000000000000000) : UINT64_C(0x80000000);
        a ^= sign; b ^= sign;
    }
    const unsigned nibble = (a < b ? 8 : a > b ? 4 : 2) | ((state.xer >> 31) & 1);
    const unsigned shift = (7 - field) * 4;
    state.cr = (state.cr & ~(UINT32_C(15) << shift)) | (nibble << shift);
}
[[maybe_unused,gnu::always_inline]] static inline void record(PpuState& state, std::uint64_t value) {
    compare(state, 0, value, 0, true, true);
}
[[maybe_unused]] static std::uint64_t sign_word(std::uint64_t value) {
    return ((value & UINT32_MAX) ^ UINT64_C(0x80000000)) - UINT64_C(0x80000000);
}
[[maybe_unused]] static std::uint64_t divide_integer(std::uint64_t a,std::uint64_t b,bool wide,bool sign) {
    const auto mask=wide?UINT64_MAX:UINT64_C(0xffffffff);
    const auto sign_bit=wide?UINT64_C(0x8000000000000000):UINT64_C(0x80000000);
    a&=mask;b&=mask;
    // Division by zero and signed INT_MIN/-1 are architecturally undefined;
    // derived from RPCS3 PPUInterpreter.cpp DIVW/DIVWU/DIVD/DIVDU (5014, 5032,
    // 5118-5119, 5137-5138): the Cell result register is 0.
    if(!b || (sign && a==sign_bit && b==mask))return 0;
    const bool negative_a=sign && (a&sign_bit),negative_b=sign && (b&sign_bit);
    if(negative_a)a=(0-a)&mask;
    if(negative_b)b=(0-b)&mask;
    const auto quotient=a/b;
    return negative_a!=negative_b?0-quotient:quotient;
}
[[maybe_unused]] static std::int64_t signed_word(std::uint64_t value) {
    return static_cast<std::int64_t>(value & UINT32_MAX) - (value & 0x80000000 ? INT64_C(0x100000000) : 0);
}
[[maybe_unused]] static std::uint64_t arithmetic_shift(PpuState& state, std::uint64_t value, unsigned amount, unsigned width) {
    if (width == 32) value &= UINT32_MAX;
    const bool negative = (value >> (width - 1)) & 1;
    const auto discarded = amount >= width ? value : amount ? value & ((UINT64_C(1) << amount) - 1) : 0;
    const bool carry = negative && discarded;
    state.xer = (state.xer & ~UINT64_C(0x20000000)) | (carry ? UINT64_C(0x20000000) : 0);
    if (amount >= width) return negative ? UINT64_MAX : 0;
    if (width == 32) value = sign_word(value);
    return amount ? (value >> amount) | (negative ? UINT64_MAX << (64 - amount) : 0) : value;
}
[[maybe_unused]] static unsigned leading_zeroes(std::uint64_t value, unsigned width) {
    unsigned result = 0;
    for (unsigned i = width; i && !(value & (UINT64_C(1) << (i - 1))); --i) ++result;
    return result;
}
[[maybe_unused]] static bool branch_test(PpuState& state, unsigned bo, unsigned bi) {
    if (!(bo & 4)) --state.ctr;
    const bool count_ok = (bo & 4) || ((state.ctr != 0) != bool(bo & 2));
    const bool condition_ok = (bo & 16) || (((state.cr >> (31 - bi)) & 1) == unsigned(bool(bo & 8)));
    return count_ok && condition_ok;
}
'''
