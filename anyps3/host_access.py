"""Provenance captured at the guest operation, preserved through owner RPC."""
CPP = r'''
struct HostAccessOrigin {
    enum class Engine {Host,Ppu,Spu,Dma,Rsx};
    Engine engine=Engine::Host;
    std::uint32_t guest_id=0,guest_pc=0;
    std::uint64_t ea=UINT64_MAX,operation_id=0;
    const char* name()const{
        switch(engine){case Engine::Host:return "host";case Engine::Ppu:return "ppu";
        case Engine::Spu:return "spu";case Engine::Dma:return "dma";case Engine::Rsx:return "rsx";}
        return "invalid";
    }
};
struct HostAccessContext {
    HostAccessOrigin origin;
    const std::uint32_t* id=nullptr;const std::uint32_t* pc=nullptr;
    const std::uint64_t* operation=nullptr;const unsigned* in_block=nullptr;
    inline static thread_local HostAccessContext* current=nullptr;
    static HostAccessOrigin sample(){
        if(!current)return {};
        auto value=current->origin;
        if(current->id)value.guest_id=*current->id;
        if(current->pc)value.guest_pc=*current->pc;
        if(current->operation)value.operation_id=*current->operation;
        if(current->in_block)value.operation_id+=*current->in_block;
        return value;
    }
    HostAccessContext* previous;
    explicit HostAccessContext(HostAccessOrigin value):origin(value),previous(current){current=this;}
    HostAccessContext(HostAccessOrigin::Engine engine,const std::uint32_t& tid,const std::uint32_t& address,const std::uint64_t* sequence=nullptr,const unsigned* block=nullptr):
        id(&tid),pc(&address),operation(sequence),in_block(block),previous(current){origin.engine=engine;current=this;}
    ~HostAccessContext(){current=previous;}
    HostAccessContext(const HostAccessContext&)=delete;
    struct Address {
        HostAccessContext* context;std::uint64_t previous=UINT64_MAX;
        explicit Address(std::uint64_t ea,bool enabled=true):context(enabled?current:nullptr){if(context){previous=context->origin.ea;context->origin.ea=ea;}}
        ~Address(){if(context)context->origin.ea=previous;}
        Address(const Address&)=delete;
    };
};
'''
