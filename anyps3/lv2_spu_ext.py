"""LV2 SPU services; no service succeeds without changing or reading real state.

Reference: RPCS3 ffba2bf0, lv2/sys_spu.cpp and sys_spu.h. Unknown
system scheduling calls 248/249 deliberately remain explicit runtime stops.
"""

TYPES = r'''
struct Lv2SpuExt {
    struct Image {
        std::uint32_t id,entry;
        std::shared_ptr<std::vector<std::uint8_t>> ls;
    };
    std::vector<Image> images;
    std::uint32_t next_image=0x23000100;
};
'''

CPP = r'''
static std::uint32_t spu_close_kernel_image(PpuState& s,std::uint32_t image){
    if(s.memory.load(image,4)!=1)return MM_EINVAL;
    const auto id=s.memory.load(image+4,4);
    auto& images=s.spu_ext.images;
    const auto it=std::find_if(images.begin(),images.end(),[&](const auto& img){return img.id==id;});
    if(it==images.end())return MM_ESRCH;
    // Threads own a shared reference to initial_ls, independently of the handle.
    images.erase(it);return 0;
}

// Queue.type must be supplied by the event runtime. Older event runtimes
// cannot distinguish PPU/SPU queues, so binding must stop rather than guess.
template<class Q> static auto spu_queue_type(const Q& q,int)->decltype(q.type){return q.type;}
template<class Q> static unsigned spu_queue_type(const Q&,long){
    throw std::runtime_error("SPU queue binding requires Events::Queue::type (1=PPU, 2=SPU)");
}

static std::uint32_t spu_open_image(PpuState& s){
    auto& m=s.memory;const auto out=s.r[3];
    if(!out)return 0x8001000d;
    m.validate(out,16,true);
    std::vector<std::uint8_t> bytes;
    try{
        const auto path=s.fs.resolve(native_fs_path(s,s.r[4]));
        const int fd=::open(path.host.c_str(),O_RDONLY);
        if(fd<0)return NativeFilesystem::host_error(errno);
        struct Close {int fd;~Close(){::close(fd);}} close{fd};
        struct stat st{};if(::fstat(fd,&st))return NativeFilesystem::host_error(errno);
        if(!S_ISREG(st.st_mode))return 0x80010007;
        if(st.st_size<52)return 0x80010007;
        if(st.st_size>16*1024*1024)throw std::runtime_error("SPU ELF file exceeds diagnostic size bound");
        bytes.resize(st.st_size);std::size_t done=0;
        while(done<bytes.size()){
            const auto n=::read(fd,bytes.data()+done,bytes.size()-done);
            if(n<0){if(errno==EINTR)continue;return NativeFilesystem::host_error(errno);}
            if(!n)return 0x80010007;
            done+=n;
        }
    }catch(const NativeFilesystem::Error& e){return e.code;}
    auto get=[&](std::size_t off,unsigned size)->std::uint32_t{
        if(off>bytes.size() || size>bytes.size()-off)throw std::runtime_error("truncated SPU ELF field");
        std::uint32_t v=0;for(unsigned i=0;i<size;++i)v=(v<<8)|bytes[off+i];return v;
    };
    if(get(0,4)==0x53434500)throw std::runtime_error("encrypted SPU SELF requires offline decryption and AOT translation");
    if(get(0,4)!=0x7f454c46 || get(4,1)!=1 || get(5,1)!=2 || get(6,1)!=1 ||
       get(16,2)!=2 || get(18,2)!=23 || get(20,4)!=1 || get(40,2)!=52 || get(42,2)!=32)return 0x80010007;
    const auto entry=get(24,4),phoff=get(28,4),count=get(44,2);
    if(entry>0x3fffc || entry%4 || !count || count>128 || phoff<52 ||
       phoff>bytes.size() || count*32>bytes.size()-phoff)return 0x80010007;
    auto ls=std::make_shared<std::vector<std::uint8_t>>(0x40000,0);
    std::vector<std::pair<std::uint32_t,std::uint32_t>> spans;bool copied=false,executable=false;
    for(unsigned i=0;i<count;++i){
        const auto p=phoff+32*i,type=get(p,4),offset=get(p+4,4),base=get(p+8,4),
            filesz=get(p+16,4),memsz=get(p+20,4),flags=get(p+24,4);
        if(offset>bytes.size() || filesz>bytes.size()-offset)return 0x80010007;
        if(type==4)continue;
        if(type!=1 || filesz>memsz || base>0x40000 || memsz>0x40000-base)return 0x80010007;
        for(const auto& span:spans)if(memsz && base<span.second && span.first<base+memsz)return 0x80010007;
        spans.push_back({base,base+memsz});
        std::copy_n(bytes.data()+offset,filesz,ls->data()+base);copied|=filesz!=0;
        executable|=(flags&1) && entry>=base && entry+4<=base+filesz;
    }
    if(!copied || !executable)return 0x80010007;
    NativeSpu::Thread probe;probe.entry=entry;
    // Use the same full-LS fingerprint gate as thread startup; instruction
    // dispatch continues to check exact original words at every executed PC.
    spu_prepare(s,probe,ls->data());
    const auto id=s.spu_ext.next_image;
    s.spu_ext.images.push_back({id,entry,std::move(ls)});s.spu_ext.next_image+=0x100;
    m.store(out,4,1);m.store(out+4,4,id);m.store(out+8,4,0);m.store(out+12,4,0);
    return 0;
}

[[maybe_unused]] static bool syscall_spu_ext(PpuState& s) {
    auto& r=s.r;auto& m=s.memory;const auto op=r[11];
    switch(op){
    case 156:case 158:case 165:case 166:case 174:case 175:case 176:case 177:
    case 179:case 180:case 181:case 182:case 190:case 191:case 192:case 193:case 194:
    case 248:case 249:case 250:break;
    default:return false;
    }
    const bool thread_control=op==165 || op==166 || op==181 || op==182 || (op>=190 && op<=194);
    std::unique_ptr<SpuOwnerScope> control;
    if(!(s.process.spu_worker.pool_mode && (op==156 || op==158 || op==190)))
        control=std::make_unique<SpuOwnerScope>(s,std::uint32_t(r[3]),thread_control,false);
    const auto result=[&]()->std::uint32_t{
        if(op==248 || op==249)throw std::runtime_error("undocumented SPU system scheduling syscall "+std::to_string(op)+" (libsre firmware 3.01)");
        if(op==156)return spu_open_image(s);
        if(op==158)return spu_close_kernel_image(s,std::uint32_t(r[3]));
        NativeSpu::Group* g=nullptr;NativeSpu::Thread* t=nullptr;unsigned index=0;
        const bool thread_op=op==165 || op==166 || op==181 || op==182 || (op>=190 && op<=194);
        for(auto& group:s.spu.groups){
            if(!thread_op && group.id==std::uint32_t(r[3]))g=&group;
            if(thread_op)for(unsigned i=0;i<group.threads.size();++i)if(group.threads[i].id==std::uint32_t(r[3])){g=&group;t=&group.threads[i];index=i;}
        }
        if(op==181 || op==182){
            const auto lsa=std::uint32_t(r[4]),size=std::uint32_t(r[6]);
            if((size!=1 && size!=2 && size!=4 && size!=8) || lsa>=0x40000 || lsa%size || size>0x40000-lsa)return MM_EINVAL;
        }
        if(!g)return MM_ESRCH;
        if(op==165){
            if(!t->exit_status_ready)return 0x8001000f;
            if(!r[4])return 0x8001000d;
            m.store(r[4],4,t->exit_status);return 0;
        }
        if(op==166){
            m.validate(r[4],32,false);for(unsigned i=0;i<4;++i)t->arguments[i]=m.load(r[4]+8*i,8);return 0;
        }
        if(op==181 || op==182){
            if(g->state<3 || g->state>6)return 0x8001000f;
            const auto lsa=std::uint32_t(r[4]),size=std::uint32_t(r[6]);
            if(g->context.size()<(index+1)*0x40000u)throw std::runtime_error("SPU LS context is absent");
            auto* p=g->context.data()+index*0x40000+lsa;
            if(op==181){for(unsigned i=0;i<size;++i)p[i]=r[5]>>(8*(size-1-i));}
            else{
                if(!r[5])return 0x8001000d;
                std::uint64_t v=0;for(unsigned i=0;i<size;++i)v=(v<<8)|p[i];m.store(r[5],8,v);
            }
            return 0;
        }
        if(op==190){
            // RPCS3 SPU inbound mailbox push(value,true) overwrites the last
            // slot when full; this syscall does not wait for free capacity.
            t->in_mailbox.push_overwrite(std::uint32_t(r[4]));
            return 0;
        }
        auto queue=[&](std::uint32_t id){return std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& q){return q.id==id;});};
        if(op==191){
            const auto q=queue(r[4]);if(q==s.events.queues.end())return MM_ESRCH;
            if(r[5]!=1 || r[6]>63)return MM_EINVAL;
            if(queue(t->ports[r[6]])!=s.events.queues.end())return 0x80010015;
            t->ports[r[6]]=r[4];t->event_targets[r[6]]=q->data.state;return 0;
        }
        if(op==192){
            if(r[4]!=1 || r[5]>63)return MM_EINVAL;
            if(queue(t->ports[r[5]])==s.events.queues.end())return 0x80010016;
            t->ports[r[5]]=0;t->event_targets[r[5]].reset();return 0;
        }
        if(op==193){
            const auto q=queue(r[4]);if(q==s.events.queues.end())return MM_ESRCH;
            if(spu_queue_type(*q,0)!=2)return MM_EINVAL;
            for(const auto& b:t->queue_bindings)if(b.second && (b.first==std::uint32_t(r[5]) || b.second==q->id))return MM_EBUSY;
            for(auto& b:t->queue_bindings)if(!b.second){b={std::uint32_t(r[5]),q->id};return 0;}
            return 0x80010001;
        }
        if(op==194){
            for(auto& b:t->queue_bindings)if(b.second && b.first==std::uint32_t(r[4])){b={};return 0;}
            return MM_ESRCH;
        }
        if(op==179){
            if(g->type || r[4]<16 || r[4]>255)return MM_EINVAL;
            g->priority=r[4];return 0;
        }
        if(op==180){if(!r[4])return 0x8001000d;m.store(r[4],4,g->type==0x18?0:g->priority);return 0;}
        if(op==250){
            if(!(g->type&0x20) || g->count>=32 || r[4]>=(UINT64_C(1)<<g->count))return MM_EINVAL;
            throw std::runtime_error("SPU cooperative victim scheduling is not implemented");
        }
        if(op==174 || op==175){
            if(g->type)return MM_EINVAL;
            if(op==174){
                if(g->state==6)g->state=4;
                else if(g->state==3)g->state=5;
                else if(g->state!=4 && g->state!=5)return 0x8001000f;
            }else{
                if(g->state==4)g->state=6;
                else if(g->state==5)g->state=3;
                else return 0x8001000f;
            }
            return 0;
        }
        if(op==176){if(!g->type && g->state!=6)return 0x8001000f;g->schedule_order=++s.spu.schedule_order;s.reschedule=true;return 0;}
        if(op==177){
            if(g->state<=1 || g->state==3 || g->state==5)return 0x8001000f;
            for(auto& thread:g->threads){thread.stopped=true;thread.waiting_receive=false;}
            g->state=1;g->join_cause=4;g->exit_status=r[4];return 0;
        }
        throw std::runtime_error("missing SPU extension dispatch");
    }();
    r[3]=result;return true;
}
'''
