"""SPU static translation and runtime fallback from one semantic lifter.

Register byte order and encodings follow IBM SPU ISA v1.2. Unsupported
instructions and channels are explicit traps; changed code uses the shared lifter.
"""
import json
import zlib
from .elf import ElfImage
from .errors import AnyPS3Error
from .spu_interp import Expr, hex_value, uint32, generate as generate_interpreter
from .spu_interp_support import CPP as DYNAMIC_CPP
from .spu_reloc import generate_index
from .spu_block import generate_blocks, CORE_CPP as BLOCK_CORE_CPP
from .spu_simd import VEC_CPP


def signed(value, bits):
    if isinstance(value, Expr):
        return Expr(f'(std::int64_t({value} ^ {1 << (bits-1)})-{1 << (bits-1)})')
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def lift(pc, word, *, runtime=False):
    body,nxt=lift_parts(pc,word,runtime=runtime)
    return body+nxt


def lift_parts(pc, word, *, runtime=False):
    """Return (semantic body, PC/control suffix) of one instruction."""
    op=word>>21
    if runtime: word=Expr("word")
    t=word&127; a=(word>>7)&127; b=(word>>14)&127; d=(word>>21)&127
    i7=(word>>14)&127; i10=signed((word>>14)&1023,10)
    i16=(word>>7)&65535; si16=signed(i16,16)
    w=lambda reg,lane='i': f'spu_word(g[{reg}],{lane})'
    setword=lambda expr: f'for(unsigned i=0;i<4;++i)spu_word(out,i,{expr}); g[{t}]=out;'
    nxt=f't.pc={hex_value((pc+4)&0x3fffc)};'
    body=None
    if op in (1,0x201,0x1ac) or op>>4 in (8,9): body=''
    elif op in (2,3):
        body='std::atomic_thread_fence(std::memory_order_seq_cst);'
        if op==2:
            if runtime:body+='if(word&(1u<<20))spu_sync_srr0(t);'
            elif word&(1<<20):body+='spu_sync_srr0(t);'
    # Cell BE Handbook v1.1 PDF p.67: this SPU has no special-purpose
    # registers. ISA v1.2 PDF pp.244-245 defines reads as zero, writes as NOP.
    elif op==0xc:body=f'g[{t}]={{}};'
    elif op==0x10c:body=''
    elif op>>2 in (0x81,0x82,0x83):
        value={0x81:si16&0xffffffff,0x82:i16<<16,0x83:i16|(i16<<16)}[op>>2]
        body=setword(f'{uint32(value)}')
    elif op>>4==0x21:body=setword(uint32((word>>7)&0x3ffff))
    elif op>>2==0xc1:body=setword(f'{w(t)}|{uint32(i16)}')
    elif op in (0xc0,0x40,0xc1,0x41,0x241,0x49,0x2c1,0x3c0,0xc2,0x340):
        expr={0xc0:f'{w(a)}+{w(b)}',0x40:f'{w(b)}-{w(a)}',0xc1:f'{w(a)}&{w(b)}',0x41:f'{w(a)}|{w(b)}',0x241:f'{w(a)}^{w(b)}',0x49:f'~({w(a)}|{w(b)})',0x2c1:f'{w(a)}&~{w(b)}',0x3c0:f'({w(a)}=={w(b)}?UINT32_MAX:0)',0xc2:f'((std::uint64_t({w(a)})+{w(b)})>>32)',0x340:f'{w(a)}+{w(b)}+({w(t)}&1)'}[op]
        body=setword(expr)
    elif op>>3 in (4,0xc,0x14,0x1c,0x44,0x4c,0x5c,0x7c):
        imm=f'{uint32(i10&0xffffffff)}'
        expr={4:f'{w(a)}|{imm}',0xc:f'{imm}-{w(a)}',0x14:f'{w(a)}&{imm}',0x1c:f'{w(a)}+{imm}',0x44:f'{w(a)}^{imm}',0x4c:f'(static_cast<std::int32_t>({w(a)})>{i10}?UINT32_MAX:0)',0x5c:f'({w(a)}>{imm}?UINT32_MAX:0)',0x7c:f'({w(a)}=={imm}?UINT32_MAX:0)'}[op>>3]
        body=setword(expr)
    elif op in (0x48,0xc8) or op>>3 in (0xd,0x1d):
        operand=f'{uint32(i10&65535)}' if op>>3 in (0xd,0x1d) else f'spu_element(g[{b}],i,2)'
        symbol='-' if op==0x48 or op>>3==0xd else '+'
        body=f'for(unsigned i=0;i<16;i+=2){{const auto value={operand}{symbol}spu_element(g[{a}],i,2);out[i]=value>>8;out[i+1]=value;}}g[{t}]=out;'
    elif op>>3==0x16:body=f'for(unsigned i=0;i<16;++i)out[i]=g[{a}][i]&{i10&255};g[{t}]=out;'
    elif op>>3 in (5,6,0x15,0x45,0x46):
        kind=op>>3
        imm=(i10&255)*0x01010101 if kind in (6,0x46) else (i10&65535)*0x10001
        symbol='&' if kind==0x15 else '^' if kind in (0x45,0x46) else '|'
        body=setword(f'{w(a)}{symbol}{uint32(imm)}')
    elif op in (0x240,0x248,0x250,0x2c0,0x2c8,0x2d0,0x3c8,0x3d0) or op>>3 in (0x4d,0x4e,0x5d,0x5e,0x7d,0x7e):
        immediate=op>>3 in (0x4d,0x4e,0x5d,0x5e,0x7d,0x7e)
        size=(2 if op>>3 in (0x4d,0x5d,0x7d) else 1) if immediate else (4 if op in (0x240,0x2c0) else 2 if op in (0x248,0x2c8,0x3c8) else 1)
        sign=op in (0x240,0x248,0x250) or (immediate and op>>3 in (0x4d,0x4e))
        equal=op in (0x3c8,0x3d0) or (immediate and op>>3 in (0x7d,0x7e))
        left=f'spu_element(g[{a}],i,{size})';right=str(i10&((1<<(size*8))-1)) if immediate else f'spu_element(g[{b}],i,{size})'
        if sign:left=f'static_cast<std::int{size*8}_t>({left})';right=f'static_cast<std::int{size*8}_t>({right})'
        body=f'for(unsigned i=0;i<16;i+={size}){{const bool yes={left}{"==" if equal else ">"}{right};for(unsigned j=0;j<{size};++j)out[i+j]=yes?255:0;}}g[{t}]=out;'
    elif op in (0x58,0x78):
        amount=f'({w(b)}&31)' if op==0x58 else str(i7&31)
        body=setword(f'({w(a)}<<{amount})|({w(a)}>>((32-{amount})&31))')
    elif op in (0x5a,0x7a):
        amount=f'((0u-{w(b)})&63)' if op==0x5a else uint32((-i7)&63)
        body=setword(f'static_cast<std::uint32_t>(static_cast<std::int32_t>({w(a)})>>std::min({amount},31u))')
    elif op in (0x5b,0x7b,0x59,0x79):
        amount=f'({w(b)}&63)' if op in (0x5b,0x59) else str(i7&63)
        if op in (0x59,0x79):amount=f'((0u-{amount})&63)'
        symbol='<<' if op in (0x5b,0x7b) else '>>'
        body=setword(f'({amount}<32?{w(a)}{symbol}({amount}&31):0)')
    elif op in (0x5c,0x5d,0x5e,0x5f,0x7c,0x7d,0x7e,0x7f):
        count=str(i7) if op>=0x7c else f'spu_element(g[{b}],i,2)'
        kind=op&3
        amount=f'({count}&15)' if kind==0 else f'((0u-{count})&31)' if kind in (1,2) else f'({count}&31)'
        expr={0:'(value<<n)|(value>>((16-n)&15))',1:'(n<16?value>>n:0)',2:'std::uint32_t(static_cast<std::int32_t>(static_cast<std::int16_t>(value))>>n)',3:'(n<16?value<<n:0)'}[kind]
        body=f'for(unsigned i=0;i<16;i+=2){{const auto value=spu_element(g[{a}],i,2);const unsigned n={amount};const auto result={expr};out[i]=result>>8;out[i+1]=result;}}g[{t}]=out;'
    elif op in (0x1fc,0x1dc,0x1ff,0x1fd,0x1cc,0x1cd,0x1cf,0x1dd,0x1df):
        if op in (0x1cc,0x1cd,0x1cf):
            count=f'({w(b,0)}>>3)'
            amount=f'({count}&15)' if op==0x1cc else f'((0u-{count})&31)' if op==0x1cd else f'({count}&31)'
        elif op in (0x1dd,0x1df):amount=f'((0u-{w(b,0)})&31)' if op==0x1dd else f'({w(b,0)}&31)'
        else:amount=f'({w(b,0)}&15)' if op==0x1dc else str(i7&15 if op==0x1fc else i7&31 if op==0x1ff else (-i7)&31)
        if op in (0x1fc,0x1dc,0x1cc):expr=f'g[{a}][(i+n)&15]'
        elif op in (0x1ff,0x1cf,0x1df):expr=f'(i+n<16?g[{a}][i+n]:0)'
        else:expr=f'(i>=n?g[{a}][i-n]:0)'
        body=f'const unsigned n={amount};for(unsigned i=0;i<16;++i)out[i]={expr};g[{t}]=out;'
    elif op in (0x1d8,0x1f8):
        amount=f'({w(b,0)}&7)' if op==0x1d8 else str(i7&7)
        body=f'const unsigned n={amount};for(unsigned i=0;i<16;++i)out[i]=(g[{a}][i]<<n)|(g[{a}][(i+1)&15]>>(8-n));g[{t}]=out;'
    elif op in (0x1d9,0x1db,0x1fb,0x1f9):
        amount=str((-i7)&7) if op==0x1f9 else str(i7&7) if op==0x1fb else f'((0u-{w(b,0)})&7)' if op==0x1d9 else f'({w(b,0)}&7)'
        expr=f'(g[{a}][i]>>n)|(i?g[{a}][i-1]<<(8-n):0)' if op in (0x1d9,0x1f9) else f'(g[{a}][i]<<n)|(i<15?g[{a}][i+1]>>(8-n):0)'
        body=f'const unsigned n={amount};for(unsigned i=0;i<16;++i)out[i]={expr};g[{t}]=out;'
    elif op>>7==0xb:
        body=f'g[{d}]=spu_shuffle(g[{a}],g[{b}],g[{t}]);'
    elif op>>7==8:
        body=f'for(unsigned i=0;i<16;++i)out[i]=(g[{a}][i]&~g[{t}][i])|(g[{b}][i]&g[{t}][i]);g[{d}]=out;'
    elif op in (0x1d4,0x1d5,0x1d6,0x1d7,0x1f4,0x1f5,0x1f6,0x1f7):
        size=1<<(op&3);offset=w(b,0) if op<0x1f4 else str(i7)
        body=f'for(unsigned i=0;i<16;++i)out[i]=16+i;const unsigned n=({w(a,0)}+{offset})&{16-size};for(unsigned i=0;i<{size};++i)out[n+i]={max(0,4-size)}+i;g[{t}]=out;'
    elif op>>2==0x65:
        body=f'for(unsigned i=0;i<16;++i)out[i]=({uint32(i16)}&(1u<<(15-i)))?255:0;g[{t}]=out;'
    elif op in (0x1b0,0x1b1,0x1b2):
        size={0x1b0:4,0x1b1:2,0x1b2:1}[op]
        body=f'unsigned bits=0;for(unsigned i=0;i<{16//size};++i)bits|=(g[{a}][i*{size}+{size-1}]&1)<<({16//size-1}-i);spu_word(out,0,bits);g[{t}]=out;'
    elif op in (0x1b4,0x1b5,0x1b6):
        size={0x1b4:4,0x1b5:2,0x1b6:1}[op]
        body=f'for(unsigned i=0;i<16;++i)out[i]=({w(a,0)}&(1u<<({16//size-1}-i/{size})))?255:0;g[{t}]=out;'
    elif op==0x2a5:
        body=f'for(unsigned i=0;i<4;++i){{auto value={w(a)};unsigned count=0;while(count<32 && !(value&0x80000000)){{++count;value<<=1;}}spu_word(out,i,count);}}g[{t}]=out;'
    elif op==0x2b4:
        body=f'for(unsigned i=0;i<16;++i){{unsigned count=0;for(unsigned bit=0;bit<8;++bit)count+=(g[{a}][i]>>bit)&1;out[i]=count;}}g[{t}]=out;'
    elif op==0x253:
        body=f'for(unsigned i=0;i<4;++i){{unsigned sa=0,sb=0;for(unsigned j=0;j<4;++j){{sa+=g[{a}][i*4+j];sb+=g[{b}][i*4+j];}}spu_word(out,i,(sb<<16)|sa);}}g[{t}]=out;'
    elif op==0x3cc:
        body=setword(f'({w(a)}&65535u)*({w(b)}&65535u)')
    elif op in (0x3c4,0x3c5,0x3c6,0x3c7,0x3ce,0x346,0x34e) or op>>3 in (0x74,0x75):
        high=op in (0x3c5,0x3c6,0x3ce,0x346,0x34e)
        left=f'({w(a)}>>16)' if high else f'({w(a)}&65535u)'
        right=str(i10&65535) if op>>3 in (0x74,0x75) else f'({w(b)}>>16)' if op in (0x3c6,0x3ce,0x346,0x34e) else f'({w(b)}&65535u)'
        signed_product=op in (0x3c4,0x3c6,0x3c7,0x346) or op>>3==0x74
        if signed_product:
            left=f'static_cast<std::int32_t>(static_cast<std::int16_t>({left}))'
            right=f'static_cast<std::int32_t>(static_cast<std::int16_t>({right}))'
        product=f'({left}*{right})'
        if op==0x3c5:product=f'({product}<<16)'
        elif op==0x3c7:product=f'({product}>>16)'
        product=f'std::uint32_t({product})'
        if op in (0x346,0x34e):product=f'({w(t)}+{product})'
        body=setword(product)
    elif op==0x398:body=f'g[{t}]=t.fpscr;'
    elif op==0x3ba:
        body=f'for(unsigned i=0;i<4;++i)spu_word(t.fpscr,i,{w(a)}&((i==0 || i==3)?0xf07u:0x3f07u));'
    elif op==0x1f0:
        body=f'spu_word(out,0,{w(a,0)}|{w(a,1)}|{w(a,2)}|{w(a,3)});g[{t}]=out;'
    elif op in (0x2b6,0x2ae,0x2a6):
        size={0x2b6:2,0x2ae:4,0x2a6:8}[op]
        body=f'for(unsigned i=0;i<16;i+={size}){{for(unsigned j=0;j<{size//2};++j)out[i+j]=(g[{a}][i+{size//2}]&128)?255:0;for(unsigned j={size//2};j<{size};++j)out[i+j]=g[{a}][i+j];}}g[{t}]=out;'
    elif op>>3 in (0x4f,0x5f,0x7f):
        condition={0x4f:f'static_cast<std::int32_t>({w(a,0)})>{i10}',0x5f:f'{w(a,0)}>{uint32(i10&0xffffffff)}',0x7f:f'{w(a,0)}=={uint32(i10&0xffffffff)}'}[op>>3]
        body=f'if({condition})throw std::runtime_error("SPU conditional halt reached");'
    elif op>>2 in (0x41,0x47,0x61,0x67) or op>>3 in (0x24,0x34) or op in (0x144,0x1c4):
        relative=op>>2 in (0x47,0x67)
        address=f'({w(a,0)}+{uint32((i10*16)&0xffffffff)})&0x3fff0' if op>>3 in (0x24,0x34) else f'{hex_value(((pc if relative else 0)+si16*4)&0x3fff0)}'
        if op in (0x144,0x1c4):address=f'({w(a,0)}+{w(b,0)})&0x3fff0'
        if op>>2 in (0x41,0x47) or op>>3==0x24 or op==0x144:body=f'const unsigned address={address};std::copy(g[{t}].begin(),g[{t}].end(),ls+address);spu_ls_forget(t,address,16);'
        else:body=f'const unsigned address={address};std::copy_n(ls+address,16,g[{t}].begin());'
    elif op>>2 in (0x40,0x42,0x44,0x46,0x60,0x62,0x64,0x66):
        kind=op>>2;target=((0 if kind in (0x60,0x62) else pc)+si16*4)&0x3fffc
        if kind in (0x62,0x66):body=f'spu_word(out,0,{(pc+4)&0x3fffc});g[{t}]=out;'
        else:body=''
        if kind in (0x40,0x42,0x44,0x46):
            value=f'({w(t,0)}&65535)' if kind in (0x44,0x46) else w(t,0)
            nxt=f't.pc=({value}{"==" if kind in (0x40,0x44) else "!="}0)?{hex_value(target)}:{hex_value((pc+4)&0x3fffc)};'
        else:nxt=f't.pc={hex_value(target)};'
    elif op in (0x1a8,0x1a9,0x128,0x129,0x12a,0x12b,0x1ab):
        flags=(word>>18)&3
        body=f'const auto target={w(a,0)}&0x3fffc;'
        if op in (0x1a9,0x1ab):body+=f'spu_word(out,0,{(pc+4)&0x3fffc});g[{t}]=out;'
        nxt=f'spu_branch_interrupts(t,{flags});t.pc=target;'
        if op in (0x128,0x129,0x12a,0x12b):
            value=f'({w(t,0)}&65535)' if op in (0x12a,0x12b) else w(t,0)
            nxt=f'if({value}{"==" if op in (0x128,0x12a) else "!="}0){{{nxt}}}else{{t.pc={hex_value((pc+4)&0x3fffc)};}}'
        elif op==0x1ab:
            nxt=f'if(spu_external_event(s,t)){{{nxt}}}else{{t.pc={hex_value((pc+4)&0x3fffc)};}}'
    elif op==0x1aa:
        body=f'const auto target=spu_interrupt_return(t);spu_branch_interrupts(t,{(word>>18)&3});'
        nxt='t.pc=target;'
    elif op==0x10d:body=f'spu_write_channel(s,t,ls,{a},{w(t,0)});'
    elif op==0xd:body=f'spu_word(out,0,spu_read_channel(s,t,{a}));g[{t}]=out;'
    elif op in (0x2c4,0x2c5,0x2c6):
        body=f'g[{t}]=spu_single_vector(t.fpscr,g[{a}],g[{b}],SpuRegister{{}},{op-0x2c4});'
    elif op>>7 in (0xd,0xe,0xf):
        kind={0xd:5,0xe:3,0xf:4}[op>>7]
        body=f'g[{d}]=spu_single_vector(t.fpscr,g[{a}],g[{b}],g[{t}],{kind});'
    elif op in (0x2c2,0x2ca,0x3c2,0x3ca):
        body=setword(f'spu_single_compare({w(a)},{w(b)},{str(bool(op&8)).lower()},{str(bool(op&0x100)).lower()})')
    elif op>>1 in (0x1d8,0x1d9,0x1da,0x1db):
        kind=op>>1;i8=Expr("std::int64_t((word>>14)&255)") if runtime else (word>>14)&255
        scale=(173 if kind in (0x1d8,0x1d9) else 155)-i8
        if not runtime and not 0<=scale<=127:raise AnyPS3Error('SPU conversion scale outside ISA-defined range 0..127')
        if kind in (0x1d8,0x1d9):body=setword(f'spu_float_to_int({w(a)},{scale},{str(kind==0x1d9).lower()})')
        else:body=setword(f'spu_int_to_float(t.fpscr,i,{w(a)},{scale},{str(kind==0x1db).lower()})')
    elif op in (0x1b8,0x1b9):body=setword(f'spu_{"frest" if op==0x1b8 else "frsqest"}({w(a)})')
    elif op==0x3d4:body=setword(f'spu_interpolate(t.fpscr,i,{w(a)},{w(b)})')
    elif op in (0x2cc,0x2cd,0x2ce,0x35c,0x35d,0x35e,0x35f):
        kind={0x2cc:0,0x2cd:1,0x2ce:2,0x35c:3,0x35d:4,0x35e:5,0x35f:6}[op]
        body=f'for(unsigned i=0;i<2;++i)spu_doubleword(out,i,spu_double_math(t.fpscr,i,spu_doubleword(g[{a}],i),spu_doubleword(g[{b}],i),spu_doubleword(g[{t}],i),{kind}));g[{t}]=out;'
    elif op in (0x3b8,0x3b9):
        source=f'spu_doubleword(g[{a}],i)' if op==0x3b9 else f'spu_word(g[{a}],i*2)'
        result=f'spu_ieee_convert(t.fpscr,i,{source},{str(op==0x3b9).lower()})'
        body=f'for(unsigned i=0;i<2;++i)'+(f'spu_word(out,i*2,std::uint32_t({result}));' if op==0x3b9 else f'spu_doubleword(out,i,{result});')+f'g[{t}]=out;'
    elif op in (0x2c3,0x2cb,0x3c3,0x3cb):
        body=f'for(unsigned i=0;i<2;++i)spu_doubleword(out,i,spu_double_compare(t.fpscr,i,spu_doubleword(g[{a}],i),spu_doubleword(g[{b}],i),{str(bool(op&8)).lower()},{str(bool(op&0x100)).lower()}));g[{t}]=out;'
    elif op==0x3bf:
        body=f'for(unsigned i=0;i<2;++i)spu_doubleword(out,i,spu_double_test(spu_doubleword(g[{a}],i),{i7}));g[{t}]=out;'
    elif op in (0x42,0x341,0x342,0x343,0xc9,0x249,0x2c9):
        expr={0x42:f'({w(b)}>={w(a)}?1u:0u)',0x341:f'{w(b)}-{w(a)}-1u+({w(t)}&1u)',0x342:f'std::uint32_t((std::uint64_t({w(a)})+{w(b)}+({w(t)}&1u))>>32)',0x343:f'(std::uint64_t({w(b)})>=std::uint64_t({w(a)})+1u-({w(t)}&1u)?1u:0u)',0xc9:f'~({w(a)}&{w(b)})',0x249:f'~({w(a)}^{w(b)})',0x2c9:f'{w(a)}|~{w(b)}'}[op]
        body=setword(expr)
    elif op in (0x53,0xd3):
        expr=f'unsigned(std::abs(int(g[{a}][i])-int(g[{b}][i])))' if op==0x53 else f'(unsigned(g[{a}][i])+g[{b}][i]+1)/2'
        body=f'for(unsigned i=0;i<16;++i)out[i]={expr};g[{t}]=out;'
    elif op>>7==0xc:
        body=f'for(unsigned i=0;i<4;++i)spu_word(out,i,std::uint32_t(std::int32_t(std::int16_t({w(a)}))*std::int32_t(std::int16_t({w(b)})))+{w(t)});g[{d}]=out;'
    elif op in (0x258,0x2d8,0x3d8):
        left=w(a,0);right=w(b,0)
        if op==0x258:left=f'static_cast<std::int32_t>({left})';right=f'static_cast<std::int32_t>({right})'
        body=f'if({left}{"==" if op==0x3d8 else ">"}{right})throw std::runtime_error("SPU conditional halt reached");'
    elif op==0xf:body=f'spu_word(out,0,spu_channel_count(s,t,{a}));g[{t}]=out;'
    elif op in (0,0x140):
        code=word&0x3fff if op==0 else 0x3fff
        # STOP/STOPD are valid ISA instructions for every immediate. The
        # external LV2 protocol is checked by spu_stop; unknown services trap
        # there without pretending to have exited a thread or joined a group.
        body=f'spu_stop(s,t,{hex_value(code)});'
    if body is None:raise AnyPS3Error('unsupported SPU opcode' if runtime else f'unsupported SPU instruction 0x{word:08x}')
    # These helpers inline a single host vector expression when the compile
    # target provides SSSE3. Generic targets retain the original scalar body.
    if op in (0xc0,0x40,0xc1,0x41,0x241,0x49,0x2c1,0x3c0,0x240,0x2c0,0xc8,0x48,0x3c8,0x248,0x2c8):
        body=f'g[{t}]=spu_integer_vector<{op}>(g[{a}],g[{b}]);'
    elif op>>3==0x1c:
        body=f'g[{t}]=spu_integer_vector<192>(g[{a}],spu_splat_word({uint32(i10&0xffffffff)}));'
    elif op in (0x1fc,0x1dc,0x1cc,0x1ff,0x1cf,0x1df,0x1fd,0x1cd,0x1dd):
        kind=0 if op in (0x1fc,0x1dc,0x1cc) else 1 if op in (0x1ff,0x1cf,0x1df) else 2
        body=f'g[{t}]=spu_rotate_bytes<{kind}>(g[{a}],{amount});'
    if runtime and op>>1 in (0x1d8,0x1d9,0x1da,0x1db):
        body=f'if({scale}<0 || {scale}>127)spu_dynamic_fault(t,ls,"SPU conversion scale outside ISA-defined range 0..127");'+body
    return body,nxt


def generate(images: tuple[ElfImage,...], overlays: tuple[tuple[tuple[int,bytes],...],...] = ()) -> str:
    core,functions,dispatch=generate_parts(images,overlays)
    return '\n'.join([core,*(text for _,text in functions),dispatch])


def generate_parts(images: tuple[ElfImage,...], overlays: tuple[tuple[tuple[int,bytes],...],...] = ()):
    """Return (helpers, [(name, native function)], dispatchers) for split compilation."""
    lines=[]
    fingerprints=[];reloc_sources=[]
    for index,image in enumerate(images):
        if image.machine!=23 or image.bits!=32 or image.byte_order!='big':raise AnyPS3Error('Expected big-endian ELF32 SPU image')
        ls=bytearray(0x40000);sources=[]
        for segment in image.segments:
            if segment.type!=1:continue
            a=segment.virtual_address
            if a+segment.memory_size>len(ls):raise AnyPS3Error('SPU segment outside local storage')
            ls[a:a+segment.file_size]=image.data[segment.offset:segment.offset+segment.file_size]
            if segment.flags&1:sources.append((a,bytes(ls[a:a+segment.file_size])))
        # type=0 is translation-only observed content, never a boot identity.
        fingerprints.append((image.entry,zlib.crc32(ls)) if image.type == 2 else None)
        for base,payload in overlays[index] if index<len(overlays) else ():
            if base%4 or len(payload)%4 or base+len(payload)>0x40000:raise AnyPS3Error('Invalid static SPU overlay range')
            sources.append((base,payload))
        reloc_sources.append(sources)
    # Content-validated superblocks of every source (spu_block.py). Dispatch is
    # by content, so any image's translation serves any thread with equal bytes.
    functions,native_dispatch=generate_blocks(images,overlays,lift_parts)
    lines.append('static void spu_prepare(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls){(void)s;(void)t;(void)ls;')
    if any(identity is not None for identity in fingerprints):
        lines.append('const auto hash=crc32(std::vector<std::uint8_t>(ls,ls+0x40000));')
        for i,identity in enumerate(fingerprints):
            if identity is None:continue
            entry,crc=identity
            lines.append(f'if(t.entry=={entry} && hash==UINT32_C({crc})){{t.image_index={i};t.pc=t.entry;t.reloc_delta=0;t.reloc_image_index=UINT32_MAX;t.gpr={{}};t.fpscr={{}};t.interrupts_enabled=false;t.srr0_valid=false;t.srr0_pending=false;t.srr0=0;t.srr0_write=0;spu_reset_decrementer(t,timebase_ticks());for(unsigned a=0;a<4;++a)for(unsigned b=0;b<8;++b)t.gpr[3+a][b]=t.arguments[a]>>(56-b*8);return;}}')
    lines.append('throw std::runtime_error("SPU image has no matching native AOT translation");}')
    lines.append(generate_index(reloc_sources))
    lines.append(native_dispatch)
    lines.append(r"""static void spu_step(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls,unsigned budget=1,std::uint64_t limit=UINT64_MAX,bool trace=false){
    HostAccessContext origin(HostAccessOrigin::Engine::Spu,t.id,t.pc,&t.steps);
    if(!budget)return;
    // Only spu_prepare binds a thread to a translated image; never run unbound LS.
    if(t.image_index==UINT32_MAX)throw std::runtime_error("unknown native SPU image");
    if(t.steps>=limit)throw std::runtime_error("SPU execution step limit reached");
    t.block_budget=budget;t.block_limit=limit;t.block_trace=trace || spu_capture.file;t.block_control_exit=false;
    // Budgets below one maximal superblock are exact; scheduler slices may end after a whole block.
    t.block_exact=budget<SPU_MAX_BLOCK;
    spu_take_interrupt(s,t);
    if(t.pc>0x3fffc || t.pc%4)spu_dynamic_fault(t,ls,"invalid SPU fetch address");
    // Traces/captures observe every fetch: the exact interpreter serves them.
    // Native superblocks validate their bytes on every entry (spu_block.py);
    // the dispatch cache is only a hint. No validity outlives an entry.
    for(;;){
        if(!t.block_trace){
            while(spu_native_enter(s,t,ls))
                if(!t.block_budget || t.steps>=limit || t.block_control_exit)return;
        }
        spu_stat(t,10);
        if(t.block_trace){t.reloc_delta=0;spu_interpret_block(s,t,ls);}
        else{
            // Exact interpreter until the next superblock leader. Interrupts are sampled
            // at superblock entries and after channel instructions, so a handler's IRET
            // returns to a leader rather than into the middle of a block.
            const auto delta=t.reloc_delta;
            if(__builtin_expect(spu_stats_on,0)){
                if(!t.interp_entries)t.interp_entries=std::make_shared<std::array<std::atomic<std::uint64_t>,1024>>();
                auto& table=*t.interp_entries;
                for(unsigned i=0,h=(t.pc>>2)*2654435761u>>22;i<64;++i,h=(h+1)&1023){
                    const auto v=table[h].load(std::memory_order_relaxed);
                    if(!v || (v>>32)==t.pc+1){table[h].store((std::uint64_t(t.pc+1)<<32)|((v&0xffffffffu)+1),std::memory_order_relaxed);break;}
                }
            }
            do{
                const auto op=spu_load_word(ls+t.pc)>>21;
                spu_interpret(s,t,ls);
                if(op==0xd || op==0xf || op==0x10d){t.irq_next=0;spu_take_interrupt(s,t);}
            }while(t.block_budget && t.steps<limit && !t.block_control_exit && !spu_native_leader(t,ls,delta));
            t.reloc_delta=0;
        }
        if(!t.block_budget || t.steps>=limit || t.block_control_exit)return;
    }
}""")
    lines.append(SCHEDULER_CPP + POOL_CPP)
    declarations=[]
    return CORE_CPP,functions,'\n'.join([DYNAMIC_CPP,generate_interpreter(lift),*declarations,*lines])


CORE_CPP = r'''
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cinttypes>
// Shared across AOT translation units. Disabled profiling never reads the clock.
struct SpuProfile {
    enum Category {FP,FP_FAST,FP_SLOW,FP_DOUBLE,DMA,CHANNEL,STOP,WAIT,COUNT};
    struct Counter {std::uint64_t calls=0,ns=0;};
    std::array<Counter,COUNT> category{};
    std::uint64_t fp_fast_lanes=0,fp_slow_lanes=0,dma_commands=0,dma_bytes=0,dma_elements=0;
    bool enabled=[](){const char* v=std::getenv("ANYPS3_SPU_PROFILE");return v && v[0]=='1' && !v[1];}();
    std::chrono::steady_clock::time_point at{};
    unsigned polls=0;
};
inline thread_local SpuProfile spu_profile;
// Process-wide copies of the diagnostic switches: hot paths test a plain global,
// never the dynamically initialized TLS profile (TLS wrapper call per access).
inline bool spu_profile_on=[](){const char* v=std::getenv("ANYPS3_SPU_PROFILE");return v && v[0]=='1' && !v[1];}();
inline const bool spu_stats_on=[](){const char* v=std::getenv("ANYPS3_SPU_STATS");return v && v[0]=='1' && !v[1];}();
template<class T> [[gnu::always_inline]] static inline void spu_stat(T& t,unsigned i){
    if(__builtin_expect(spu_stats_on,0)){auto& c=t.stats[i].value;c.store(c.load(std::memory_order_relaxed)+1,std::memory_order_relaxed);}
}
// DMA command history (shared mutex-protected ring) is a diagnostic: recorded only
// with ANYPS3_SPU_DMA_HISTORY=1 or the live ANYPS3_SPU_TRACE_DMA=1 trace.
inline const bool spu_dma_history_on=[](){for(const char* name:{"ANYPS3_SPU_DMA_HISTORY","ANYPS3_SPU_TRACE_DMA"}){const char* v=std::getenv(name);if(v && v[0]=='1' && !v[1])return true;}return false;}();
inline const bool spu_provenance_on=[](){const char* v=std::getenv("ANYPS3_SPU_DYNAMIC_LOG");return v && *v;}();
struct SpuProfileScope {
    SpuProfile::Counter* counter=nullptr;
    std::chrono::steady_clock::time_point start{};
    explicit SpuProfileScope(SpuProfile::Category c){
        if(spu_profile_on){counter=&spu_profile.category[c];++counter->calls;start=std::chrono::steady_clock::now();}
    }
    ~SpuProfileScope(){if(counter)counter->ns+=std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now()-start).count();}
};
[[maybe_unused]] static void spu_profile_report(bool force=false){
    if(!spu_profile_on)return;
    // Poll infrequently; reporting has the same two-second cadence as PERF.
    if(!force && (++spu_profile.polls&1023))return;
    const auto now=std::chrono::steady_clock::now();
    if(spu_profile.at==std::chrono::steady_clock::time_point{})spu_profile.at=now;
    const double seconds=std::chrono::duration<double>(now-spu_profile.at).count();
    if(!force && seconds<2)return;
    std::fprintf(stderr,"SPU PERF interval_s=%.3f fp_fast_lanes=%" PRIu64 " fp_slow_lanes=%" PRIu64 " dma_commands=%" PRIu64 " dma_bytes=%" PRIu64 " dma_elements=%" PRIu64,
        seconds,spu_profile.fp_fast_lanes,spu_profile.fp_slow_lanes,spu_profile.dma_commands,spu_profile.dma_bytes,spu_profile.dma_elements);
    static constexpr const char* names[]={"fp","fp_fast","fp_slow","fp_double","dma","channel","stop","wait"};
    for(unsigned i=0;i<SpuProfile::COUNT;++i)std::fprintf(stderr," %s_calls=%" PRIu64 " %s_ns=%" PRIu64,names[i],spu_profile.category[i].calls,names[i],spu_profile.category[i].ns);
    std::fputc('\n',stderr);
    spu_profile.at=now;
    // Counters are cumulative; only the wall-clock interval is reset.
}
[[maybe_unused]] static std::uint32_t spu_load_word(const std::uint8_t* p){
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
    std::uint32_t value;__builtin_memcpy(&value,p,4);return __builtin_bswap32(value);
#elif __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
    std::uint32_t value;__builtin_memcpy(&value,p,4);return value;
#else
    return (std::uint32_t(p[0])<<24)|(std::uint32_t(p[1])<<16)|(std::uint32_t(p[2])<<8)|p[3];
#endif
}
[[maybe_unused]] static std::uint32_t spu_word(const SpuRegister& r,unsigned lane){return spu_load_word(r.data()+lane*4);}
[[maybe_unused]] static std::uint32_t spu_element(const SpuRegister& r,unsigned offset,unsigned size){std::uint32_t value=0;for(unsigned i=0;i<size;++i)value=(value<<8)|r[offset+i];return value;}
[[maybe_unused]] static void spu_word(SpuRegister& r,unsigned lane,std::uint32_t value){
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
    value=__builtin_bswap32(value);__builtin_memcpy(r.data()+lane*4,&value,4);
#elif __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
    __builtin_memcpy(r.data()+lane*4,&value,4);
#else
    for(unsigned i=0;i<4;++i)r[lane*4+i]=value>>(24-i*8);
#endif
}
[[maybe_unused]] static SpuRegister spu_shuffle_scalar(const SpuRegister& a,const SpuRegister& b,const SpuRegister& control){
    SpuRegister out{};for(unsigned i=0;i<16;++i){const auto c=control[i];out[i]=(c&128)?((c&64)?((c&32)?128:255):0):((c&31)<16?a[c&15]:b[c&15]);}return out;
}
#if defined(__x86_64__) || defined(__i386__)
#include <tmmintrin.h>
// Runtime feature selection keeps generic builds valid on non-SSSE3 hosts.
// memcpy permits unaligned register objects and preserves all alias cases.
#if defined(__SSSE3__)
[[gnu::always_inline]] static inline
#else
__attribute__((target("ssse3"))) static
#endif
SpuRegister spu_shuffle_ssse3(const SpuRegister& a,const SpuRegister& b,const SpuRegister& control){
    __m128i av,bv,c;__builtin_memcpy(&av,a.data(),16);__builtin_memcpy(&bv,b.data(),16);__builtin_memcpy(&c,control.data(),16);
    const auto select_a=_mm_cmpeq_epi8(_mm_and_si128(c,_mm_set1_epi8(16)),_mm_setzero_si128());
    const auto normal=_mm_or_si128(_mm_and_si128(select_a,_mm_shuffle_epi8(av,c)),_mm_andnot_si128(select_a,_mm_shuffle_epi8(bv,c)));
    const auto constants=_mm_setr_epi8(0,0,0,0,0,0,-1,-128,0,0,0,0,0,0,0,0);
    const auto index=_mm_and_si128(_mm_srli_epi16(c,5),_mm_set1_epi8(7));
    const auto value=_mm_or_si128(normal,_mm_shuffle_epi8(constants,index));
    SpuRegister result;__builtin_memcpy(result.data(),&value,16);return result;
}
#endif
[[maybe_unused]] static SpuRegister spu_shuffle(const SpuRegister& a,const SpuRegister& b,const SpuRegister& control){
#if defined(__SSSE3__)
    return spu_shuffle_ssse3(a,b,control);
#elif defined(__x86_64__) || defined(__i386__)
    if(__builtin_cpu_supports("ssse3"))return spu_shuffle_ssse3(a,b,control);
#endif
    return spu_shuffle_scalar(a,b,control);
}
[[maybe_unused]] static SpuRegister spu_splat_word(std::uint32_t value){
    SpuRegister out;for(unsigned i=0;i<4;++i)spu_word(out,i,value);return out;
}
// Byte arrays are architectural BE order. Swap only within a host word (or
// halfword), never between lanes. memcpy supports unaligned objects/aliases.
template<unsigned Op> static SpuRegister spu_integer_vector(const SpuRegister& a,const SpuRegister& b){
    SpuRegister out;
#if defined(__SSSE3__)
    __m128i av,bv,result;__builtin_memcpy(&av,a.data(),16);__builtin_memcpy(&bv,b.data(),16);
    if constexpr(Op==0xc1)result=_mm_and_si128(av,bv);
    else if constexpr(Op==0x41)result=_mm_or_si128(av,bv);
    else if constexpr(Op==0x241)result=_mm_xor_si128(av,bv);
    else if constexpr(Op==0x49)result=_mm_xor_si128(_mm_or_si128(av,bv),_mm_set1_epi32(-1));
    else if constexpr(Op==0x2c1)result=_mm_andnot_si128(bv,av);
    else {
        constexpr bool half=Op==0xc8 || Op==0x48 || Op==0x3c8 || Op==0x248 || Op==0x2c8;
        const auto endian=half?_mm_setr_epi8(1,0,3,2,5,4,7,6,9,8,11,10,13,12,15,14):_mm_setr_epi8(3,2,1,0,7,6,5,4,11,10,9,8,15,14,13,12);
        av=_mm_shuffle_epi8(av,endian);bv=_mm_shuffle_epi8(bv,endian);
        if constexpr(Op==0xc0)result=_mm_add_epi32(av,bv);
        else if constexpr(Op==0x40)result=_mm_sub_epi32(bv,av);
        else if constexpr(Op==0xc8)result=_mm_add_epi16(av,bv);
        else if constexpr(Op==0x48)result=_mm_sub_epi16(bv,av);
        else if constexpr(Op==0x3c0)result=_mm_cmpeq_epi32(av,bv);
        else if constexpr(Op==0x3c8)result=_mm_cmpeq_epi16(av,bv);
        else if constexpr(Op==0x240)result=_mm_cmpgt_epi32(av,bv);
        else if constexpr(Op==0x248)result=_mm_cmpgt_epi16(av,bv);
        else if constexpr(Op==0x2c0)result=_mm_cmpgt_epi32(_mm_xor_si128(av,_mm_set1_epi32(INT32_MIN)),_mm_xor_si128(bv,_mm_set1_epi32(INT32_MIN)));
        else if constexpr(Op==0x2c8)result=_mm_cmpgt_epi16(_mm_xor_si128(av,_mm_set1_epi16(INT16_MIN)),_mm_xor_si128(bv,_mm_set1_epi16(INT16_MIN)));
        result=_mm_shuffle_epi8(result,endian);
    }
    __builtin_memcpy(out.data(),&result,16);
#else
    constexpr unsigned width=Op==0xc8 || Op==0x48 || Op==0x3c8 || Op==0x248 || Op==0x2c8?2:4;
    for(unsigned i=0;i<16;i+=width){
        const auto av=spu_element(a,i,width),bv=spu_element(b,i,width);std::uint32_t result=0;
        if constexpr(Op==0xc0 || Op==0xc8)result=av+bv;
        else if constexpr(Op==0x40 || Op==0x48)result=bv-av;
        else if constexpr(Op==0xc1)result=av&bv;
        else if constexpr(Op==0x41)result=av|bv;
        else if constexpr(Op==0x241)result=av^bv;
        else if constexpr(Op==0x49)result=~(av|bv);
        else if constexpr(Op==0x2c1)result=av&~bv;
        else if constexpr(Op==0x3c0 || Op==0x3c8)result=av==bv?UINT32_MAX:0;
        else if constexpr(Op==0x240)result=std::int32_t(av)>std::int32_t(bv)?UINT32_MAX:0;
        else if constexpr(Op==0x248)result=std::int16_t(av)>std::int16_t(bv)?UINT32_MAX:0;
        else if constexpr(Op==0x2c0 || Op==0x2c8)result=av>bv?UINT32_MAX:0;
        for(unsigned j=0;j<width;++j)out[i+j]=result>>(8*(width-1-j));
    }
#endif
    return out;
}
template<unsigned Kind> static SpuRegister spu_rotate_bytes(const SpuRegister& a,unsigned n){
    SpuRegister out;
#if defined(__SSSE3__)
    __m128i av;__builtin_memcpy(&av,a.data(),16);
    auto index=_mm_setr_epi8(0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15);
    if constexpr(Kind==2)index=_mm_sub_epi8(index,_mm_set1_epi8(n));
    else index=_mm_add_epi8(index,_mm_set1_epi8(n));
    if constexpr(Kind==0)index=_mm_and_si128(index,_mm_set1_epi8(15));
    else index=_mm_or_si128(index,_mm_cmpgt_epi8(index,_mm_set1_epi8(15)));
    const auto result=_mm_shuffle_epi8(av,index);__builtin_memcpy(out.data(),&result,16);
#else
    for(unsigned i=0;i<16;++i){
        if constexpr(Kind==0)out[i]=a[(i+n)&15];
        else if constexpr(Kind==1)out[i]=i+n<16?a[i+n]:0;
        else out[i]=i>=n?a[i-n]:0;
    }
#endif
    return out;
}
// SPU ISA v1.2 PDF pp.195-201. All finite arithmetic is integer-only.
// A fixed binary accumulator retains every bit of a product and an addend:
// singles need bits -298..257; doubles (including subnormals) -2148..2047.
template<unsigned N,int Base> struct SpuExact {
    std::array<std::uint64_t,N> limb{}; bool negative=false;
    static SpuExact value(__uint128_t mantissa,int exponent,bool sign=false){
        SpuExact r;r.negative=sign;
        if(!mantissa)return r;
        const unsigned shift=unsigned(exponent-Base),index=shift/64,bits=shift%64;
        const std::uint64_t lo=std::uint64_t(mantissa),hi=std::uint64_t(mantissa>>64);
        r.limb.at(index)|=lo<<bits;
        if(index+1<N)r.limb[index+1]|=bits?(lo>>(64-bits))|(hi<<bits):hi;
        if(bits && index+2<N)r.limb[index+2]|=hi>>(64-bits);
        return r;
    }
    int top()const{
        for(unsigned i=N;i--;){if(limb[i])return int(i*64+63-__builtin_clzll(limb[i]));}
        return -1;
    }
    bool bit(unsigned n)const{return n<N*64 && ((limb[n/64]>>(n%64))&1);}
    bool below(unsigned n)const{
        for(unsigned i=0;i<N && i<n/64;++i)if(limb[i])return true;
        return n/64<N && n%64 && (limb[n/64]&((UINT64_C(1)<<(n%64))-1));
    }
    std::uint64_t extract(unsigned n)const{
        if(n/64>=N)return 0;
        auto r=limb[n/64]>>(n%64);
        if(n%64 && n/64+1<N)r|=limb[n/64+1]<<(64-n%64);
        return r;
    }
    void add(const SpuExact& b){
        if(negative==b.negative){
            __uint128_t carry=0;
            for(unsigned i=0;i<N;++i){carry+=__uint128_t(limb[i])+b.limb[i];limb[i]=std::uint64_t(carry);carry>>=64;}
        }else{
            bool smaller=false;
            for(unsigned i=N;i--;){if(limb[i]!=b.limb[i]){smaller=limb[i]<b.limb[i];break;}}
            std::uint64_t borrow=0;
            for(unsigned i=0;i<N;++i){
                const auto x=smaller?b.limb[i]:limb[i],y=smaller?limb[i]:b.limb[i];
                const __uint128_t sub=__uint128_t(y)+borrow;
                limb[i]=std::uint64_t(__uint128_t(x)-sub);borrow=__uint128_t(x)<sub;
            }
            if(smaller)negative=b.negative;
        }
    }
};
using SpuSingleExact=SpuExact<10,-298>;
using SpuDoubleExact=SpuExact<68,-2148>;
struct SpuFloatParts {std::uint64_t m;int e;bool sign;};
[[maybe_unused]] static SpuFloatParts spu_single_parts(std::uint32_t x){
    const unsigned e=(x>>23)&255;
    return {e?UINT64_C(0x800000)|(x&0x7fffff):0,int(e)-150,bool(x>>31)};
}
[[maybe_unused]] static void spu_fp_flag(SpuRegister& fpscr,unsigned word,std::uint32_t flag){spu_word(fpscr,word,spu_word(fpscr,word)|flag);}
[[maybe_unused]] static bool spu_single_diff(std::uint32_t x){return (x&0x7f800000)==0x7f800000 || (!(x&0x7f800000) && (x&0x7fffff));}
[[maybe_unused]] static std::uint32_t spu_single_round(const SpuSingleExact& x,SpuRegister& fpscr,unsigned lane){
    const int top=x.top(),exponent=top-298;
    if(top<0)return 0;
    if(exponent< -126){spu_fp_flag(fpscr,lane,3);return 0;}
    const unsigned shift=unsigned(top-23);
    const auto mantissa=x.extract(shift);
    if(exponent>128 || (exponent==128 && mantissa==0xffffff && x.below(shift))){
        spu_fp_flag(fpscr,lane,5);return (std::uint32_t(x.negative)<<31)|0x7fffffff;
    }
    if(exponent==128)spu_fp_flag(fpscr,lane,1);
    return (std::uint32_t(x.negative)<<31)|(std::uint32_t(exponent+127)<<23)|(std::uint32_t(mantissa)&0x7fffff);
}
[[maybe_unused]] static std::uint32_t spu_single_math(SpuRegister& fpscr,unsigned lane,std::uint32_t a,std::uint32_t b,std::uint32_t c,unsigned kind){
    SpuProfileScope profile(SpuProfile::FP_SLOW);
    if(spu_profile_on)++spu_profile.fp_slow_lanes;
    // kind: 0 add, 1 subtract, 2 multiply, 3 fma, 4 fms, 5 fnms.
    if(spu_single_diff(a)||spu_single_diff(b)||(kind>=3 && spu_single_diff(c)))spu_fp_flag(fpscr,lane,1);
    const auto av=spu_single_parts(a),bv=spu_single_parts(b),cv=spu_single_parts(c);
    SpuSingleExact r;
    if(kind<2){r=SpuSingleExact::value(av.m,av.e,av.sign);r.add(SpuSingleExact::value(bv.m,bv.e,bv.sign^(kind==1)));}
    else{
        r=SpuSingleExact::value(__uint128_t(av.m)*bv.m,av.e+bv.e,av.sign^bv.sign^(kind==5));
        if(kind>=3)r.add(SpuSingleExact::value(cv.m,cv.e,cv.sign^(kind==4)));
    }
    return spu_single_round(r,fpscr,lane);
}
#if defined(__x86_64__) || defined(__i386__)
#include <immintrin.h>
// ISA v1.2 PDF195-196,208,210,212: RZ, positive zero, extended exponent,
// fused unbounded product. Never expose this host environment to PPU/RSX.
struct SpuSingleEnvironment {
    unsigned previous=_mm_getcsr();
    SpuSingleEnvironment(){_mm_setcsr((previous&~0x603fu)|0xffc0u);}
    ~SpuSingleEnvironment(){_mm_setcsr(previous);}
};
__attribute__((target("fma"),noinline)) static __m128 spu_single_fused(__m128 a,__m128 b,__m128 c,unsigned kind){
    if(kind==3)return _mm_fmadd_ps(a,b,c);
    if(kind==4)return _mm_fmsub_ps(a,b,c);
    return _mm_fnmadd_ps(a,b,c);
}
// A call boundary keeps arithmetic between the MXCSR save and restore even
// under ordinary builds without -frounding-math. No compiler FP contraction.
__attribute__((noinline)) static __m128 spu_single_sse(__m128 a,__m128 b,unsigned kind){
    if(kind==0)return _mm_add_ps(a,b);
    if(kind==1)return _mm_sub_ps(a,b);
    return _mm_mul_ps(a,b);
}
#endif
[[maybe_unused]] static SpuRegister spu_single_vector(SpuRegister& fpscr,const SpuRegister& a,const SpuRegister& b,const SpuRegister& c,unsigned kind){
    SpuProfileScope profile(SpuProfile::FP);
    SpuRegister out{};
#if defined(__x86_64__) || defined(__i386__)
    // SSE2 is mandatory on x86-64; the runtime guard also permits i386 builds.
    if(__builtin_cpu_supports("sse2") && (kind<3 || __builtin_cpu_supports("fma"))){
        unsigned slow=0;
        {
        SpuProfileScope fast_profile(SpuProfile::FP_FAST);
        const auto av=_mm_setr_epi32(spu_word(a,0),spu_word(a,1),spu_word(a,2),spu_word(a,3));
        const auto bv=_mm_setr_epi32(spu_word(b,0),spu_word(b,1),spu_word(b,2),spu_word(b,3));
        const auto cv=_mm_setr_epi32(spu_word(c,0),spu_word(c,1),spu_word(c,2),spu_word(c,3));
        std::array<std::uint32_t,4> result;
        {
            SpuSingleEnvironment environment;
            const auto r=kind<3?spu_single_sse(_mm_castsi128_ps(av),_mm_castsi128_ps(bv),kind):
                spu_single_fused(_mm_castsi128_ps(av),_mm_castsi128_ps(bv),_mm_castsi128_ps(cv),kind);
            __builtin_memcpy(result.data(),&r,16);
        }
        for(unsigned i=0;i<4;++i){
            const auto x=spu_word(a,i),y=spu_word(b,i),z=spu_word(c,i),r=result[i],m=r&0x7fffffffu;
            // Denormal/extended inputs need DIFF. IEEE RZ overflow can also
            // produce 0x7f7fffff, so include that boundary in the slow path.
            bool safe=!spu_single_diff(x) && !spu_single_diff(y) && (kind<3 || !spu_single_diff(z));
            const bool exact_zero=kind<2?((x&0x7fffffff)==(y&0x7fffffff)):
                ((!(x&0x7fffffff) || !(y&0x7fffffff)) && (kind==2 || !(z&0x7fffffff)));
            safe=safe && ((m>=0x00800000u && m<0x7f7fffffu) || (!m && exact_zero));
            if(safe){spu_word(out,i,m?r:0);if(spu_profile_on)++spu_profile.fp_fast_lanes;}
            else slow|=1u<<i;
        }
        }
        for(unsigned i=0;i<4;++i)if(slow&(1u<<i))spu_word(out,i,spu_single_math(fpscr,i,spu_word(a,i),spu_word(b,i),spu_word(c,i),kind));
        return out;
    }
#endif
    for(unsigned i=0;i<4;++i)spu_word(out,i,spu_single_math(fpscr,i,spu_word(a,i),spu_word(b,i),spu_word(c,i),kind));
    return out;
}
[[maybe_unused]] static bool spu_fp_compare(std::uint64_t a,std::uint64_t b,bool magnitude,bool equal,unsigned signbit){
    const auto sign=UINT64_C(1)<<signbit;
    if(magnitude){a&=sign-1;b&=sign-1;}
    if(!(a&(sign-1)))a=0;
    if(!(b&(sign-1)))b=0;
    if(equal)return a==b;
    if((a^b)&sign)return !(a&sign);
    return (a&sign)?a<b:a>b;
}
[[maybe_unused]] static std::uint32_t spu_single_compare(std::uint32_t a,std::uint32_t b,bool magnitude,bool equal){
    if(!(a&0x7f800000))a=0;
    if(!(b&0x7f800000))b=0;
    return spu_fp_compare(a,b,magnitude,equal,31)?UINT32_MAX:0;
}
[[maybe_unused]] static std::uint32_t spu_float_to_int(std::uint32_t x,int scale,bool unsign){
    const auto p=spu_single_parts(x);
    if(!p.m || (unsign && p.sign))return 0;
    const int e=p.e+scale;
    const auto limit=unsign?UINT64_C(0xffffffff):p.sign?UINT64_C(0x80000000):UINT64_C(0x7fffffff);
    std::uint64_t result;
    if(e>=0){if(e>32 || p.m>(limit>>e))result=limit;else result=p.m<<e;}
    else result=e<=-64?0:p.m>>(-e);
    if(result>limit)result=limit;
    return p.sign?std::uint32_t(0u-std::uint32_t(result)):std::uint32_t(result);
}
[[maybe_unused]] static std::uint32_t spu_int_to_float(SpuRegister& fpscr,unsigned lane,std::uint32_t x,int scale,bool unsign){
    const bool sign=!unsign && (x>>31);
    return spu_single_round(SpuSingleExact::value(sign?std::uint32_t(0u-x):x,-scale,sign),fpscr,lane);
}
[[maybe_unused]] static std::uint32_t spu_interpolate(SpuRegister& fpscr,unsigned lane,std::uint32_t a,std::uint32_t b){
    // PDF pp.215,219: base has 13 fraction bits, step has 10 bits at 2^-13,
    // Y is RA[13:31] at 2^-19. Evaluate the complete subtraction before rounding.
    if(spu_single_diff(a)||spu_single_diff(b))spu_fp_flag(fpscr,lane,1);
    const int e=int((b>>23)&255)-127;const bool sign=b>>31;
    auto r=SpuSingleExact::value(0x2000|((b>>10)&0x1fff),e-13,sign);
    r.add(SpuSingleExact::value(std::uint64_t(b&1023)*(a&0x7ffff),e-32,!sign));
    return spu_single_round(r,fpscr,lane);
}
[[maybe_unused]] static std::uint64_t spu_doubleword(const SpuRegister& r,unsigned lane){return (std::uint64_t(spu_word(r,lane*2))<<32)|spu_word(r,lane*2+1);}
[[maybe_unused]] static void spu_doubleword(SpuRegister& r,unsigned lane,std::uint64_t x){spu_word(r,lane*2,std::uint32_t(x>>32));spu_word(r,lane*2+1,std::uint32_t(x));}
[[maybe_unused]] static SpuFloatParts spu_ieee_parts(std::uint64_t x,unsigned fraction,int bias){
    const auto exponent=(x>>fraction)&unsigned(2*bias+1),mantissa=x&((UINT64_C(1)<<fraction)-1);
    return {exponent?(UINT64_C(1)<<fraction)|mantissa:mantissa,int(exponent?exponent:1)-bias-int(fraction),bool(x>>(fraction+(fraction==52?11:8)))};
}
[[maybe_unused]] static bool spu_double_nan(std::uint64_t x){return (x&UINT64_C(0x7fffffffffffffff))>UINT64_C(0x7ff0000000000000);}
[[maybe_unused]] static void spu_double_flag(SpuRegister& f,unsigned lane,unsigned flags){spu_fp_flag(f,lane+1,flags<<8);}
[[maybe_unused]] static unsigned spu_double_mode(const SpuRegister& f,unsigned lane){return (spu_word(f,0)>>(lane?8:10))&3;}
[[maybe_unused]] static std::uint64_t spu_ieee_round(const SpuDoubleExact& x,SpuRegister& f,unsigned lane,unsigned fraction,int bias,bool zero_sign){
    const unsigned mode=spu_double_mode(f,lane),signbit=fraction+(fraction==52?11:8);
    const auto sign=std::uint64_t(x.negative)<<signbit;
    const int top=x.top();
    if(top<0)return std::uint64_t(zero_sign)<<signbit;
    int exponent=top-2148;
    const int quantum=std::max(exponent-int(fraction),1-bias-int(fraction));
    const unsigned shift=unsigned(quantum+2148);
    auto mantissa=x.extract(shift);
    const bool inexact=x.below(shift);
    if(inexact){
        spu_double_flag(f,lane,8);
        if(exponent<1-bias)spu_double_flag(f,lane,16);
        const bool up=mode==0?(x.bit(shift-1)&&(x.below(shift-1)||(mantissa&1))):mode==2?!x.negative:mode==3?x.negative:false;
        if(up)++mantissa;
    }
    if(mantissa>=(UINT64_C(2)<<fraction)){mantissa>>=1;++exponent;}
    if(exponent>bias){
        spu_double_flag(f,lane,40);
        const bool inf=mode==0 || (mode==2 && !x.negative) || (mode==3 && x.negative);
        const auto infinity=std::uint64_t(2*bias+1)<<fraction;
        return sign|(inf?infinity:infinity-1);
    }
    const auto encoded_exponent=mantissa>=(UINT64_C(1)<<fraction)?unsigned(std::max(exponent,1-bias)+bias):0;
    return sign|(std::uint64_t(encoded_exponent)<<fraction)|(mantissa&((UINT64_C(1)<<fraction)-1));
}
[[maybe_unused]] static std::uint64_t spu_double_math(SpuRegister& f,unsigned lane,std::uint64_t a,std::uint64_t b,std::uint64_t c,unsigned kind){
    SpuProfileScope profile(SpuProfile::FP_DOUBLE);
    // 0 dfa,1 dfs,2 dfm,3 dfma,4 dfms,5 dfnms,6 dfnma.
    const auto av=spu_ieee_parts(a,52,1023),bv=spu_ieee_parts(b,52,1023),cv=spu_ieee_parts(c,52,1023);
    bool nan=false,invalid=false;
    for(auto x:{a,b,kind>=3?c:UINT64_C(0)})if(spu_double_nan(x)){nan=true;if(!(x&UINT64_C(0x8000000000000)))invalid=true;}
    if(nan){spu_double_flag(f,lane,2|(invalid?4:0));return UINT64_C(0x7ff8000000000000);}
    const auto inf=[](std::uint64_t x){return (x&UINT64_C(0x7fffffffffffffff))==UINT64_C(0x7ff0000000000000);};
    const bool ai=inf(a),bi=inf(b),ci=kind>=3 && inf(c);
    bool sign=av.sign;
    bool result_inf=false;
    if(kind<2){
        const bool bs=bv.sign^(kind==1);invalid=ai && bi && av.sign!=bs;
        result_inf=ai||bi;sign=ai?av.sign:bs;
    }else{
        const bool ps=av.sign^bv.sign,cs=cv.sign^(kind==4 || kind==5);
        invalid=(ai && !bv.m)||(bi && !av.m);
        invalid=invalid||((ai||bi)&&ci&&ps!=cs);
        result_inf=ai||bi||ci;sign=(ai||bi)?ps:cs;
    }
    if(invalid){spu_double_flag(f,lane,4);return UINT64_C(0x7ff8000000000000);}
    if(result_inf){if(kind>=5)sign=!sign;return (std::uint64_t(sign)<<63)|UINT64_C(0x7ff0000000000000);}
    SpuDoubleExact r;
    bool zero_sign=false;
    const bool down=spu_double_mode(f,lane)==3;
    if(kind<2){
        const bool bs=bv.sign^(kind==1);
        r=SpuDoubleExact::value(av.m,av.e,av.sign);r.add(SpuDoubleExact::value(bv.m,bv.e,bs));
        zero_sign=(!av.m && !bv.m && av.sign==bs)?av.sign:down;
    }else{
        const bool ps=av.sign^bv.sign,cs=cv.sign^(kind==4 || kind==5);
        r=SpuDoubleExact::value(__uint128_t(av.m)*bv.m,av.e+bv.e,ps);
        if(kind>=3)r.add(SpuDoubleExact::value(cv.m,cv.e,cs));
        zero_sign=kind==2?ps:((!av.m||!bv.m)&&!cv.m&&ps==cs)?ps:down;
    }
    auto result=spu_ieee_round(r,f,lane,52,1023,zero_sign);
    if(kind>=5)result^=UINT64_C(0x8000000000000000); // Negate AFTER rounding (PDF211,214).
    return result;
}
[[maybe_unused]] static std::uint64_t spu_ieee_convert(SpuRegister& f,unsigned lane,std::uint64_t x,bool to_single){
    const unsigned fraction=to_single?52:23;const int bias=to_single?1023:127;
    const auto p=spu_ieee_parts(x,fraction,bias);
    const auto mask=(UINT64_C(1)<<fraction)-1;
    const bool special=((x>>fraction)&unsigned(2*bias+1))==unsigned(2*bias+1);
    if(special){
        if(x&mask){spu_double_flag(f,lane,2|((x&(UINT64_C(1)<<(fraction-1)))?0:4));return to_single?UINT64_C(0x7fc00000):UINT64_C(0x7ff8000000000000);}
        return to_single?((std::uint64_t(p.sign)<<31)|0x7f800000):((std::uint64_t(p.sign)<<63)|UINT64_C(0x7ff0000000000000));
    }
    return spu_ieee_round(SpuDoubleExact::value(p.m,p.e,p.sign),f,lane,to_single?23:52,to_single?127:1023,p.sign);
}
[[maybe_unused]] static std::uint64_t spu_double_compare(SpuRegister& f,unsigned lane,std::uint64_t a,std::uint64_t b,bool magnitude,bool equal){
    if(spu_double_nan(a)||spu_double_nan(b)){spu_double_flag(f,lane,4);return 0;}
    return spu_fp_compare(a,b,magnitude,equal,63)?UINT64_MAX:0;
}
[[maybe_unused]] static std::uint64_t spu_double_test(std::uint64_t x,unsigned mask){
    const auto magnitude=x&UINT64_C(0x7fffffffffffffff);const bool sign=x>>63;
    const unsigned category=magnitude>UINT64_C(0x7ff0000000000000)?64:magnitude==UINT64_C(0x7ff0000000000000)?(sign?16:32):!magnitude?(sign?4:8):magnitude<UINT64_C(0x10000000000000)?(sign?1:2):0;
    return (mask&category)?UINT64_MAX:0;
}

// Cell BE Handbook v1.1 PDF pp.448-449,483-484: decrementers use the
// PPE timebase, run modulo 2^32, latch on 0 -> -1 (including long gaps).
// Initial values/options: RPCS3 ffba2bf0 SPUThread.cpp:1343-1345.
template<class Thread> static void spu_reset_decrementer(Thread& t,std::uint64_t now){
    t.decrementer_tick=now;t.decrementer=(t.options&2)?~std::uint32_t(now):0;
    t.decrementer_running=true;
}
template<class Thread> static void spu_update_decrementer(Thread& t,std::uint64_t now){
    if(!t.decrementer_running)return;
    const auto elapsed=now-t.decrementer_tick;
    if(elapsed>t.decrementer){t.event_status|=0x20;if(t.event_mask&0x20)t.event_ready=true;}
    t.decrementer-=std::uint32_t(elapsed);t.decrementer_tick=now;
}
// With interrupts enabled every executed instruction samples the external condition here, so the
// common path must not read the host clock. Deferred decrementer accounting is exact: one later update
// over the whole interval latches the same underflow event and the same modulo-2^32 value. The clock is
// read only while TM (0x20) is enabled, or with full=true before channels that read the value (8),
// change the mask/acknowledge (1/2) or reload the decrementer (7).
//
// Reservation-lost check. With concurrent guest memory the full compare is GuestMemory::reservation_token():
// reader admission, stripe lock, map lookup and tracking state. Pool lanes repeat this check on every
// GETLLAR/PUTLLC, rchcnt/rdch of channel 0, branch-on-event and slice entry, and it contends for the very
// stripe a worker's PUTLLUC or a PPU store needs. The filter keeps the result exact: notify() (par_wait.py)
// publishes every bump of a tracked granule and every map-epoch change, and the counters observed BEFORE the
// last successful full compare prove that nothing was published since (proof at
// ReservationNotifications::touched). The full compare runs on the first check, after any published change
// (hash collisions included) and for a different granule or memory; serial memory always compares fully.
// One effect of the compare is not a published change: a synchronous host-access observer (RSX/GPU
// coherence) materializes deferred writes when a CPU access reaches its range, and that write is what loses
// the reservation. A reservation line such an observer covers therefore always takes the full compare.
static void spu_collect_events(PpuState& s,NativeSpu::Thread& t,bool full=false){
    spu_stat(t,12);
    unsigned produced=0;
    const auto take=[](auto& flag){return flag.load(std::memory_order_relaxed) && flag.exchange(false,std::memory_order_acq_rel);};
    if(take(t.in_mailbox.state->event))produced|=0x10;
    for(unsigned i=0;i<2;++i)if(take(t.snr[i].state->event))produced|=i?0x100:0x200;
    t.event_status|=produced;if(produced&t.event_mask)t.event_ready=true;
    if(t.decrementer_running && (full || (t.event_mask&0x20)))spu_update_decrementer(t,timebase_ticks());
    if(t.reservation_address!=UINT64_MAX){
        auto& memory=s.memory;const bool concurrent=memory.concurrent();
        if(__builtin_expect(concurrent && t.reservation_observed_valid && t.reservation_observed_pointer==t.reservation.first &&
            t.reservation_observed_memory==memory.parallel->identity &&
            memory.parallel->notifications->observe(t.reservation.first)==t.reservation_observed &&
            (!memory.host_access_observer || !memory.host_access_observer.synchronous(reinterpret_cast<const void*>(t.reservation.first),128)),1))return;
        // The token compare reads guest memory; give observers the SPU origin (pc, retired steps).
        HostAccessContext event_origin(HostAccessOrigin::Engine::Spu,t.id,t.pc,&t.steps);
        // Sample first, compare second: a bump after the sample either shows in the compare (the version
        // is read under the stripe lock) or publishes later and invalidates the sample.
        const auto observed=concurrent?memory.parallel->notifications->observe(t.reservation.first):std::array<std::uint64_t,3>{};
        if(t.reservation!=s.memory.reservation_token(t.reservation_address)){
            t.reservation_observed_valid=false;
            t.reservation_address=UINT64_MAX;t.event_status|=0x400;
            if(t.event_mask&0x400)t.event_ready=true;
        }else if(concurrent){
            t.reservation_observed=observed;t.reservation_observed_pointer=t.reservation.first;
            t.reservation_observed_memory=memory.parallel->identity;t.reservation_observed_valid=true;
        }
    }
}
// ---- Busy-poll parking (pool lanes only) ----
// A pool lane that spins on an idempotent poll burns a host core and, for GETLLAR, contends for the stripe a
// worker's PUTLLUC needs. The detector recognizes two polls and delays the retry of the SAME instruction
// until the polled state can have changed:
//   * GETLLAR of the line whose reservation this SPU still holds (spu_collect_events has just proved the
//     line unwritten, so the read would return the same 128 bytes and re-arm the same reservation);
//   * rchcnt of channel 0 reporting no event.
// Only a fixed point parks: a window of >= SPU_POLL_REPEATS consecutive polls at the same site (kind, EA, PC),
// with the register file identical to the previous visit of that site and no channel traffic besides
// idempotent setup (DMA parameter/tag-mask/event-mask/ack writes, atomic-status and decrementer reads) in
// between. Then only external state decides what the next iteration does. The park throws SpuBlocked from
// the instruction BEFORE any side effect (pc stays on it, nothing retires); spu_lane_prepare_wait arms the
// wake sources (write to the polled line, mailbox/signal, decrementer deadline) and a short timeout. A park
// therefore only delays the retry and never changes a result. Serial spu_run never sets enabled.
static constexpr unsigned SPU_POLL_REPEATS=16,SPU_POLL_WINDOW_MAX=1024;
// ANYPS3_SPU_POLL_PARK=0 disables parking (diagnostics/A-B runs). Mutable for tests.
inline std::atomic<bool> spu_poll_park_on{[]{const char* v=std::getenv("ANYPS3_SPU_POLL_PARK");return !v || std::string(v)!="0";}()};
// A confirmed fixed point keeps polling for this long before the first park: a dependency that resolves
// within one thread park/wake round trip is cheaper to keep polling than to sleep on (a park adds the host
// wake latency to the poll). Mutable for tests.
inline std::atomic<std::uint64_t> spu_poll_spin_ns{[]()->std::uint64_t{
    const char* v=std::getenv("ANYPS3_SPU_POLL_SPIN_US");
    if(!v || !*v)return 50000;
    char* end=nullptr;const std::uint64_t us=std::strtoull(v,&end,10);
    return (*end || us>10000)?50000:us*1000;}()};
// First park timeout and its cap; consecutive parks without a relevant change double the timeout. Writes to
// the watched line, mailbox/signal pushes and owner scopes (LS writes, peer DMA, group control) wake a parked
// lane at once; the timeout only bounds state that nothing signals. Mutable for tests.
inline std::atomic<std::uint64_t> spu_poll_park_ns{100000},spu_poll_park_max_ns{1600000};
[[maybe_unused]] static void spu_poll_reset(NativeSpu::Thread& t){
    auto& p=t.idle_poll;p.count=0;p.parks=0;p.window=SPU_POLL_REPEATS;p.resume_pass=false;p.spinning=false;p.sites[0].valid=false;p.sites[1].valid=false;
}
[[maybe_unused]] static void spu_poll_note(NativeSpu::Thread& t,NativeSpu::Thread::PollState::Kind kind,std::uint64_t address){
    auto& p=t.idle_poll;if(!p.enabled)return;
    // The retry after a park executes the poll for real once, whatever woke it: only then can state the
    // park does not watch (a timeout wake) be observed. The next iteration reaches this note again and,
    // while still at the fixed point, parks again.
    if(p.resume_pass){p.resume_pass=false;return;}
    if(!p.window)p.window=SPU_POLL_REPEATS;
    ++p.count;
    using Site=NativeSpu::Thread::PollState::Site;
    const auto remember=[&](Site& x){x.kind=kind;x.address=address;x.pc=t.pc;x.regs=t.gpr;x.fpscr=t.fpscr;x.valid=true;};
    Site* site=nullptr;
    for(auto& x:p.sites)if(x.valid && x.kind==kind && x.address==address && x.pc==t.pc){site=&x;break;}
    if(!site){remember(p.sites[p.next_site]);p.next_site^=1;return;}
    // Snapshot one poll before the decision so start-up transients are gone; a mismatch refreshes it and
    // doubles the window, so a loop with a changing register (iteration counter, decrementer value) pays a
    // 2 KB copy/compare per window and never parks.
    if(p.count+1==p.window)remember(*site);
    if(p.count<p.window)return;
    if(std::memcmp(site->regs.data(),t.gpr.data(),sizeof(t.gpr)) || site->fpscr!=t.fpscr){
        remember(*site);p.count=0;p.spinning=false;p.window=std::min(p.window*2,SPU_POLL_WINDOW_MAX);return;
    }
    // Fixed point confirmed: spin for spu_poll_spin_ns first (once per episode; later parks of the same
    // episode go straight to sleep), then park.
    const auto now=std::chrono::steady_clock::now();
    if(!p.spinning){p.spinning=true;p.spin_start=now;}
    if(now-p.spin_start<std::chrono::nanoseconds(spu_poll_spin_ns.load(std::memory_order_relaxed)))return;
    p.count=p.window;++p.parks;++p.total_parks;p.parked=true;p.resume_pass=true;p.parked_kind=kind;throw SpuBlocked{};
}
void spu_interpret(PpuState&,NativeSpu::Thread&,std::uint8_t*);
[[noreturn]] void spu_dynamic_fault(const NativeSpu::Thread&,const std::uint8_t*,const char*);
void spu_ls_write(NativeSpu::Thread&,unsigned,unsigned,std::uint64_t);
static inline void spu_ls_forget(NativeSpu::Thread& t,unsigned base,unsigned size){
    if(!t.dma_writes.empty())spu_ls_write(t,base,size,UINT64_MAX);
}
// Diagnostic capture is TLS and disabled by default. It observes one scheduler
// slice, including attempted blocking instructions and the actual DMA payloads.
// Unsupported external stimuli are recorded as such, never fabricated for replay.
struct SpuCapture {
    FILE* file=nullptr;bool first=true;
    void event(){if(!first)std::fputc(',',file);first=false;}
    static void hex(FILE* f,const std::uint8_t* p,std::size_t n){
        static constexpr char digits[]="0123456789abcdef";std::fputc('"',f);
        for(std::size_t i=0;i<n;++i){std::fputc(digits[p[i]>>4],f);std::fputc(digits[p[i]&15],f);}std::fputc('"',f);
    }
    static void state(FILE* f,const NativeSpu::Thread& t,const std::uint8_t* ls){
        std::fputs("{\"scalar\":[",f);bool first=true;
        auto value=[&](std::uint64_t v){if(!first)std::fputc(',',f);first=false;std::fprintf(f,"%" PRIu64,v);};
        value(t.pc);value(t.entry);value(t.image_index);value(t.steps);value(t.interrupts_enabled);
        value(t.srr0_valid);value(t.srr0_pending);value(t.srr0);value(t.srr0_write);
        value(t.decrementer);value(t.decrementer_tick);value(t.decrementer_running);
        value(t.tag_mask);value(t.tag_status);value(t.tag_status_ready);value(t.out_mailbox);value(t.out_mailbox_full);
        value(t.atomic_status);value(t.atomic_status_ready);value(t.event_mask);value(t.event_status);
        value(t.event_ready);value(t.waiting_event);value(t.waiting_signal);value(t.stopped);value(t.exit_status);
        value(t.exit_status_ready);value(t.waiting_receive);value(t.receive_key);value(t.control_generation);
        value(t.signal_config);value(t.signals[0]);value(t.signals[1]);value(t.signal_ready[0]);value(t.signal_ready[1]);
        std::fputs("],\"dma\":[",f);for(unsigned i=0;i<5;++i)std::fprintf(f,"%s%u",i?",":"",t.dma[i]);
        std::fputs("],\"gpr\":[",f);for(unsigned i=0;i<128;++i){if(i)std::fputc(',',f);hex(f,t.gpr[i].data(),16);}
        std::fputs("],\"fpscr\":",f);hex(f,t.fpscr.data(),16);std::fputs(",\"ls\":",f);hex(f,ls,0x40000);
        {std::lock_guard<std::mutex> lock(t.in_mailbox.state->mutex);std::fputs(",\"inbox\":[",f);
            for(unsigned i=0;i<t.in_mailbox.state->values.size();++i)std::fprintf(f,"%s%u",i?",":"",t.in_mailbox.state->values[i]);}
        std::fputs("],\"async\":[",f);first=true;value(t.in_mailbox.state->event.load());
        for(const auto& signal:t.snr){value(signal.state->data.load());value(signal.state->event.load());}std::fputs("]}",f);
    }
};
inline thread_local SpuCapture spu_capture;
[[maybe_unused]] static void spu_capture_unsupported(const char* reason){
    if(!spu_capture.file)return;
    spu_capture.event();std::fprintf(spu_capture.file,"{\"kind\":\"unsupported\",\"reason\":\"%s\"}",reason);
}
[[maybe_unused]] static void spu_trace_fetch(const NativeSpu::Thread& t,const std::uint8_t* ls,std::uint64_t step){
    if(spu_capture.file){spu_capture.event();std::fprintf(spu_capture.file,
        "{\"kind\":\"fetch\",\"pc\":%u,\"word\":%u,\"step\":%" PRIu64 "}",t.pc,spu_load_word(ls+t.pc),step);}
    if(!spu_capture.file || anyps3_trace_enabled("ANYPS3_SPU_TRACE"))
        std::fprintf(stderr,"SPU trace id=%x pc=%05x step=%" PRIu64 "\n",t.id,t.pc,step);
}
struct SpuCaptureScope {
    NativeSpu::Thread& thread;const std::uint8_t* ls;bool active=false;
    static std::uint64_t option(const char* name,std::uint64_t fallback){const auto* p=std::getenv(name);if(!p)return fallback;
        char* end=nullptr;const auto value=std::strtoull(p,&end,0);if(!*p || *end || *p=='-')throw std::runtime_error("invalid SPU capture option");return value;}
    explicit SpuCaptureScope(NativeSpu::Thread& t,const std::uint8_t* data,unsigned budget):thread(t),ls(data){
        static const char* path=std::getenv("ANYPS3_SPU_CAPTURE");if(!path || !*path)return;
        if(t.id!=option("ANYPS3_SPU_CAPTURE_ID",t.id) || t.steps<option("ANYPS3_SPU_CAPTURE_MIN_STEPS",0) ||
           t.pc!=option("ANYPS3_SPU_CAPTURE_PC",t.pc))return;
        if(const auto* arm=std::getenv("ANYPS3_SPU_CAPTURE_ARM")){auto* gate=std::fopen(arm,"rb");if(!gate)return;std::fclose(gate);}
        static std::atomic<bool> claimed{false};if(claimed.exchange(true))return;
        auto* f=std::fopen(path,"wx");if(!f){std::fprintf(stderr,"SPU capture cannot create new file: %s\n",path);return;}
        active=true;spu_capture.file=f;spu_capture.first=true;
        std::fprintf(f,"{\"schema\":1,\"id\":%u,\"budget\":%u,\"initial\":",t.id,budget);SpuCapture::state(f,t,ls);
        std::fputs(",\"events\":[",f);
        if(t.interrupts_enabled || t.reservation_address!=UINT64_MAX)spu_capture_unsupported("active interrupts or reservation at slice entry");
    }
    ~SpuCaptureScope(){if(!active)return;auto* f=spu_capture.file;
        std::fputs("],\"final\":",f);SpuCapture::state(f,thread,ls);std::fputs("}\n",f);
        const bool failed=std::ferror(f);const int closed=std::fclose(f);spu_capture.file=nullptr;
        std::fprintf(stderr,"SPU capture %s id=%x pc=%05x steps=%" PRIu64 "\n",failed || closed?"I/O error":"complete",thread.id,thread.pc,thread.steps);
    }
};
// Local counters avoid publishing steps/budget on every instruction. RAII also
// publishes the retired prefix on a blocking channel, STOP, or runtime error.
// limit: soft slice end (a superblock may start below it and overshoot it);
// hard: exact distance to the step limit; fast: entries that need no exact check.
struct SpuBlockScope {
    NativeSpu::Thread& thread;unsigned executed=0,limit,hard,fast;std::uint64_t generation;HostAccessContext origin;
    explicit SpuBlockScope(NativeSpu::Thread& t):thread(t),limit(static_cast<unsigned>(std::min<std::uint64_t>(t.block_budget,t.block_limit-t.steps))),
        hard(static_cast<unsigned>(std::min<std::uint64_t>(t.block_limit-t.steps,UINT32_MAX))),fast(t.block_exact?0u:std::min(limit,hard>=64u?hard-63u:0u)),
        generation(t.control_generation),origin(HostAccessOrigin::Engine::Spu,t.id,t.pc,&t.steps,&executed){}
    ~SpuBlockScope(){if(thread.control_generation==generation){thread.steps+=executed;thread.published_steps.value.store(thread.steps,std::memory_order_relaxed);thread.block_budget-=std::min(executed,thread.block_budget);}}
};

// SPU ISA v1.2 PDF pp.178-181,251-252; Cell BE Handbook v1.1 pp.470-471.
// External condition = nonzero SPU_RdEventStat channel count. Taking an
// interrupt does not consume that channel; the handler must read/ack it.
[[maybe_unused]] static bool spu_external_event(PpuState& s,NativeSpu::Thread& t){
    spu_collect_events(s,t);return t.event_ready;
}
[[maybe_unused]] static void spu_branch_interrupts(NativeSpu::Thread& t,unsigned flags){
    if(flags==3)throw std::runtime_error("reserved SPU branch D=E=1");
    if(flags)t.interrupts_enabled=flags==1;
    t.irq_next=0;
}
[[maybe_unused]] static void spu_sync_srr0(NativeSpu::Thread& t){
    if(t.srr0_pending){t.srr0=t.srr0_write;t.srr0_pending=false;t.srr0_valid=true;}
}
[[maybe_unused]] static std::uint32_t spu_interrupt_return(const NativeSpu::Thread& t){
    if(t.srr0_pending)throw std::runtime_error("SPU iret requires syncc after wrch SRR0");
    if(!t.srr0_valid)throw std::runtime_error("SPU iret requires initialized SRR0");
    return t.srr0&0x3fffc;
}
[[maybe_unused]] static void spu_take_interrupt(PpuState& s,NativeSpu::Thread& t){
    if(!t.interrupts_enabled || !spu_external_event(s,t))return;
    t.srr0=t.pc;t.srr0_valid=true;t.srr0_pending=false;
    t.interrupts_enabled=false;t.pc=0;
    // The blocked instruction is retried on iret. Waiting flags must not
    // prevent the scheduler from executing the handler at LS zero.
    t.waiting_event=false;t.waiting_signal=0;
}
// SPU thread group EA window (LV2): 0xf0000000 + spu_num*0x100000 maps that thread's LS,
// +0x5400c/+0x5c00c its SNR1/SNR2 (RPCS3 ffba2bf0 SPUThread.cpp:2066-2127, SPUThread.h:144-146).
// Raw SPU MMIO (0xe0000000..) and other window registers stop explicitly.
// Fast DMA uses the memory owner's current index caches, never a retained raw
// pointer. Bounds/permissions are checked once and writes keep alias versions
// and GPU observers. Split spans retain full prevalidation in GuestMemory.
static void spu_transfer_main(GuestMemory& memory,std::uint64_t address,std::uint8_t* local,unsigned size,bool put){
    if(memory.concurrent()){memory.transfer(address,local,size,put);return;}
    if(address>UINT32_MAX || size>UINT64_C(0x100000000)-address)
        throw std::runtime_error("guest transfer outside 32-bit space");
    if(!size)return;
    auto hit=[&](std::size_t index){
        if(index>=memory.regions.size())return false;
        const auto& region=memory.regions[index];
        if(address<region.base || address-region.base>=region.bytes->size() || size>region.bytes->size()-(address-region.base))return false;
        if(!(region.flags&(put?2:4)))throw std::runtime_error("guest memory permission fault");
        if(put && (region.flags&1))throw std::runtime_error("self-modifying code is unsupported");
        auto* host=region.bytes->data()+address-region.base;
        memory.last_region=index;memory.page_regions[address>>12]=static_cast<std::uint32_t>(index+1);
        if(put){memory.mark_write(host,size);std::memcpy(host,local,size);}
        else{memory.notify_host(host,size,false);std::memcpy(local,host,size);}
        return true;
    };
    const auto cached=memory.page_regions[address>>12];
    if(cached && hit(cached-1))return;
    if(hit(memory.last_region))return;
    // A cold or split span is rare; the established transfer validates all
    // mappings before touching bytes or reservation versions.
    memory.transfer(address,local,size,put);
}
static void spu_transfer(PpuState&,NativeSpu::Thread&,std::uint64_t,std::uint8_t*,std::uint32_t,bool);
static void spu_transfer_impl(PpuState& s,NativeSpu::Thread& t,std::uint64_t address,std::uint8_t* local,std::uint32_t size,bool put){
    if(address>=0xe0000000u && NativeSpuWorker::current && NativeSpuWorker::current->parent){
        if(spu_owner_call(s,t,[&]{spu_transfer(s,t,address,local,size,put);},{SpuOwnerOperation::Transfer,address,size,put}))return;
    }
    if(address<0xe0000000u){spu_transfer_main(s.memory,address,local,size,put);if(spu_profile_on){spu_profile.dma_bytes+=size;++spu_profile.dma_elements;}return;}
    if(address<0xf0000000u)throw std::runtime_error("SPU DMA to raw SPU MMIO is not implemented");
    const auto index=static_cast<std::uint32_t>((address-0xf0000000u)/0x100000),offset=static_cast<std::uint32_t>((address-0xf0000000u)%0x100000);
    for(auto& group:s.spu.groups)for(std::size_t i=0;i<group.threads.size();++i){
        if(&group.threads[i]!=&t)continue;
        for(std::size_t j=0;j<group.threads.size();++j){
            auto& target=group.threads[j];
            if(target.number!=index)continue;
            if(offset<0x40000 && size<=0x40000-offset){
                auto* other=group.context.data()+j*0x40000+offset;
                if(put){std::memmove(other,local,size);spu_ls_forget(target,offset,size);}else std::memmove(local,other,size);
                if(spu_profile_on){spu_profile.dma_bytes+=size;++spu_profile.dma_elements;}
                return;
            }
            if(put && size==4 && (offset==0x5400c || offset==0x5c00c)){
                const unsigned number=offset==0x5c00c;const auto value=spu_load_word(local);
                if(s.process.spu_worker.pool_mode){target.snr[number].push(value,(target.signal_config&(1u<<number))!=0);return;}
                if(!target.signal_ready[number])target.snr[number].state->event.store(true,std::memory_order_release);
                target.signals[number]=(target.signal_config&(1u<<number))?target.signals[number]|value:value;
                if(spu_profile_on){spu_profile.dma_bytes+=size;++spu_profile.dma_elements;}
                target.signal_ready[number]=true;return;
            }
            throw std::runtime_error("unsupported SPU thread group window offset "+std::to_string(offset));
        }
        throw std::runtime_error("SPU thread group window names no thread in this group");
    }
    throw std::runtime_error("SPU thread group window used outside an SPU thread group");
}
static void spu_transfer(PpuState& s,NativeSpu::Thread& t,std::uint64_t address,std::uint8_t* local,std::uint32_t size,bool put){
    spu_transfer_impl(s,t,address,local,size,put);
    if(spu_capture.file){spu_capture.event();std::fprintf(spu_capture.file,"{\"kind\":\"dma\",\"ea\":%" PRIu64 ",\"put\":%s,\"data\":",address,put?"true":"false");
        SpuCapture::hex(spu_capture.file,local,size);std::fputc('}',spu_capture.file);}
}
// Write the existing shared circular trace directly. Fixed-width hex fields
// avoid two printf formatters and their scans/locale work on every DMA command.
// Dumps and ANYPS3_SPU_TRACE_DMA=1 still use the same shared history.
static void spu_trace_dma(const NativeSpu::Thread& t,const char* operation,std::uint64_t address,unsigned local,unsigned size,unsigned tag,unsigned end=UINT32_MAX){
    std::lock_guard<std::mutex> trace_lock(anyps3_trace_history.mutex);
    auto* p=anyps3_trace_history.lines[anyps3_trace_history.written++%Anyps3TraceHistory::capacity].data();
    auto* const begin=p;
    auto text=[&](const char* s){while(*s)*p++=*s++;};
    auto hex=[&](std::uint64_t v,unsigned digits){static constexpr char alphabet[]="0123456789abcdef";for(unsigned n=digits;n--;) *p++=alphabet[(v>>(n*4))&15];};
    text("SPU DMA ");text(operation);
    if(end==UINT32_MAX){text(" ea=");hex(address,16);text(" ls=");hex(local,5);text(" size=");hex(size,4);}
    else{text(" list=");hex(address,5);text(" size=");hex(size,4);text(" ls=");hex(local,5);text("..");hex(end,5);text(" ea_high=");hex(t.dma[1],8);}
    text(" tag=");hex(tag,2);text(" spu=");hex(t.id,8);text(" pc=");hex(t.pc,5);*p++='\n';*p=0;
    static const bool live=anyps3_trace_enabled("ANYPS3_SPU_TRACE_DMA");
    if(live)std::fputs(begin,stderr);
}
// LV2 STOP services: RPCS3 ffba2bf0 SPUThread.cpp:6506-6997,
// lv2/sys_spu.h:75-82. No instruction decoding or service guessing here.
[[maybe_unused]] static NativeSpu::Group& spu_owner(PpuState& s,NativeSpu::Thread& t){
    for(auto& g:s.spu.groups)for(auto& thread:g.threads)if(&thread==&t)return g;
    throw std::runtime_error("LV2 SPU STOP outside a managed thread group");
}
[[maybe_unused]] static void spu_stop(PpuState& s,NativeSpu::Thread& t,unsigned code){
    if(code!=1 && code!=0x100)spu_capture_unsupported("LV2 STOP service requires external group state");
    if(NativeSpuWorker::current && NativeSpuWorker::current->parent && code!=1 && code!=0x100){
        if(spu_owner_call(s,t,[&]{spu_stop(s,t,code);},{SpuOwnerOperation::Stop,code}))return;
    }
    SpuProfileScope profile(SpuProfile::STOP);
    if(code==1){t.pc=(t.pc+4)&0x3fffc;throw SpuSliceEnd{};}
    // STOP 2 is RPCS3's internal fast-call return. There is no fast-call
    // continuation in this runtime, so it cannot count as a guest thread exit.
    if(code==2)throw std::runtime_error("SPU STOP 2 requires a fast-call continuation");
    if(code==0x100){
        if(t.out_mailbox_full)throw std::runtime_error("SPU yield requires empty outbound mailbox");
        t.pc=(t.pc+4)&0x3fffc;throw SpuSliceEnd{};
    }
    if(code!=0x101 && code!=0x102 && code!=0x110 && code!=0x111)
        throw std::runtime_error("unimplemented SPU STOP service "+std::to_string(code));
    auto& g=spu_owner(s,t);
    if(code==0x101 || code==0x102){
        if(!t.out_mailbox_full)throw std::runtime_error("SPU exit requires outbound mailbox status");
        const auto status=t.out_mailbox;t.out_mailbox_full=false;
        if(code==0x101){
            for(auto& thread:g.threads){thread.stopped=true;thread.waiting_receive=false;}
            g.state=1;g.join_cause=1;g.exit_status=status;
        }else{
            t.stopped=true;t.exit_status_ready=true;t.exit_status=status;
            if(std::all_of(g.threads.begin(),g.threads.end(),[](const auto& x){return x.stopped && x.exit_status_ready;})){
                g.state=1;g.join_cause=2;g.exit_status=0;
            }
        }
        t.pc=(t.pc+4)&0x3fffc;throw SpuSliceEnd{};
    }
    if(!t.waiting_receive){
        if(!t.out_mailbox_full)throw std::runtime_error("SPU receive requires outbound mailbox queue key");
        t.receive_key=t.out_mailbox;
        if(!t.in_mailbox.empty()){
            // RECEIVE reads before its pop guard is installed; TRYRECEIVE
            // pops first. Keep this observable mailbox difference on EBUSY.
            if(code==0x111)t.out_mailbox_full=false;
            t.in_mailbox={MM_EBUSY};return;
        }
        t.out_mailbox_full=false;
        if(code==0x110 && g.type==0x18){t.in_mailbox={MM_EINVAL};return;}
    }
    std::uint32_t qid=0;
    for(const auto& b:t.queue_bindings)if(b.second && b.first==t.receive_key)qid=b.second;
    auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& x){return qid && x.id==qid;});
    if(q==s.events.queues.end()){
        t.in_mailbox={t.waiting_receive?0x80010013u:MM_EINVAL};t.waiting_receive=false;g.receive_thread=UINT32_MAX;return;
    }
    if(q->data.empty()){
        if(code==0x111){t.in_mailbox={MM_EBUSY};return;}
        t.waiting_receive=true;g.state=3;g.receive_thread=t.id;throw SpuBlocked{};
    }
    const auto event=q->data.front();q->data.erase(q->data.begin());
    t.in_mailbox={0,std::uint32_t(event[1]),std::uint32_t(event[2]),std::uint32_t(event[3])};
    t.waiting_receive=false;g.receive_thread=UINT32_MAX;
}
// MFC list elements [at,end) of tag (Cell BE Handbook v1.1 19.4.4, PDF p.530; CBEA v1.02 9.1.4). After an element with the
// stall-and-notify bit, if elements remain, the list stalls: tag added to the stall mask/status, SN event when the status
// was empty, rest kept until MFC_WrListStallAck (RPCS3 ffba2bf0 SPUThread.cpp do_list_transfer). Returns false if stalled.
static bool spu_list_run(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls,unsigned tag,bool list_put,std::uint64_t high,
                         std::uint32_t at,std::uint32_t end,std::uint32_t& local){
    for(;at<end;at+=8){
        const auto lts=spu_load_word(ls+at),leal=spu_load_word(ls+at+4);
        if(lts&0x7fff8000u)throw std::runtime_error("SPU DMA list element reserved bits set");
        const std::uint32_t size=lts&0x7fff;const std::uint64_t address=high|leal;
        if(size>0x4000 || local>0x40000 || size>0x40000-local)throw std::runtime_error("invalid SPU DMA list element range");
        if(size){
            const auto alignment=size<16?size:16;
            if((alignment&(alignment-1)) || size%alignment || (local|address)%alignment || (local&15)!=(address&15))
                throw std::runtime_error("misaligned SPU DMA list element");
            spu_transfer(s,t,address,ls+local,size,list_put);if(!list_put)spu_ls_write(t,local,size,address);
        }
        local+=size;if(size<16)local=(local+15)&~15u; // short elements resume on a quadword
        if((lts&0x80000000u) && at+8<end){
            spu_capture_unsupported("DMA list stall-and-notify");
            auto& stalled=t.stalled_lists[tag];stalled.active=true;stalled.put=list_put;stalled.high=high;
            stalled.next=at+8;stalled.end=end;stalled.local=local;
            t.stall_mask|=1u<<tag;
            if(!t.stall_stat_ready){t.event_status|=0x2;if(t.event_mask&0x2)t.event_ready=true;}
            t.stall_stat|=1u<<tag;t.stall_stat_ready=true;
            return false;
        }
    }
    t.stalled_lists[tag].active=false;return true;
}
static std::uint32_t spu_incomplete_tags(const NativeSpu::Thread& t){
    std::uint32_t mask=0;for(unsigned i=0;i<32;++i)if(t.stalled_lists[i].active)mask|=1u<<i;return mask;
}
// Tag update request (channel 23): 0 immediate, 1 ANY, 2 ALL. Only stalled lists are incomplete; other transfers
// complete synchronously. An unsatisfied ANY/ALL stays pending until a stall is acknowledged and its list completes.
static void spu_tag_update(NativeSpu::Thread& t,unsigned mode){
    const auto incomplete=spu_incomplete_tags(t),done=t.tag_mask&~incomplete;
    const bool satisfied=mode==0 || !t.tag_mask || (mode==1?done!=0:!(t.tag_mask&incomplete));
    if(satisfied){t.tag_status=done;t.tag_status_ready=true;t.tag_update_pending=0;}
    else{t.tag_status_ready=false;t.tag_update_pending=mode;}
}
[[maybe_unused]] static void spu_write_channel(PpuState& s,NativeSpu::Thread& t,std::uint8_t* ls,unsigned channel,std::uint32_t value){
    if(channel==1 || channel==2 || channel==7 || channel==30 ||
       (channel==21 && (value==0xd0 || value==0xb0 || value==0xb4)))
        spu_capture_unsupported("external channel write or reservation DMA");
    // Only idempotent setup (DMA parameters, tag mask, event mask/ack) and the polling GETLLAR itself leave
    // the busy-poll detector alive; any other write is externally visible activity and ends the candidate poll.
    if(!(channel>=16 && channel<=20) && channel!=22 && channel!=1 && channel!=2 && !(channel==21 && value==0xd0))spu_poll_reset(t);
    if(channel==30 && NativeSpuWorker::current && NativeSpuWorker::current->parent){
        const unsigned code=value>>24,port=code&63;
        // Real connect syscalls pin the queue. Legacy fixtures which assign a
        // numeric port directly retain owner lookup, with no registry race.
        std::shared_ptr<EventQueueState> pinned;bool connected=false;
        if(code<128){auto* group=NativeSpuWorker::current->group;std::lock_guard<std::recursive_mutex> lock(*group->metadata_mutex);pinned=t.event_targets[port];connected=t.ports[port]!=0;}
        if(code<128 && (pinned || !connected)){
            SpuProfileScope profile(SpuProfile::CHANNEL);
            if(!t.out_mailbox_full)throw std::runtime_error("SPU event requires outbound mailbox data");
            const bool reply=code<64;
            if(reply && !t.in_mailbox.reserve_reply()){t.out_mailbox_full=false;return;}
            auto target=std::move(pinned);
            const auto result=target?target->send({UINT64_C(0xffffffff53505501),t.id,(std::uint64_t(port)<<32)|(value&0xffffff),t.out_mailbox}):0x80010016u;
            std::fprintf(stderr,"SPU event id=%x port=%u data=%06x:%08x result=%08x reply=%u\n",t.id,port,value&0xffffff,t.out_mailbox,result,unsigned(reply));
            t.out_mailbox_full=false;if(reply)t.in_mailbox.finish_reply(result);return;
        }
        if(spu_owner_call(s,t,[&]{spu_write_channel(s,t,ls,channel,value);},{SpuOwnerOperation::Channel,channel,value}))return;
    }
    SpuProfileScope profile(SpuProfile::CHANNEL);spu_stat(t,13);t.irq_next=0;
    if(channel==7){
        spu_collect_events(s,t,true);
        if(!(t.decrementer&0x80000000u) && (value&0x80000000u)){
            t.event_status|=0x20;if(t.event_mask&0x20)t.event_ready=true;
        }
        t.decrementer=value;t.decrementer_tick=timebase_ticks();t.decrementer_running=true;return;
    }
    if(channel==14){
        if(t.interrupts_enabled)throw std::runtime_error("SPU wrch SRR0 while interrupts enabled is undefined");
        t.srr0_write=value;t.srr0_pending=true;return;
    }
    if(channel==28){
        if(t.out_mailbox_full)throw SpuBlocked{};
        t.out_mailbox=value;t.out_mailbox_full=true;return;
    }
    if(channel==30){
        const unsigned code=value>>24,port=code&63;
        if(code>=128)throw std::runtime_error("unimplemented SPU interrupt mailbox command "+std::to_string(code));
        if(!t.out_mailbox_full)throw std::runtime_error("SPU event requires outbound mailbox data");
        const bool reply=code<64;
        if(reply && !t.in_mailbox.empty()){
            t.out_mailbox_full=false;t.in_mailbox={MM_EBUSY};return;
        }
        auto queue=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return t.ports[port] && q.id==t.ports[port];});
        // RPCS3 ffba2bf0 sys_event.cpp:154-163, SPUThread.cpp:6119-6158:
        // EBUSY is a completed send failure, not the internal EAGAIN retry.
        const std::uint32_t result=queue==s.events.queues.end()?0x80010016:
            queue->data.send({UINT64_C(0xffffffff53505501),t.id,(std::uint64_t(port)<<32)|(value&0xffffff),t.out_mailbox});
        std::fprintf(stderr,"SPU event id=%x port=%u data=%06x:%08x result=%08x reply=%u\n",t.id,port,value&0xffffff,t.out_mailbox,result,unsigned(reply));
        t.out_mailbox_full=false;
        if(reply)t.in_mailbox.push_back(result);
        return;
    }
    if(channel==1 || channel==2){
        if(channel==1 && (value&~0x422u))throw std::runtime_error("unimplemented SPU event mask "+std::to_string(value));
        spu_collect_events(s,t,true);
        if(channel==1)t.event_mask=value;else t.event_status&=~value;
        if(channel==2 && (value&0x20) && !(t.event_mask&0x20))t.decrementer_running=false;
        if(t.event_status&t.event_mask)t.event_ready=true;
        return;
    }
    if(channel>=16 && channel<=20){t.dma[channel-16]=value;return;}
    if(channel==22){t.tag_mask=value;return;}
    if(channel==23){
        if(value>2)throw std::runtime_error("invalid SPU tag update mode");
        spu_tag_update(t,value);return;
    }
    if(channel==26){
        const auto tag=value&0x1f,bit=1u<<tag;
        if(!(t.stall_mask&bit))return; // RPCS3: acknowledging a non-stalled tag has no effect
        t.stall_mask&=~bit;auto& stalled=t.stalled_lists[tag];
        auto local=stalled.local;
        if(stalled.active && spu_list_run(s,t,ls,tag,stalled.put,stalled.high,stalled.next,stalled.end,local) && t.tag_update_pending)
            spu_tag_update(t,t.tag_update_pending);
        return;
    }
    if(channel==21){
        auto access_origin=HostAccessContext::sample();access_origin.engine=HostAccessOrigin::Engine::Dma;
        access_origin.guest_id=t.id;access_origin.guest_pc=t.pc;
        HostAccessContext dma_origin(access_origin);
        SpuProfileScope dma_profile(SpuProfile::DMA);
        if(spu_profile_on)++spu_profile.dma_commands;
        spu_stat(t,value==0xd0?3:value==0xb4?4:value==0xb0?5:16);
        if(value==0xd0 || value==0xb4 || value==0xb0){
            const auto address=(std::uint64_t(t.dma[1])<<32)|t.dma[2];
            const auto local=t.dma[0];
            if(address%128 || local%128 || local>0x3ff80)throw std::runtime_error("misaligned SPU atomic DMA");
            spu_collect_events(s,t);
            if(value==0xd0){
                // Re-reading the line whose reservation is still intact repeats the previous GETLLAR exactly;
                // a fixed-point spin on it parks here, before the read has any effect (see spu_poll_note).
                if(t.reservation_address==address)spu_poll_note(t,NativeSpu::Thread::PollState::Getllar,address);
                else spu_poll_reset(t);
                s.memory.getllar(address,ls+local,t.reservation);spu_ls_write(t,local,128,address);
                t.reservation_address=address;t.atomic_status=4;
                if(spu_profile_on){spu_profile.dma_bytes+=128;++spu_profile.dma_elements;}
            }else {
                // A reservation on this line that fails the store was lost to another writer after the event check
                // above; like RPCS3 ffba2bf0 SPUThread.cpp do_putllc failure ("Last check for event before we clear
                // the reservation") raise LR. PUTLLUC with that reservation first tries the conditional store.
                const bool reserved=t.reservation_address==address;
                bool success=false;
                if(value==0xb0){
                    if(reserved && s.memory.putllc(address,ls+local,t.reservation,false))success=true;
                    else{if(reserved){t.event_status|=0x400;if(t.event_mask&0x400)t.event_ready=true;}
                        success=s.memory.putllc(address,ls+local,t.reservation,true);}
                }else{
                    success=reserved && s.memory.putllc(address,ls+local,t.reservation,false);
                    if(reserved && !success){t.event_status|=0x400;if(t.event_mask&0x400)t.event_ready=true;}
                }
                if(success){if(spu_profile_on){spu_profile.dma_bytes+=128;++spu_profile.dma_elements;}}
                t.atomic_status=value==0xb0?2:success?0:1;t.reservation_address=UINT64_MAX;
            }
            t.atomic_status_ready=true;
            if(spu_dma_history_on)spu_trace_dma(t,value==0xd0?"GETLLAR":value==0xb0?"PUTLLUC":"PUTLLC",address,local,128,0);
            return;
        }
        // DMA lists (Cell BE Handbook v1.1 19.4.4, PDF p.530): MFC_EAL holds the LS address of 8-byte
        // {LTS, LEAL} elements, MFC_Size the list size. Transfers complete synchronously and in order,
        // so the barrier/fence forms (b/f) are satisfied as for single transfers. LSZ/8 elements
        // (CBEA v1.02 9.1.4): a zero list size transfers nothing.
        if((value>=0x44 && value<=0x46) || (value>=0x24 && value<=0x26)){
            const bool list_put=value<0x40;
            const auto high=std::uint64_t(t.dma[1])<<32;
            const auto list=t.dma[2],list_size=t.dma[3],tag=t.dma[4];
            auto local=t.dma[0];
            if(tag>31 || list%8 || list_size%8 || list_size>0x4000 || list>0x40000 || list_size>0x40000-list)
                throw std::runtime_error("invalid SPU DMA list range/tag");
            if(t.stalled_lists[tag].active)throw std::runtime_error("SPU DMA list queued behind a stalled list on the same tag is not modeled");
            spu_list_run(s,t,ls,tag,list_put,high,list,list+list_size,local);
            if(spu_dma_history_on)spu_trace_dma(t,list_put?"PUTL":"GETL",list,t.dma[0],list_size,tag,local);
            return;
        }
        const bool get=value>=0x40 && value<=0x42,put=value>=0x20 && value<=0x22;
        if(!get && !put)throw std::runtime_error("unimplemented SPU DMA command "+std::to_string(value));
        const auto address=(std::uint64_t(t.dma[1])<<32)|t.dma[2];
        const auto local=t.dma[0],size=t.dma[3],tag=t.dma[4];
        if(tag>31 || size>0x4000 || local>0x40000 || size>0x40000-local)
            throw std::runtime_error("invalid SPU DMA range/tag");
        if(size){
            const auto alignment=size<16?size:16;
            if((alignment&(alignment-1)) || size%alignment || (local|address)%alignment || (local&15)!=(address&15))
                throw std::runtime_error("misaligned SPU DMA transfer");
            spu_transfer(s,t,address,ls+local,size,put);if(get)spu_ls_write(t,local,size,address);
        }
        if(spu_dma_history_on)spu_trace_dma(t,get?"GET":"PUT",address,local,size,tag);
        return;
    }
    throw std::runtime_error("unimplemented SPU write channel "+std::to_string(channel)+" value="+std::to_string(value));
}
// frest/frsqest estimate tables: RPCS3 ffba2bf0 rpcs3/Emu/Cell/SPUThread.cpp:51-107 (commit 606a60e90
// "Accurate frest and frsqest"). Derived reference, not specified by SPU ISA v1.2 (pp.215-218).
static constexpr std::uint32_t spu_frest_fraction_lut[32]={
0x007ffbe0, 0x007f87a6, 0x0070ef72, 0x00708b40, 0x00638b12, 0x00633aea, 0x005792c4, 0x00574aa0,
0x004cca7e, 0x004c9262, 0x00430a44, 0x0042d62a, 0x003a2e12, 0x0039fdfa, 0x003215e4, 0x0031f1d2,
0x002aa9be, 0x002a85ac, 0x0023d59a, 0x0023bd8e, 0x001d8576, 0x001d8576, 0x0017ad5a, 0x0017ad5a,
0x00124543, 0x00124543, 0x000d392d, 0x000d392d, 0x0008851a, 0x0008851a, 0x00041d07, 0x00041d07};
static constexpr std::uint32_t spu_frest_exponent_lut[256]={
0x7f800000, 0x7e000000, 0x7d800000, 0x7d000000, 0x7c800000, 0x7c000000, 0x7b800000, 0x7b000000,
0x7a800000, 0x7a000000, 0x79800000, 0x79000000, 0x78800000, 0x78000000, 0x77800000, 0x77000000,
0x76800000, 0x76000000, 0x75800000, 0x75000000, 0x74800000, 0x74000000, 0x73800000, 0x73000000,
0x72800000, 0x72000000, 0x71800000, 0x71000000, 0x70800000, 0x70000000, 0x6f800000, 0x6f000000,
0x6e800000, 0x6e000000, 0x6d800000, 0x6d000000, 0x6c800000, 0x6c000000, 0x6b800000, 0x6b000000,
0x6a800000, 0x6a000000, 0x69800000, 0x69000000, 0x68800000, 0x68000000, 0x67800000, 0x67000000,
0x66800000, 0x66000000, 0x65800000, 0x65000000, 0x64800000, 0x64000000, 0x63800000, 0x63000000,
0x62800000, 0x62000000, 0x61800000, 0x61000000, 0x60800000, 0x60000000, 0x5f800000, 0x5f000000,
0x5e800000, 0x5e000000, 0x5d800000, 0x5d000000, 0x5c800000, 0x5c000000, 0x5b800000, 0x5b000000,
0x5a800000, 0x5a000000, 0x59800000, 0x59000000, 0x58800000, 0x58000000, 0x57800000, 0x57000000,
0x56800000, 0x56000000, 0x55800000, 0x55000000, 0x54800000, 0x54000000, 0x53800000, 0x53000000,
0x52800000, 0x52000000, 0x51800000, 0x51000000, 0x50800000, 0x50000000, 0x4f800000, 0x4f000000,
0x4e800000, 0x4e000000, 0x4d800000, 0x4d000000, 0x4c800000, 0x4c000000, 0x4b800000, 0x4b000000,
0x4a800000, 0x4a000000, 0x49800000, 0x49000000, 0x48800000, 0x48000000, 0x47800000, 0x47000000,
0x46800000, 0x46000000, 0x45800000, 0x45000000, 0x44800000, 0x44000000, 0x43800000, 0x43000000,
0x42800000, 0x42000000, 0x41800000, 0x41000000, 0x40800000, 0x40000000, 0x3f800000, 0x3f000000,
0x3e800000, 0x3e000000, 0x3d800000, 0x3d000000, 0x3c800000, 0x3c000000, 0x3b800000, 0x3b000000,
0x3a800000, 0x3a000000, 0x39800000, 0x39000000, 0x38800000, 0x38000000, 0x37800000, 0x37000000,
0x36800000, 0x36000000, 0x35800000, 0x35000000, 0x34800000, 0x34000000, 0x33800000, 0x33000000,
0x32800000, 0x32000000, 0x31800000, 0x31000000, 0x30800000, 0x30000000, 0x2f800000, 0x2f000000,
0x2e800000, 0x2e000000, 0x2d800000, 0x2d000000, 0x2c800000, 0x2c000000, 0x2b800000, 0x2b000000,
0x2a800000, 0x2a000000, 0x29800000, 0x29000000, 0x28800000, 0x28000000, 0x27800000, 0x27000000,
0x26800000, 0x26000000, 0x25800000, 0x25000000, 0x24800000, 0x24000000, 0x23800000, 0x23000000,
0x22800000, 0x22000000, 0x21800000, 0x21000000, 0x20800000, 0x20000000, 0x1f800000, 0x1f000000,
0x1e800000, 0x1e000000, 0x1d800000, 0x1d000000, 0x1c800000, 0x1c000000, 0x1b800000, 0x1b000000,
0x1a800000, 0x1a000000, 0x19800000, 0x19000000, 0x18800000, 0x18000000, 0x17800000, 0x17000000,
0x16800000, 0x16000000, 0x15800000, 0x15000000, 0x14800000, 0x14000000, 0x13800000, 0x13000000,
0x12800000, 0x12000000, 0x11800000, 0x11000000, 0x10800000, 0x10000000, 0x0f800000, 0x0f000000,
0x0e800000, 0x0e000000, 0x0d800000, 0x0d000000, 0x0c800000, 0x0c000000, 0x0b800000, 0x0b000000,
0x0a800000, 0x0a000000, 0x09800000, 0x09000000, 0x08800000, 0x08000000, 0x07800000, 0x07000000,
0x06800000, 0x06000000, 0x05800000, 0x05000000, 0x04800000, 0x04000000, 0x03800000, 0x03000000,
0x02800000, 0x02000000, 0x01800000, 0x01000000, 0x00800000, 0x00000000, 0x00000000, 0x00000000};
static constexpr std::uint32_t spu_frsqest_fraction_lut[64]={
0x00350160, 0x0034e954, 0x002f993d, 0x002f993d, 0x002aa523, 0x002aa523, 0x0026190d, 0x0026190d,
0x0021e4f9, 0x0021e4f9, 0x001e00e9, 0x001e00e9, 0x001a5cd9, 0x001a5cd9, 0x0016f8cb, 0x0016f8cb,
0x0013ccc0, 0x0013ccc0, 0x0010ccb3, 0x0010ccb3, 0x000e00aa, 0x000e00aa, 0x000b58a1, 0x000b58a1,
0x0008d498, 0x0008d498, 0x00067491, 0x00067491, 0x00043089, 0x00043089, 0x00020c83, 0x00020c83,
0x007ffdf4, 0x007fd1de, 0x007859c8, 0x00783dba, 0x0071559c, 0x0071559c, 0x006ae57c, 0x006ae57c,
0x0064f561, 0x0064f561, 0x005f7149, 0x005f7149, 0x005a4d33, 0x005a4d33, 0x0055811f, 0x0055811f,
0x0051050f, 0x0051050f, 0x004cc8fe, 0x004cc8fe, 0x0048d0f0, 0x0048d0f0, 0x004510e4, 0x004510e4,
0x004180d7, 0x004180d7, 0x003e24cc, 0x003e24cc, 0x003af4c3, 0x003af4c3, 0x0037e8ba, 0x0037e8ba};
static constexpr std::uint32_t spu_frsqest_exponent_lut[256]={
0x7f800000, 0x5e800000, 0x5e800000, 0x5e000000, 0x5e000000, 0x5d800000, 0x5d800000, 0x5d000000,
0x5d000000, 0x5c800000, 0x5c800000, 0x5c000000, 0x5c000000, 0x5b800000, 0x5b800000, 0x5b000000,
0x5b000000, 0x5a800000, 0x5a800000, 0x5a000000, 0x5a000000, 0x59800000, 0x59800000, 0x59000000,
0x59000000, 0x58800000, 0x58800000, 0x58000000, 0x58000000, 0x57800000, 0x57800000, 0x57000000,
0x57000000, 0x56800000, 0x56800000, 0x56000000, 0x56000000, 0x55800000, 0x55800000, 0x55000000,
0x55000000, 0x54800000, 0x54800000, 0x54000000, 0x54000000, 0x53800000, 0x53800000, 0x53000000,
0x53000000, 0x52800000, 0x52800000, 0x52000000, 0x52000000, 0x51800000, 0x51800000, 0x51000000,
0x51000000, 0x50800000, 0x50800000, 0x50000000, 0x50000000, 0x4f800000, 0x4f800000, 0x4f000000,
0x4f000000, 0x4e800000, 0x4e800000, 0x4e000000, 0x4e000000, 0x4d800000, 0x4d800000, 0x4d000000,
0x4d000000, 0x4c800000, 0x4c800000, 0x4c000000, 0x4c000000, 0x4b800000, 0x4b800000, 0x4b000000,
0x4b000000, 0x4a800000, 0x4a800000, 0x4a000000, 0x4a000000, 0x49800000, 0x49800000, 0x49000000,
0x49000000, 0x48800000, 0x48800000, 0x48000000, 0x48000000, 0x47800000, 0x47800000, 0x47000000,
0x47000000, 0x46800000, 0x46800000, 0x46000000, 0x46000000, 0x45800000, 0x45800000, 0x45000000,
0x45000000, 0x44800000, 0x44800000, 0x44000000, 0x44000000, 0x43800000, 0x43800000, 0x43000000,
0x43000000, 0x42800000, 0x42800000, 0x42000000, 0x42000000, 0x41800000, 0x41800000, 0x41000000,
0x41000000, 0x40800000, 0x40800000, 0x40000000, 0x40000000, 0x3f800000, 0x3f800000, 0x3f000000,
0x3f000000, 0x3e800000, 0x3e800000, 0x3e000000, 0x3e000000, 0x3d800000, 0x3d800000, 0x3d000000,
0x3d000000, 0x3c800000, 0x3c800000, 0x3c000000, 0x3c000000, 0x3b800000, 0x3b800000, 0x3b000000,
0x3b000000, 0x3a800000, 0x3a800000, 0x3a000000, 0x3a000000, 0x39800000, 0x39800000, 0x39000000,
0x39000000, 0x38800000, 0x38800000, 0x38000000, 0x38000000, 0x37800000, 0x37800000, 0x37000000,
0x37000000, 0x36800000, 0x36800000, 0x36000000, 0x36000000, 0x35800000, 0x35800000, 0x35000000,
0x35000000, 0x34800000, 0x34800000, 0x34000000, 0x34000000, 0x33800000, 0x33800000, 0x33000000,
0x33000000, 0x32800000, 0x32800000, 0x32000000, 0x32000000, 0x31800000, 0x31800000, 0x31000000,
0x31000000, 0x30800000, 0x30800000, 0x30000000, 0x30000000, 0x2f800000, 0x2f800000, 0x2f000000,
0x2f000000, 0x2e800000, 0x2e800000, 0x2e000000, 0x2e000000, 0x2d800000, 0x2d800000, 0x2d000000,
0x2d000000, 0x2c800000, 0x2c800000, 0x2c000000, 0x2c000000, 0x2b800000, 0x2b800000, 0x2b000000,
0x2b000000, 0x2a800000, 0x2a800000, 0x2a000000, 0x2a000000, 0x29800000, 0x29800000, 0x29000000,
0x29000000, 0x28800000, 0x28800000, 0x28000000, 0x28000000, 0x27800000, 0x27800000, 0x27000000,
0x27000000, 0x26800000, 0x26800000, 0x26000000, 0x26000000, 0x25800000, 0x25800000, 0x25000000,
0x25000000, 0x24800000, 0x24800000, 0x24000000, 0x24000000, 0x23800000, 0x23800000, 0x23000000,
0x23000000, 0x22800000, 0x22800000, 0x22000000, 0x22000000, 0x21800000, 0x21800000, 0x21000000,
0x21000000, 0x20800000, 0x20800000, 0x20000000, 0x20000000, 0x1f800000, 0x1f800000, 0x1f000000};
[[maybe_unused]] static std::uint32_t spu_frest(std::uint32_t a){
    return spu_frest_fraction_lut[(a>>18)&0x1f]|spu_frest_exponent_lut[(a>>23)&0xff]|(a&0x80000000u);
}
[[maybe_unused]] static std::uint32_t spu_frsqest(std::uint32_t a){
    return spu_frsqest_fraction_lut[(a>>18)&0x3f]|spu_frsqest_exponent_lut[(a>>23)&0xff];
}
// rchcnt: capacities per Cell BE Handbook v1.1 SPE channel table (md lines 28120-28170).
// DMA and tag updates complete synchronously in this runtime; unmodeled channels stop.
[[maybe_unused]] static std::uint32_t spu_channel_count(PpuState& s,NativeSpu::Thread& t,unsigned channel){
    if(channel==0 || channel==3 || channel==4)spu_capture_unsupported("external channel count");
    SpuProfileScope profile(SpuProfile::CHANNEL);spu_stat(t,15);
    // Only the polled counts (channel 0 here, the atomic status) keep the busy-poll detector alive.
    if(channel!=0 && channel!=27)spu_poll_reset(t);
    switch(channel){
    case 0:
        spu_stat(t,0);spu_collect_events(s,t);
        // No event: a repeat of this poll at an unchanged SPU state may park (spu_poll_note) before returning.
        if(t.event_ready)spu_poll_reset(t);else{spu_stat(t,1);spu_poll_note(t,NativeSpu::Thread::PollState::EventCount,0);}
        return t.event_ready?1:0;
    case 3:case 4:return s.process.spu_worker.pool_mode?(t.snr[channel-3].ready()?1:0):(t.signal_ready[channel-3]?1:0);
    case 1:case 2:case 7:case 8:case 9:case 11:case 12:case 13:case 14:case 15:case 16:case 17:case 18:case 19:case 20:case 22:case 23:case 26:return 1;
    case 21:return 16;
    case 24:return t.tag_status_ready?1:0;
    case 25:return t.stall_stat_ready?1:0;
    case 27:return t.atomic_status_ready?1:0;
    case 28:return t.out_mailbox_full?0:1;
    case 29:return static_cast<std::uint32_t>(t.in_mailbox.size());
    case 30:return 1; // interrupt mailbox writes are delivered (or block) before the write completes
    }
    // Cell BE Handbook v1.1 PDF pp.444-446; ISA v1.2 p.249:
    // reserved/unimplemented channels have zero capacity. This does not
    // authorize reads or writes to them (those still stop explicitly).
    if(channel<128 && (channel==5 || channel==6 || channel==10 || channel>=31))return 0;
    throw std::runtime_error("unimplemented SPU channel count "+std::to_string(channel));
}
[[maybe_unused]] static std::uint32_t spu_read_channel(PpuState& s,NativeSpu::Thread& t,unsigned channel){
    if(channel==0 || channel==3 || channel==4 || channel==8)spu_capture_unsupported("external channel read");
    SpuProfileScope profile(SpuProfile::CHANNEL);spu_stat(t,14);if(!channel)spu_stat(t,2);t.irq_next=0;
    // Reads of private state (decrementer, event mask, tag mask, interrupt enable, SRR0, atomic status) are
    // neutral for the busy-poll detector; every consuming or external read ends the candidate poll.
    if(channel!=8 && channel!=11 && channel!=12 && channel!=13 && channel!=15 && channel!=27)spu_poll_reset(t);
    if(channel==29){
        std::uint32_t value=0;if(!t.in_mailbox.pop(value))throw SpuBlocked{};return value;
    }
    if(channel==3 || channel==4){
        const auto number=channel-3;
        if(s.process.spu_worker.pool_mode){std::uint32_t value=0;
            if(!t.snr[number].pop(value)){t.waiting_signal=number+1;throw SpuBlocked{};}
            t.waiting_signal=0;return value;
        }
        if(!t.signal_ready[number]){t.waiting_signal=number+1;throw SpuBlocked{};}
        const auto value=t.signals[number];t.signals[number]=0;t.signal_ready[number]=false;t.waiting_signal=0;return value;
    }
    if(channel==11)return t.event_mask;
    if(channel==8){spu_collect_events(s,t,true);return t.decrementer;}
    if(channel==12)return t.tag_mask;
    // Ordinary groups only; isolated execution is not supported.
    if(channel==13)return t.interrupts_enabled?1:0;
    if(channel==15){
        if(t.srr0_pending)throw std::runtime_error("SPU rdch SRR0 requires syncc after wrch SRR0");
        if(!t.srr0_valid)throw std::runtime_error("SPU rdch requires initialized SRR0");
        return t.srr0;
    }
    if(channel==0){
        spu_collect_events(s,t);
        if(!t.event_ready){t.waiting_event=true;throw SpuBlocked{};}
        t.event_ready=false;t.waiting_event=false;return t.event_status&t.event_mask;
    }
    if(channel==27){
        if(!t.atomic_status_ready)throw std::runtime_error("SPU atomic status read has no completed command");
        t.atomic_status_ready=false;return t.atomic_status;
    }
    if(channel==24){
        if(!t.tag_status_ready){if(t.tag_update_pending)throw SpuBlocked{};throw std::runtime_error("SPU tag status read has no pending update");}
        t.tag_status_ready=false;return t.tag_status;
    }
    if(channel==25){ // MFC_RdListStallStat: returns and clears the stalled tags; blocks while none
        if(!t.stall_stat_ready)throw SpuBlocked{};
        const auto value=t.stall_stat;t.stall_stat=0;t.stall_stat_ready=false;return value;
    }
    throw std::runtime_error("unimplemented SPU read channel "+std::to_string(channel));
}
'''+VEC_CPP+BLOCK_CORE_CPP

SCHEDULER_CPP = r'''
[[maybe_unused]] static std::uint64_t spu_step_limit(const char* value){
    if(!value)return 10000000;
    std::uint64_t limit=0;
    for(const char* p=value;*p;++p){
        if(*p<'0' || *p>'9' || limit>(UINT64_MAX-unsigned(*p-'0'))/10)
            throw std::runtime_error("invalid ANYPS3_SPU_MAX_STEPS: expected positive decimal uint64");
        limit=limit*10+unsigned(*p-'0');
    }
    if(!limit)throw std::runtime_error("invalid ANYPS3_SPU_MAX_STEPS: expected positive decimal uint64");
    return limit;
}
[[maybe_unused]] static bool spu_run(PpuState& s,unsigned budget){
    spu_profile_report();
    static const bool trace=[](){const char* value=std::getenv("ANYPS3_SPU_TRACE");return value && std::string(value)=="1";}();
    static const auto max_steps=spu_step_limit(std::getenv("ANYPS3_SPU_MAX_STEPS"));
    bool progress=false;
    for(auto& group:s.spu.groups){
        if(group.state==3){
            auto receiver=std::find_if(group.threads.begin(),group.threads.end(),[&](const auto& t){return t.waiting_receive && t.id==group.receive_thread;});
            if(receiver==group.threads.end())throw std::runtime_error("SPU group waiting without a receiving thread");
            std::uint32_t qid=0;
            for(const auto& b:receiver->queue_bindings)if(b.second && b.first==receiver->receive_key)qid=b.second;
            const auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& x){return qid && x.id==qid;});
            if(q!=s.events.queues.end() && q->data.empty()){SpuProfileScope profile(SpuProfile::WAIT);continue;}
            group.state=6;
            // Complete receive before running peer threads after wakeup.
            if(receiver->steps>=max_steps)throw std::runtime_error("SPU execution step limit reached");
            spu_stop(s,*receiver,0x110);receiver->pc=(receiver->pc+4)&0x3fffc;
            ++receiver->steps;receiver->published_steps.value.store(receiver->steps,std::memory_order_relaxed);progress=true;
        }
        if(group.state!=6)continue;
        for(unsigned i=0;i<group.threads.size() && group.state==6;++i){
            auto& t=group.threads[i];auto* ls=group.context.data()+i*0x40000;
            if(t.stopped)continue;
            spu_collect_events(s,t);
            spu_take_interrupt(s,t);
            if(t.waiting_event && !t.event_ready){SpuProfileScope profile(SpuProfile::WAIT);continue;}
            if(t.waiting_signal && !t.signal_ready[t.waiting_signal-1]){SpuProfileScope profile(SpuProfile::WAIT);continue;}
            SpuCaptureScope capture(t,ls,budget);
            const auto slice_start=t.steps;
            try {
                while(t.steps-slice_start<budget){
                    if(t.steps>=max_steps)throw std::runtime_error("SPU execution step limit reached");
                    spu_step(s,t,ls,budget-static_cast<unsigned>(t.steps-slice_start),max_steps,trace);progress=true;
                }
            }catch(const SpuSliceEnd&){
                ++t.steps;progress=true;
            }catch(const SpuBlocked&){
                SpuProfileScope profile(SpuProfile::WAIT);
                progress=progress || t.steps!=slice_start;
                std::fprintf(stderr,"SPU wait id=%x pc=%05x steps=%" PRIu64 "\n",t.id,t.pc,t.steps);
            }catch(...){
                if(s.process.spu_worker.enabled)s.process.spu_worker.failed_context(t.pc,t.id);
                std::fprintf(stderr,"SPU STOP id=%x pc=%05x steps=%" PRIu64 "\n",t.id,t.pc,t.steps);
                std::fprintf(stderr,"SPU DMA state ls=%08x ea=%08x%08x size=%x tag=%u\n",t.dma[0],t.dma[1],t.dma[2],t.dma[3],t.dma[4]);
                for(unsigned reg=0;reg<16;++reg)std::fprintf(stderr,"spu_r%u=%08x %08x %08x %08x\n",reg,spu_word(t.gpr[reg],0),spu_word(t.gpr[reg],1),spu_word(t.gpr[reg],2),spu_word(t.gpr[reg],3));
                throw;
            }
        }
    }
    return progress;
}
'''


POOL_CPP = r'''
static void spu_lane_prepare_wait(PpuState& s,NativeSpu::Thread& t){
    spu_stat(t,7);
    const auto blocked_pc=t.pc;
    auto& lane=*NativeSpuWorker::current;
    // A poll park (spu_poll_note) waits for: a write to the polled reservation line, a mailbox/signal push
    // (lane wake hooks), the decrementer deadline when TM is enabled, or the park timeout - whichever is first.
    const bool poll=t.idle_poll.parked;
    const bool poll_line=poll && t.idle_poll.parked_kind==NativeSpu::Thread::PollState::Getllar;
    lane.event_driven=true;lane.wait_deadline=std::chrono::steady_clock::time_point::max();
    // Arm first, then recheck the reservation. A write before arm is seen by
    // collect_events; a write after arm changes wake_generation before sleep.
    const bool watch_lost_event=(t.waiting_event || t.interrupts_enabled || poll) && (t.event_mask&0x400);
    if((watch_lost_event || poll_line) && t.reservation_address!=UINT64_MAX)
        lane.watch_reservation(s.memory,t.reservation.first);
    else lane.reservation_watch.reset();
    spu_collect_events(s,t);spu_take_interrupt(s,t);
    if((t.waiting_event || t.interrupts_enabled || poll) && !t.event_ready && (t.event_mask&0x20) && t.decrementer_running){
        const auto ticks=std::uint64_t(t.decrementer)+1;
        const auto ns=(ticks*UINT64_C(1000000000)+TIMEBASE_HZ-1)/TIMEBASE_HZ;
        lane.wait_deadline=std::chrono::steady_clock::now()+std::chrono::nanoseconds(ns);
    }
    if(poll){
        // Consecutive parks without a relevant change back off: base, 2x, 4x ... up to the cap.
        const auto base=spu_poll_park_ns.load(std::memory_order_relaxed),cap=spu_poll_park_max_ns.load(std::memory_order_relaxed);
        const unsigned shift=std::min(t.idle_poll.parks?t.idle_poll.parks-1:0u,20u);
        const auto timeout=std::chrono::steady_clock::now()+std::chrono::nanoseconds(std::min(base<<shift,std::max(base,cap)));
        lane.wait_deadline=std::min(lane.wait_deadline,timeout);
    }
    // After arming: a changed polled line (lost reservation) or a ready event means the retry can progress.
    const bool poll_ready=poll && (poll_line?t.reservation_address==UINT64_MAX:t.event_ready);
    if((t.waiting_event && t.event_ready) || (t.waiting_signal && t.snr[t.waiting_signal-1].ready()) ||
       (t.pc!=blocked_pc) || poll_ready || (!poll && !t.waiting_event && !t.waiting_signal && !t.in_mailbox.empty()))lane.wake();
}
[[maybe_unused]] static bool spu_run_thread(PpuState& s,NativeSpu::Group& group,unsigned index,unsigned budget){
    spu_profile_report();
    static const bool trace=anyps3_trace_enabled("ANYPS3_SPU_TRACE");
    static const auto max_steps=spu_step_limit(std::getenv("ANYPS3_SPU_MAX_STEPS"));
    auto& t=group.threads[index];auto* ls=group.context.data()+index*0x40000;
    if(NativeSpuWorker::current->stop.load())return false;
    // Busy-poll parking exists only for pool lanes: this function is their sole entry (serial spu_run and the
    // legacy single worker never set it, so "SPU wait" reports and their timing stay as before).
    const bool resumed_poll=t.idle_poll.parked;t.idle_poll.parked=false;
    struct PollScope {
        NativeSpu::Thread& thread;
        explicit PollScope(NativeSpu::Thread& x):thread(x){thread.idle_poll.enabled=spu_poll_park_on.load(std::memory_order_relaxed);}
        ~PollScope(){thread.idle_poll.enabled=false;}
    } poll_scope(t);
    NativeSpuWorker::current->event_driven=group.state!=3;
    NativeSpuWorker::current->wait_deadline=std::chrono::steady_clock::time_point::max();
    if(group.state==3){
        NativeSpuWorker::current->owner_call([&]{
            SpuOwnerScope control(s,group.id,false,false);
            if(group.state!=3)return;
            auto receiver=std::find_if(group.threads.begin(),group.threads.end(),[&](const auto& x){return x.waiting_receive && x.id==group.receive_thread;});
            if(receiver==group.threads.end())throw std::runtime_error("SPU group waiting without a receiving thread");
            std::uint32_t qid=0;
            for(const auto& b:receiver->queue_bindings)if(b.second && b.first==receiver->receive_key)qid=b.second;
            const auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& x){return qid && x.id==qid;});
            if(q!=s.events.queues.end() && q->data.empty())return;
            if(receiver->steps>=max_steps)throw std::runtime_error("SPU execution step limit reached");
            group.state=6;spu_stop(s,*receiver,0x110);receiver->pc=(receiver->pc+4)&0x3fffc;++receiver->steps;receiver->published_steps.value.store(receiver->steps,std::memory_order_relaxed);
        });
    }
    if(NativeSpuWorker::current->stop.load() || group.state!=6 || !NativeSpuWorker::current->scheduled.load() || t.stopped)return false;
    t.block_control_exit=false;
    spu_collect_events(s,t);spu_take_interrupt(s,t);
    if(t.waiting_event && !t.event_ready){spu_lane_prepare_wait(s,t);return false;}
    if(t.waiting_signal && !t.snr[t.waiting_signal-1].ready()){spu_lane_prepare_wait(s,t);return false;}
    // A poll park keeps its line watch armed for the retry; any other run drops it as before.
    if(!resumed_poll)NativeSpuWorker::current->reservation_watch.reset();
    spu_stat(t,6);
    SpuCaptureScope capture(t,ls,budget);
    const auto start=t.steps;bool progress=false;
    try{
        while(t.steps-start<budget && group.state==6 && NativeSpuWorker::current->scheduled.load() && !NativeSpuWorker::current->stop.load() && !t.block_control_exit){
            if(t.steps>=max_steps)throw std::runtime_error("SPU execution step limit reached");
            spu_step(s,t,ls,budget-static_cast<unsigned>(t.steps-start),max_steps,trace);progress=true;
        }
    }catch(const SpuSliceEnd&){++t.steps;t.published_steps.value.store(t.steps,std::memory_order_relaxed);progress=true;}
    catch(const SpuBlocked&){
        spu_stat(t,11);spu_lane_prepare_wait(s,t);
        // Spin iterations that preceded a poll park are no progress: the lane must sleep, not re-enter.
        return !t.idle_poll.parked && (progress || t.steps!=start);
    }
    catch(...){NativeSpuWorker::current->failed_context(t.pc,t.id);throw;}
    return progress;
}
'''
