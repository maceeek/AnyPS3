"""Bounded sysPrxForUser formatter for the verified firmware 3.01 ABI.

liblv2-relocated.elf SHA256:
843006f84aa0f195ae85119e891238f896ae8f9faf339904709a91bd3be9bdfc.
Wrappers: printf 0x280029f4, vprintf 0x28002b90, snprintf 0x28004f64,
vsnprintf 0x28005030. Formatter 0x280050a4, bounded sink 0x28005d9c.
The firmware returns the full count, but terminates at dst+full_count even
after truncation! This differs from RPCS3 sys_libc_.cpp:417 and ISO snprintf.
h/hh do not narrow integers in this firmware; l is 32-bit, ll is 64-bit.
z, FP, wide, positional and write-count conversions explicitly fault.
"""

IMPORTS = {
    ("sysPrxForUser", "0x9f04f7af"): ("_sys_printf", "guest_printf_service(state,false,false);"),
    ("sysPrxForUser", "0xfa7f693d"): ("_sys_vprintf", "guest_printf_service(state,false,true);"),
    ("sysPrxForUser", "0x06574237"): ("_sys_snprintf", "guest_printf_service(state,true,false);"),
    ("sysPrxForUser", "0x0618936b"): ("_sys_vsnprintf", "guest_printf_service(state,true,true);"),
}

CPP = r'''
// Each limit is diagnostic: exceeding one faults instead of returning success.
[[maybe_unused]] static constexpr std::size_t GUEST_PRINTF_LIMIT=1024*1024;
[[maybe_unused]] static std::uint64_t guest_printf_address(std::uint64_t base,std::size_t offset){
    if(base>UINT32_MAX || offset>UINT32_MAX-base)
        throw std::runtime_error("guest printf address overflow");
    return base+offset;
}
[[maybe_unused]] static std::string guest_printf_string(PpuState& s,std::uint32_t address,
                                                       std::size_t limit,bool require_nul){
    std::string out;
    for(std::size_t i=0;i<limit;++i){
        const auto c=s.memory.load(guest_printf_address(address,i),1);
        if(!c)return out;
        out.push_back(static_cast<char>(c));
    }
    if(require_nul)throw std::runtime_error("guest printf string exceeds diagnostic limit");
    return out;
}
struct GuestPrintfArgs {
    PpuState& state;
    bool from_list;
    unsigned slot;
    std::uint32_t list;
    std::uint64_t next(){
        if(slot>=GUEST_PRINTF_LIMIT/8)throw std::runtime_error("guest printf argument limit");
        const auto i=slot++;
        if(from_list)return state.memory.load(guest_printf_address(list,8*std::size_t(i)),8);
        if(i<8)return state.r[3+i];
        // Original wrapper saves r4 at caller SP+0x38, r6 at SP+0x48.
        // r11 (slot 8, zero based) is therefore at caller SP+0x70.
        return state.memory.load(guest_printf_address(std::uint32_t(state.r[1]),0x30+8*std::size_t(i)),8);
    }
};
[[maybe_unused]] static void guest_printf_append(std::string& out,const std::string& value){
    if(value.size()>GUEST_PRINTF_LIMIT-out.size())throw std::runtime_error("guest printf output limit");
    out+=value;
}
[[maybe_unused]] static std::string guest_printf_format(PpuState& s,std::uint32_t format,GuestPrintfArgs& args){
    const auto fmt=guest_printf_string(s,format,GUEST_PRINTF_LIMIT,true);
    std::string out;
    std::size_t at=0;
    while(at<fmt.size()){
        if(fmt[at]!='%'){guest_printf_append(out,std::string(1,fmt[at++]));continue;}
        ++at;
        if(at<fmt.size() && fmt[at]=='%'){guest_printf_append(out,"%");++at;continue;}
        bool left=false,zero=false,plus=false,space=false,alternate=false;
        while(at<fmt.size()){
            const char f=fmt[at];
            if(f=='-')left=true;else if(f=='0')zero=true;else if(f=='+')plus=true;
            else if(f==' ')space=true;else if(f=='#')alternate=true;else break;
            ++at;
        }
        auto decimal=[&](){
            unsigned value=0;
            while(at<fmt.size() && fmt[at]>='0' && fmt[at]<='9'){
                value=value*10+unsigned(fmt[at++]-'0');
                if(value>GUEST_PRINTF_LIMIT)throw std::runtime_error("guest printf field limit");
            }
            return value;
        };
        unsigned width=0;
        if(at<fmt.size() && fmt[at]=='*'){
            ++at;const auto bits=std::uint32_t(args.next());
            if(bits&0x80000000u){left=true;width=0u-bits;}else width=bits;
        }else width=decimal();
        int precision=-1;
        if(at<fmt.size() && fmt[at]=='.'){
            ++at;
            if(at<fmt.size() && fmt[at]=='*'){
                ++at;const auto bits=std::uint32_t(args.next());
                if(!(bits&0x80000000u)){
                    if(bits>GUEST_PRINTF_LIMIT)throw std::runtime_error("guest printf precision limit");
                    precision=int(bits);
                }
            }else precision=int(decimal());
        }
        if(width>GUEST_PRINTF_LIMIT)throw std::runtime_error("guest printf width limit");
        enum Length { word,short_word,char_word,long_word,long_long } length=word;
        if(at<fmt.size() && fmt[at]=='h'){
            ++at;length=short_word;
            if(at<fmt.size() && fmt[at]=='h'){++at;length=char_word;}
        }else if(at<fmt.size() && fmt[at]=='l'){
            ++at;length=long_word;
            if(at<fmt.size() && fmt[at]=='l'){++at;length=long_long;}
        }
        if(at==fmt.size())throw std::runtime_error("incomplete guest printf conversion");
        const char conv=fmt[at++];
        if(conv!='d' && conv!='i' && conv!='u' && conv!='x' && conv!='X' && conv!='o' &&
           conv!='c' && conv!='s' && conv!='p')
            throw std::runtime_error("unsupported guest printf conversion: "+std::string(1,conv));
        if((conv=='s' || conv=='c' || conv=='p') && length!=word)
            throw std::runtime_error("unsupported guest printf pointer/character length");
        const auto raw=args.next();
        std::string digits,prefix;
        if(conv=='s'){
            const auto address=std::uint32_t(raw);
            const auto count=precision<0?GUEST_PRINTF_LIMIT:std::size_t(precision);
            if(address)digits=guest_printf_string(s,address,count,precision<0);
            else digits=std::string("(null)").substr(0,count); // FW TOC 0x28020c18.
        }else if(conv=='c')digits.push_back(static_cast<char>(std::uint8_t(raw)));
        else {
            const bool wide=length==long_long;
            const bool signed_decimal=conv=='d' || conv=='i';
            std::uint64_t value=wide?raw:std::uint32_t(raw);
            if(signed_decimal){
                const bool negative=wide?bool(value>>63):bool(value&0x80000000u);
                if(negative){prefix="-";value=wide?0-value:std::uint32_t(0-value);}
                else if(plus)prefix="+";else if(space)prefix=" ";
            }
            const unsigned base=conv=='o'?8:(conv=='x' || conv=='X' || conv=='p'?16:10);
            const char* alphabet=conv=='X'?"0123456789ABCDEF":"0123456789abcdef";
            if(value || precision!=0){
                auto rest=value;
                do{digits.push_back(alphabet[rest%base]);rest/=base;}while(rest);
                std::reverse(digits.begin(),digits.end());
                if(conv=='o' && alternate && digits[0]!='0')digits.insert(0,1,'0');
            }
            if(conv=='p')prefix="0x";
            else if(base==16 && alternate && value)prefix=conv=='X'?"0X":"0x";
            // FW precision counts the sign and hex prefix as well as digits
            // (0x280055dc..0x28005618), unlike ISO printf.
            const auto size=prefix.size()+digits.size();
            if(precision>=0 && std::size_t(precision)>size)digits.insert(0,std::size_t(precision)-size,'0');
            if(precision>=0)zero=false;
        }
        const auto size=prefix.size()+digits.size();
        const auto pad=width>size?width-size:0;
        if(pad>GUEST_PRINTF_LIMIT-size)throw std::runtime_error("guest printf field limit");
        std::string field;
        if(!left && !zero)field.append(pad,' ');
        field+=prefix;
        if(!left && zero)field.append(pad,'0');
        field+=digits;
        if(left)field.append(pad,' ');
        guest_printf_append(out,field);
    }
    return out;
}
[[maybe_unused]] static void guest_printf_service(PpuState& s,bool bounded,bool va_list){
    const auto dst=std::uint32_t(s.r[3]),capacity=std::uint32_t(s.r[4]);
    const auto format=std::uint32_t(s.r[bounded?5:3]);
    GuestPrintfArgs args{s,va_list,va_list?0u:(bounded?3u:1u),std::uint32_t(s.r[bounded?6:4])};
    auto result=guest_printf_format(s,format,args);
    if(bounded){
        if(capacity>0x7fffffffu)throw std::runtime_error("guest snprintf capacity exceeds supported signed range");
        if(capacity){
            const auto size=std::min<std::size_t>(capacity-1,result.size());
            const auto nul=guest_printf_address(dst,result.size());
            // Validate terminator before committing any bytes. Memory faults
            // are explicit even when the firmware's terminator is out of cap.
            s.memory.access(nul,1,true);
            if(size)s.memory.transfer(dst,reinterpret_cast<std::uint8_t*>(result.data()),unsigned(size),true);
            s.memory.store(nul,1,0);
        }
    }else{
        if(std::fputs("guest printf: ",stderr)==EOF ||
           std::fwrite(result.data(),1,result.size(),stderr)!=result.size() || std::fflush(stderr)!=0)
            throw std::runtime_error("guest printf host stderr write failed");
    }
    s.r[3]=result.size();
}
'''
