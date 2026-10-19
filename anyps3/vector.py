"""Static VMX lifting, with vector bytes stored in architectural big-endian order."""

def lift_vector(word):
    op=word>>26;d=(word>>21)&31;a=(word>>16)&31;b=(word>>11)&31;c=(word>>6)&31
    xo=(word>>1)&1023;vx=word&2047
    reg=lambda n:f'state.vr[{n}]'
    if op==31 and not word&1 and xo in (6,38):
        base=f'r[{a}]' if a else 'UINT64_C(0)'
        start='(ea&15)' if xo==6 else '(16-(ea&15))'
        return ('lvsl' if xo==6 else 'lvsr'),f'const auto ea={base}+r[{b}];for(unsigned i=0;i<16;++i){reg(d)}[i]=static_cast<std::uint8_t>({start}+i);'
    if op==31 and not word&1 and xo in (7,39,71,519,551,775,807):
        base=f'r[{a}]' if a else 'UINT64_C(0)'
        body=f'const auto ea={base}+r[{b}];std::array<std::uint8_t,16> out{{}};'
        if xo in (7,39,71):
            # Cell loads the aligned quadword for element loads; only the
            # addressed element is architecturally defined for generic VMX.
            body+='ppu_read_bytes(state,ea&~UINT64_C(15),out.data(),16);'
        elif xo in (519,775):
            body+='const unsigned n=ea&15;ppu_read_bytes(state,ea,out.data(),16-n);'
        else:
            body+='const unsigned n=ea&15;if(n){ppu_read_bytes(state,ea&~UINT64_C(15),out.data()+16-n,n);}'
        return 'vector_load',body+f'{reg(d)}=out;'
    if op!=4:return None
    out='std::array<std::uint8_t,16> out{};'
    finish=f'{reg(d)}=out;'
    if vx==0x604 and not a and not b:
        return 'mfvscr',out+'vmx_word(out,3,state.vscr);'+finish
    if vx==0x644 and not d and not a:
        return 'mtvscr',f'state.vscr=vmx_word({reg(b)},3)&0x10001;'
    if vx==0x10a and not a:
        return 'vrefp',out+f'VmxFloatScope scope(state);for(unsigned i=0;i<4;++i)vmx_word(out,i,vmx_reciprocal(state,vmx_word({reg(b)},i)));'+finish
    # vmule*/vmulo* (AltiVec PEM): products of the even (0,2,..) or odd (1,3,..) n-byte elements
    # in big-endian element order, each stored as a 2n-byte element of the result.
    multiply={0x008:(1,1,0),0x048:(2,1,0),0x108:(1,1,1),0x148:(2,1,1),
              0x208:(1,0,0),0x248:(2,0,0),0x308:(1,0,1),0x348:(2,0,1)}
    if vx in multiply:
        n,odd,signed=multiply[vx];kind=f'std::int{8*n}_t' if signed else f'std::uint{8*n}_t'
        load=lambda r:f'static_cast<{kind}>(' + '|'.join(f'(std::uint{8*n}_t({r}[{n}*(2*e+{odd})+{j}])<<{8*(n-1-j)})' for j in range(n)) + ')'
        wide=f'std::int{16*n}_t' if signed else f'std::uint{16*n}_t'   # unsigned: no signed overflow
        body=(f'for(unsigned e=0;e<{8//n};++e){{const auto product=static_cast<std::uint{16*n}_t>('
              f'static_cast<{wide}>({load(reg(a))})*static_cast<{wide}>({load(reg(b))}));'
              + ''.join(f'out[{2*n}*e+{j}]=static_cast<std::uint8_t>(product>>{8*(2*n-1-j)});' for j in range(2*n)) + '}')
        return ('vmulo' if odd else 'vmule')+('s' if signed else 'u')+('b' if n==1 else 'h'),out+body+finish
    # Integer element families; element e of size n occupies bytes n*e..n*e+n-1.
    merge={0xc:(1,0),0x4c:(2,0),0x8c:(4,0),0x10c:(1,8),0x14c:(2,8),0x18c:(4,8)}
    if vx in merge:
        n,half=merge[vx]
        body=f'for(unsigned e=0;e<{8//n};++e)for(unsigned j=0;j<{n};++j){{out[2*{n}*e+j]={reg(a)}[{half}+{n}*e+j];out[2*{n}*e+{n}+j]={reg(b)}[{half}+{n}*e+j];}}'
        return 'vector_merge',out+body+finish
    unpack={0x20e:(1,0),0x28e:(1,8),0x24e:(2,0),0x2ce:(2,8)}
    if vx in unpack and not a:
        n,half=unpack[vx]
        body=f'for(unsigned e=0;e<{8//n};++e){{const bool negative={reg(b)}[{half}+{n}*e]>>7;'
        body+=f'for(unsigned j=0;j<{n};++j){{out[2*{n}*e+j]=negative?0xff:0;out[2*{n}*e+{n}+j]={reg(b)}[{half}+{n}*e+j];}}}}'
        return 'vector_unpack',out+body+finish
    def element(v,n,signed):
        value='|'.join(f'(std::uint32_t({v}[{n}*e+{j}])<<{8*(n-1-j)})' for j in range(n))
        return f'static_cast<std::int{8*n}_t>({value})' if signed else f'static_cast<std::uint{8*n}_t>({value})'
    def store(n,value):
        return ''.join(f'out[{n}*e+{j}]=static_cast<std::uint8_t>(std::uint32_t({value})>>{8*(n-1-j)});' for j in range(n))
    extreme={0x2:(1,0,1),0x42:(2,0,1),0x82:(4,0,1),0x102:(1,1,1),0x142:(2,1,1),0x182:(4,1,1),
             0x202:(1,0,0),0x242:(2,0,0),0x282:(4,0,0),0x302:(1,1,0),0x342:(2,1,0),0x382:(4,1,0)}
    if vx in extreme:
        n,signed,maximum=extreme[vx]
        x,y=element(reg(a),n,signed),element(reg(b),n,signed)
        body=f'for(unsigned e=0;e<{16//n};++e){{const auto x={x};const auto y={y};{store(n,f"(x{">" if maximum else "<"}y?x:y)")}}}'
        return 'vector_integer_extreme',out+body+finish
    if vx in (0x4,0x44,0x84):
        n=1<<(vx>>6);bits=8*n
        x,y=element(reg(a),n,0),element(reg(b),n,0)
        body=f'for(unsigned e=0;e<{16//n};++e){{const std::uint{bits}_t x={x};const unsigned k={y}&{bits-1};'
        body+=f'const std::uint{bits}_t v=k?static_cast<std::uint{bits}_t>((x<<k)|(x>>({bits}-k))):x;{store(n,"v")}}}'
        return 'vector_rotate',out+body+finish
    if vx&0x3ff in (0x006,0x046):
        n=1 if vx&0x3ff==0x006 else 2
        body=out+'unsigned yes=0;'+f'for(unsigned e=0;e<{16//n};++e){{const bool match={element(reg(a),n,0)}=={element(reg(b),n,0)};yes+=match;for(unsigned j=0;j<{n};++j)out[{n}*e+j]=match?0xff:0;}}'
        body+=(f'state.cr=(state.cr&~UINT32_C(0xf0))|((yes=={16//n}?8u:yes==0?2u:0u)<<4);' if vx&0x400 else '(void)yes;')
        return 'vector_compare_equal',body+finish
    pack={0x8e:(2,0),0xce:(4,0),0x18e:(2,1),0x1ce:(4,1)}
    if vx in pack:
        n,signed=pack[vx];m=n//2;bits=8*m
        limit=(1<<(bits-1))-1 if signed else (1<<bits)-1
        low=-(1<<(bits-1)) if signed else 0
        body='bool saturated=false;'
        for src,offset in ((reg(a),0),(reg(b),16//n)):
            body+=f'for(unsigned e=0;e<{16//n};++e){{const std::int64_t x={element(src,n,signed)};'
            body+=f'const std::int64_t v=x>{limit}?{limit}:x<{low}?{low}:x;saturated|=v!=x;'
            body+=''.join(f'out[{m}*(e+{offset})+{j}]=static_cast<std::uint8_t>(std::uint64_t(v)>>{8*(m-1-j)});' for j in range(m))+'}'
        body+='if(saturated)state.vscr|=1;'
        return 'vector_pack_saturate',out+body+finish
    if vx==0x14a and not a:
        return 'vrsqrtefp',out+f'VmxFloatScope scope(state);for(unsigned i=0;i<4;++i)vmx_word(out,i,vmx_reciprocal_root(state,vmx_word({reg(b)},i)));'+finish
    if vx in (0x40a,0x44a):
        expr=f'vmx_extreme(vmx_word({reg(a)},i),vmx_word({reg(b)},i),{str(vx==0x40a).lower()})'
        return ('vmaxfp' if vx==0x40a else 'vminfp'),out+f'VmxFloatScope scope(state);for(unsigned i=0;i<4;++i)vmx_word(out,i,{expr});'+finish
    if vx in (0x30a,0x34a,0x38a,0x3ca):
        if vx in (0x38a,0x3ca):
            body=f'auto status=state.vscr;for(unsigned i=0;i<4;++i)vmx_word(out,i,vmx_to_integer(vmx_word({reg(b)},i),{a},{str(vx==0x3ca).lower()},status));state.vscr=status;'
        else:
            body=f'VmxFloatScope scope(state);for(unsigned i=0;i<4;++i){{const auto raw=vmx_word({reg(b)},i);'
            body+=('const bool negative=raw>>31;const std::uint32_t magnitude=negative?0u-raw:raw;' if vx==0x34a else 'const bool negative=false;const auto magnitude=raw;')
            body+=f'vmx_word(out,i,fp_narrow_exact(fp_pack(state,negative,magnitude,-{a},24)));}}'
        return 'vector_convert',out+body+finish
    if vx in (0xa,0x4a) or word&63 in (46,47):
        if vx in (0xa,0x4a):
            expr=f'vmx_add(state,vmx_word({reg(a)},i),vmx_word({reg(b)},i),{str(vx==0x4a).lower()})'
        else:
            negative=(word&63)==47
            expr=f'vmx_fused(state,vmx_word({reg(a)},i),vmx_word({reg(b)},i),vmx_word({reg(c)},i),{str(negative).lower()})'
        return 'vector_float',out+f'VmxFloatScope scope(state);for(unsigned i=0;i<4;++i)vmx_word(out,i,{expr});'+finish
    if vx in (0x20c,0x24c,0x28c,0x30c,0x34c,0x38c):
        size=1<<((vx>>6)&3)
        if vx<0x300:
            offset=(a&(16//size-1))*size
            body=f'for(unsigned i=0;i<16;++i)out[i]={reg(b)}[{offset}+i%{size}];'
        else:
            value=(a-32 if a&16 else a)&0xffffffff
            body=f'for(unsigned i=0;i<16;++i)out[i]=UINT32_C({value})>>(({size}-1-i%{size})*8);'
        return 'vector_splat',out+body+finish
    if word&63 in (42,43,44):
        kind=word&63
        if kind==42:expr=f'({reg(a)}[i]&~{reg(c)}[i])|({reg(b)}[i]&{reg(c)}[i])'
        elif kind==43:expr=f'(({reg(c)}[i]&31)<16?{reg(a)}[{reg(c)}[i]&15]:{reg(b)}[{reg(c)}[i]&15])'
        else:
            if c&16:return None
            expr=f'(i+{c}<16?{reg(a)}[i+{c}]:{reg(b)}[i+{c}-16])'
        return 'vector_permute_select',out+f'for(unsigned i=0;i<16;++i)out[i]={expr};'+finish
    saturating={0x200:(1,0,'+'),0x240:(2,0,'+'),0x280:(4,0,'+'),0x300:(1,1,'+'),0x340:(2,1,'+'),0x380:(4,1,'+'),
                0x600:(1,0,'-'),0x640:(2,0,'-'),0x700:(1,1,'-'),0x740:(2,1,'-'),0x780:(4,1,'-')}
    if vx in saturating:
        # vadd{u,s}{b,h,w}s / vsub{u,s}{b,h}s, vsubsws: element-wise saturating
        # arithmetic, VSCR[SAT] set when any element clamps (AltiVec PEM).
        n,signed,operator=saturating[vx]
        bits=8*n;low=-(1<<(bits-1)) if signed else 0;high=(1<<(bits-1))-1 if signed else (1<<bits)-1
        x,y=element(reg(a),n,signed),element(reg(b),n,signed)
        body=out+'bool saturated=false;'+f'for(unsigned e=0;e<{16//n};++e){{const std::int64_t x={x},y={y},r=x{operator}y;'
        body+=f'const std::int64_t c=r<INT64_C({low})?INT64_C({low}):(r>INT64_C({high})?INT64_C({high}):r);saturated|=c!=r;{store(n,"c")}}}'
        return 'vector_saturating',body+'if(saturated)state.vscr|=1;'+finish
    if vx==0x680:
        # vsubuws: unsigned word subtract saturating at 0, sets VSCR[SAT] (AltiVec PEM).
        body=out+'bool saturated=false;'+f'for(unsigned i=0;i<4;++i){{const auto x=vmx_word({reg(a)},i),y=vmx_word({reg(b)},i);saturated|=x<y;vmx_word(out,i,x<y?0u:x-y);}}'
        return 'vsubuws',body+'if(saturated)state.vscr|=1;'+finish
    if vx in (0x80,0x480,0x184,0x284,0x384):
        left=f'vmx_word({reg(a)},i)';right=f'vmx_word({reg(b)},i)'
        expression={0x80:f'{left}+{right}',0x480:f'{left}-{right}',0x184:f'{left}<<({right}&31)',
                    0x284:f'{left}>>({right}&31)',0x384:f'static_cast<std::int32_t>({left})>>({right}&31)'}[vx]
        return 'vector_word_arithmetic',out+f'for(unsigned i=0;i<4;++i)vmx_word(out,i,{expression});'+finish
    if vx&0x3ff in (0x86,0x286,0x386,0xc6,0x1c6,0x2c6):
        kind=vx&0x3ff
        left=f'vmx_word({reg(a)},i)';right=f'vmx_word({reg(b)},i)'
        if kind==0x386:left=f'static_cast<std::int32_t>({left})';right=f'static_cast<std::int32_t>({right})'
        match=f'{left}{"==" if kind==0x86 else ">"}{right}'
        if kind in (0xc6,0x1c6,0x2c6):match=f'vmx_compare({left},{right},{kind})'
        body=out+'unsigned yes=0;'+f'for(unsigned i=0;i<4;++i){{bool match={match};yes+=match;vmx_word(out,i,match?UINT32_MAX:0);}}'
        body+=('state.cr=(state.cr&~UINT32_C(0xf0))|((yes==4?8u:yes==0?2u:0u)<<4);' if vx&0x400 else '(void)yes;')
        return 'vector_word_compare',body+finish
    return None


CPP=r'''
[[maybe_unused]] static std::uint32_t vmx_word(const std::array<std::uint8_t,16>& r,unsigned i){
    i*=4;return (std::uint32_t(r[i])<<24)|(std::uint32_t(r[i+1])<<16)|(std::uint32_t(r[i+2])<<8)|r[i+3];
}
[[maybe_unused]] static void vmx_word(std::array<std::uint8_t,16>& r,unsigned i,std::uint32_t value){
    for(unsigned j=0;j<4;++j)r[i*4+j]=value>>(24-j*8);
}
struct VmxFloatScope {
    PpuState& state;std::uint32_t saved;
    explicit VmxFloatScope(PpuState& s):state(s),saved(s.fpscr){
        if(s.vscr&0x10000)throw std::runtime_error("VMX non-Java arithmetic mode unsupported");
        s.fpscr=0;
    }
    ~VmxFloatScope(){state.fpscr=saved;}
};
[[maybe_unused]] static std::uint32_t vmx_add(PpuState& s,std::uint32_t a,std::uint32_t b,bool subtract){
    const auto x=a&0x7fffffff,y=b&0x7fffffff;
    if(x>0x7f800000)return a|0x400000;
    if(y>0x7f800000)return b|0x400000;
    if(subtract)b^=0x80000000;
    if(x==0x7f800000){if(y==0x7f800000 && ((a^b)>>31))return 0x7fc00000;return a;}
    if(y==0x7f800000)return b;
    return fp_narrow_exact(fp_add(s,fp_widen(a),fp_widen(b),24));
}
[[maybe_unused]] static std::uint32_t vmx_fused(PpuState& s,std::uint32_t a,std::uint32_t b,std::uint32_t c,bool negative){
    const auto x=a&0x7fffffff,y=b&0x7fffffff,z=c&0x7fffffff;
    if(x>0x7f800000)return a|0x400000;
    if(y>0x7f800000)return b|0x400000;
    if(z>0x7f800000)return c|0x400000;
    if(negative)b^=0x80000000;
    const auto product_sign=(a^c)&0x80000000,negation=negative?0x80000000u:0u;
    if(x==0x7f800000 || z==0x7f800000){
        if(!x || !z || (y==0x7f800000 && ((b^product_sign)>>31)))return 0x7fc00000;
        return (product_sign^negation)|0x7f800000;
    }
    if(y==0x7f800000)return b^negation;
    return fp_narrow_exact(fp_fused(s,fp_widen(a),fp_widen(c),fp_widen(b),24,negative));
}
[[maybe_unused]] static bool vmx_compare(std::uint32_t a,std::uint32_t b,unsigned kind){
    const auto x=a&0x7fffffff,y=b&0x7fffffff;
    if(x>0x7f800000 || y>0x7f800000)return false;
    const bool equal=a==b || (!x && !y);
    if(kind==0xc6)return equal;
    const bool greater=equal?false:((a^b)&0x80000000)?!(a>>31):(a>>31)?a<b:a>b;
    return greater || (kind==0x1c6 && equal);
}
[[maybe_unused]] static std::uint32_t vmx_extreme(std::uint32_t a,std::uint32_t b,bool maximum){
    // NaN from vA, then vB, quieted; max(+0,-0)=+0 and min(+0,-0)=-0.
    if((a&0x7fffffff)>0x7f800000)return a|0x400000;
    if((b&0x7fffffff)>0x7f800000)return b|0x400000;
    if(!((a|b)&0x7fffffff))return maximum?(a&b):(a|b);
    return vmx_compare(a,b,0x2c6)==maximum?a:b;
}
[[maybe_unused]] static std::uint32_t vmx_reciprocal(PpuState& s,std::uint32_t bits){
    // Compatibility choice: nearest-even reciprocal rather than the Cell's
    // implementation-specific estimate table. Low result bits can differ.
    const auto magnitude=bits&0x7fffffff,sign=bits&0x80000000;
    if(magnitude>0x7f800000)return bits|0x400000;
    if(magnitude==0x7f800000)return sign;
    if(!magnitude)return sign|0x7f800000;
    return fp_narrow_exact(fp_divide(s,UINT64_C(0x3ff0000000000000),fp_widen(bits),24));
}
[[maybe_unused]] static std::uint32_t vmx_reciprocal_root(PpuState& s,std::uint32_t bits){
    // Compatibility choice as vrefp: 1/sqrt from rounded binary32 steps
    // instead of the Cell estimate table. Low result bits can differ.
    const auto magnitude=bits&0x7fffffff,sign=bits&0x80000000;
    if(magnitude>0x7f800000)return bits|0x400000;
    if(!magnitude)return sign|0x7f800000;
    if(sign)return 0x7fc00000;
    if(magnitude==0x7f800000)return 0;
    return fp_narrow_exact(fp_divide(s,UINT64_C(0x3ff0000000000000),fp_sqrt(s,fp_widen(bits),24),24));
}
[[maybe_unused]] static std::uint32_t vmx_to_integer(std::uint32_t bits,unsigned scale,bool sign,std::uint32_t& status){
    const auto magnitude=bits&0x7fffffff;const bool negative=bits>>31;
    if(magnitude>0x7f800000)return 0; // NaN is zero and does not set saturation.
    if(!magnitude)return 0;
    if(!sign && negative){status|=1;return 0;}
    const auto exp=(bits>>23)&255;const int exponent=int(exp)-127+int(scale);
    if(exponent<0)return 0;
    const std::uint64_t limit=sign?(negative?UINT64_C(0x80000000):UINT64_C(0x7fffffff)):UINT32_MAX;
    const std::uint64_t mantissa=(bits&0x7fffff)|0x800000;
    const std::uint64_t value=exponent>32?UINT64_MAX:exponent>=23?mantissa<<(exponent-23):mantissa>>(23-exponent);
    if(value>limit){status|=1;return negative?0x80000000:std::uint32_t(limit);}
    return negative?0u-std::uint32_t(value):std::uint32_t(value);
}
'''
