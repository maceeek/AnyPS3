"""Bounded 16-bit RSX method bank with exact presence and sorted iteration."""
TYPES = r'''

// Explicit FIFO writes have a cheap generation. Legacy mutable borrows keep
// working, including writes through a pointer retained across cache lookups:
// exposed arrays reconcile exact bytes before returning their generation.
template<class T,std::size_t N> class RsxShaderArray {
    std::array<T,N> values{};
    mutable std::array<T,N> observed{};
    mutable std::uint64_t generation=1;
    bool exposed=false;
public:
    RsxShaderArray()=default;
    RsxShaderArray(const RsxShaderArray& other):values(other.values){}
    RsxShaderArray& operator=(const RsxShaderArray& other){if(this!=&other){values=other.values;++generation;}return *this;}
    constexpr std::size_t size()const{return N;}
    const T* data()const{return values.data();}
    T* data(){exposed=true;return values.data();}
    const T& operator[](std::size_t i)const{return values[i];}
    T& operator[](std::size_t i){exposed=true;return values[i];}
    auto begin()const{return values.begin();}auto end()const{return values.end();}
    auto begin(){exposed=true;return values.begin();}auto end(){exposed=true;return values.end();}
    void write(std::size_t i,T value){if(values.at(i)!=value){values[i]=value;++generation;}}
    std::uint64_t version()const{
        if(exposed && std::memcmp(observed.data(),values.data(),sizeof(values))){observed=values;++generation;}
        return generation;
    }
};

struct RsxRegisters {
    using value_type=std::pair<std::uint32_t,std::uint32_t>;
    static constexpr unsigned slots=0x10000/4;
    std::array<value_type,slots> values{};
    std::array<std::uint64_t,slots/64> written{};
    std::size_t count_=0;
    unsigned next(unsigned i)const{
        if(i>=slots)return slots;
        unsigned word=i/64;auto mask=written[word]&(~UINT64_C(0)<<(i%64));
        while(!mask){if(++word>=written.size())return slots;mask=written[word];}
        return word*64+unsigned(__builtin_ctzll(mask));
    }
    template<bool Const> struct Iterator {
        using iterator_category=std::forward_iterator_tag;using value_type=RsxRegisters::value_type;using difference_type=std::ptrdiff_t;
        using reference=std::conditional_t<Const,const value_type&,value_type&>;using pointer=std::conditional_t<Const,const value_type*,value_type*>;
        std::conditional_t<Const,const RsxRegisters*,RsxRegisters*> bank=nullptr;unsigned slot=slots;
        reference operator*()const{return bank->values[slot];}pointer operator->()const{return &bank->values[slot];}
        Iterator& operator++(){slot=bank->next(slot+1);return *this;}Iterator operator++(int){auto old=*this;++*this;return old;}
        bool operator==(const Iterator& b)const{return bank==b.bank && slot==b.slot;}bool operator!=(const Iterator& b)const{return !(*this==b);}
    };
    using iterator=Iterator<false>;using const_iterator=Iterator<true>;
    RsxRegisters()=default;
    RsxRegisters(std::initializer_list<value_type> init){for(const auto& p:init)(*this)[p.first]=p.second;}
    RsxRegisters& operator=(std::initializer_list<value_type> init){clear();for(const auto& p:init)(*this)[p.first]=p.second;return *this;}
    std::size_t size()const{return count_;}bool empty()const{return !count_;}
    bool count(std::uint32_t r)const{return r<0x10000 && !(r&3) && (written[r/256]&(UINT64_C(1)<<((r/4)%64)));}
    iterator begin(){return {this,next(0)};}iterator end(){return {this,slots};}
    const_iterator begin()const{return {this,next(0)};}const_iterator end()const{return {this,slots};}
    iterator find(std::uint32_t r){return {this,count(r)?r/4:slots};}const_iterator find(std::uint32_t r)const{return {this,count(r)?r/4:slots};}
    std::uint32_t& operator[](std::uint32_t r){
        if(r>=0x10000 || (r&3))throw std::runtime_error("invalid RSX register bank address");
        const unsigned slot=r/4;auto& mask=written[slot/64];const auto bit=UINT64_C(1)<<(slot%64);
        if(!(mask&bit)){mask|=bit;values[slot]={r,0};++count_;}return values[slot].second;
    }
    std::uint32_t& at(std::uint32_t r){if(!count(r))throw std::out_of_range("RSX register absent");return values[r/4].second;}
    const std::uint32_t& at(std::uint32_t r)const{if(!count(r))throw std::out_of_range("RSX register absent");return values[r/4].second;}
    unsigned erase(std::uint32_t r){if(!count(r))return 0;written[r/256]&=~(UINT64_C(1)<<((r/4)%64));--count_;return 1;}
    void clear(){written.fill(0);count_=0;}
};
'''
