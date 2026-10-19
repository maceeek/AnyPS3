"""Shared physical bytes with a protected raw view and an exact-access view."""
CPP = r'''
// Raw pointers retain the protected view. Only validated byte APIs use the
// second view, after their exact dependency observer has completed. Both map
// the same physical storage: this is not a snapshot or delayed CPU copy.
template<class T> struct PageAlignedAllocator {
    using value_type=T;
    static constexpr std::size_t alignment=4096;
    struct Header {void* alias;std::size_t mapped;};
    PageAlignedAllocator() noexcept=default;
    template<class U> PageAlignedAllocator(const PageAlignedAllocator<U>&) noexcept {}
    T* allocate(std::size_t n){
        if(n>(SIZE_MAX-2*alignment)/sizeof(T))throw std::bad_alloc();
        const std::size_t bytes=std::max<std::size_t>(alignment,(n*sizeof(T)+alignment-1)&~(alignment-1));
#if defined(__linux__)
        const int fd=static_cast<int>(syscall(SYS_memfd_create,"anyps3-guest",1u));
        if(fd<0)throw std::runtime_error("guest backing memfd_create failed");
        if(ftruncate(fd,bytes)){close(fd);throw std::bad_alloc();}
        auto* allocation=static_cast<std::uint8_t*>(mmap(nullptr,bytes+alignment,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0));
        if(allocation==MAP_FAILED){close(fd);throw std::bad_alloc();}
        auto* p=allocation+alignment;
        void* mapped=mmap(p,bytes,PROT_READ|PROT_WRITE,MAP_SHARED|MAP_FIXED,fd,0);
        void* alias=mapped==MAP_FAILED?MAP_FAILED:mmap(nullptr,bytes,PROT_READ|PROT_WRITE,MAP_SHARED,fd,0);
        close(fd);
        if(mapped==MAP_FAILED || alias==MAP_FAILED){munmap(allocation,bytes+alignment);throw std::bad_alloc();}
        *reinterpret_cast<Header*>(p-sizeof(Header))={alias,bytes};
        return reinterpret_cast<T*>(p);
#else
        void* allocation=nullptr;if(posix_memalign(&allocation,alignment,bytes+alignment))throw std::bad_alloc();
        auto* p=static_cast<std::uint8_t*>(allocation)+alignment;
        *reinterpret_cast<Header*>(p-sizeof(Header))={p,bytes};
        std::memset(p+n*sizeof(T),0,bytes-n*sizeof(T));return reinterpret_cast<T*>(p);
#endif
    }
    static T* coherent(T* p){return p?static_cast<T*>((reinterpret_cast<Header*>(p)-1)->alias):nullptr;}
    void deallocate(T* p,std::size_t) noexcept {
        if(!p)return;
        const auto header=*(reinterpret_cast<Header*>(p)-1);
#if defined(__linux__)
        munmap(header.alias,header.mapped);munmap(reinterpret_cast<std::uint8_t*>(p)-alignment,header.mapped+alignment);
#else
        (void)header;std::free(reinterpret_cast<std::uint8_t*>(p)-alignment);
#endif
    }
    template<class U> bool operator==(const PageAlignedAllocator<U>&) const noexcept {return true;}
    template<class U> bool operator!=(const PageAlignedAllocator<U>&) const noexcept {return false;}
};
struct GuestBacking:std::vector<std::uint8_t,PageAlignedAllocator<std::uint8_t>> {
    using Base=std::vector<std::uint8_t,PageAlignedAllocator<std::uint8_t>>;
    using Base::Base;
    std::uint8_t* coherent_data(){return PageAlignedAllocator<std::uint8_t>::coherent(data());}
    const std::uint8_t* coherent_data()const{return PageAlignedAllocator<std::uint8_t>::coherent(const_cast<std::uint8_t*>(data()));}
    std::uint8_t* coherent(const std::uint8_t* pointer){return coherent_data()+(pointer-data());}
    const std::uint8_t* coherent(const std::uint8_t* pointer)const{return coherent_data()+(pointer-data());}
};
struct HostByteAccess {
    inline static thread_local unsigned depth=0;
    bool entered;unsigned previous=0;
    explicit HostByteAccess(bool enter=true,bool override_mode=false):entered(enter || override_mode){if(entered){previous=depth;depth=enter?depth+1:0;}}
    ~HostByteAccess(){if(entered)depth=previous;}
    static bool active(){return depth!=0;}
    HostByteAccess(const HostByteAccess&)=delete;
};
'''
