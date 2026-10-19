"""LV2 syscall extension group 'rsx_net' (see docs/knowledge/experiments/2026-10-15-split-units-256.md).

syscall_rsx_net_ext() returns true only for syscalls it handled; unknown numbers fall
through to the next group and finally to the explicit 'unimplemented LV2 syscall' stop.
"""

TYPES = r'''
struct Lv2RsxNetExt {
    // No host socket, interface, routing or packet backend. These are real
    // unbound guest socket objects, not successful network connections.
    struct Socket {std::uint32_t type,protocol;std::int32_t nbio=0;};
    std::map<std::int32_t,Socket> sockets;
};
'''

CPP = r'''
// Evidence: RPCS3 ffba2bf09ef3980638f9327b25240d81414034ab,
// Emu/Cell/lv2/sys_net.{cpp,h}, sys_net/lv2_socket_native.cpp. Firmware 3.01
// libnet 0x20012c04..0x20012c48 negates a negative BSD errno itself.
static constexpr unsigned RSX_NET_RECV_WAIT=707;
static unsigned rsx_net_interrupt(PpuState& s,std::int32_t fd){
    unsigned count=0;
    for(auto& t:s.ppu_threads)if(t.wait_kind==RSX_NET_RECV_WAIT && t.wait_id==static_cast<std::uint32_t>(fd)){
        t.wait_kind=0;t.wait_id=0;t.r[3]=static_cast<std::uint64_t>(std::int64_t(-4));
        // A blocked syscall has not advanced PC. Complete it with EINTR,
        // preserving its receive buffers and all other argument registers.
        t.pc+=4;++count;s.reschedule=true;
    }
    return count;
}
static std::int32_t rsx_net_syscall(PpuState& s){
    auto& r=s.r;auto& m=s.memory;auto& sockets=s.rsx_net_ext.sockets;
    const auto fd=static_cast<std::int32_t>(r[3]);
    auto sock=sockets.find(fd);
    switch(r[11]){
    case 713:{
        const auto family=static_cast<std::int32_t>(r[3]),type=static_cast<std::int32_t>(r[4]),protocol=static_cast<std::int32_t>(r[5]);
        if(type!=1 && type!=2 && type!=3 && type!=6 && type!=10)return -43;
        if(family!=2 || (type!=1 && type!=2))
            throw std::runtime_error("unsupported offline bnet socket family/type");
        if(protocol && protocol!=(type==1?6:17))
            throw std::runtime_error("unsupported offline bnet socket protocol");
        // The LV2 socket namespace is independent of filesystem descriptors.
        for(std::int32_t id=24;id<1024;++id)if(!sockets.count(id)){
            sockets.emplace(id,Lv2RsxNetExt::Socket{static_cast<std::uint32_t>(type),static_cast<std::uint32_t>(type==1?6:17),0});
            return id;
        }
        return -24;
    }
    case 714:
        if(sock==sockets.end())return -9;
        rsx_net_interrupt(s,fd);sockets.erase(sock);return 0;
    case 711:
        if(static_cast<std::uint32_t>(r[7])<4)return -22;
        if(sock==sockets.end())return -9;
        if(static_cast<std::int32_t>(r[4])!=0xffff || static_cast<std::int32_t>(r[5])!=0x1100)
            throw std::runtime_error("unsupported offline bnet socket option");
        sock->second.nbio=static_cast<std::int32_t>(m.load(static_cast<std::uint32_t>(r[6]),4));return 0;
    case 712:
        if(static_cast<std::int32_t>(r[4])<0 || static_cast<std::int32_t>(r[4])>2)return -22;
        if(sock==sockets.end())return -9;
        // No bind/connect syscall is provided by this backend. All our
        // sockets are newly created and have no peer to shut down.
        return -57;
    case 707:{
        const auto buf=static_cast<std::uint32_t>(r[4]),len=static_cast<std::uint32_t>(r[5]);
        const auto flags=static_cast<std::uint32_t>(r[6]);
        const auto addr=static_cast<std::uint32_t>(r[7]),addrlen=static_cast<std::uint32_t>(r[8]);
        if(!buf || !len || bool(addr)!=bool(addrlen))return -22;
        if(flags&~0xcc2u)throw std::runtime_error("unsupported bnet recvfrom flags");
        if(sock==sockets.end())return -9;
        if(sock->second.type==1)return -57;
        // No packets exist; do not overwrite buffer, address or length on error.
        if(sock->second.nbio || (flags&0x80))return -35;
        if(flags&0xc00)throw std::runtime_error("offline bnet crypto receive is unsupported");
        if(s.ppu_threads.size()<2)throw std::runtime_error("blocking bnet receive requires active peer/scheduler");
        ppu_block(s,RSX_NET_RECV_WAIT,static_cast<std::uint32_t>(fd));
    }
    case 710:{
        const auto flags=static_cast<std::uint32_t>(r[6]);
        const auto addr=static_cast<std::uint32_t>(r[7]),addrlen=static_cast<std::uint32_t>(r[8]);
        if(flags&~0xcc0u)throw std::runtime_error("unsupported bnet sendto flags");
        if(addr && addrlen<16)return -22;
        if(addr && m.load(std::uint64_t(addr)+1,1)!=2)return -47;
        if(addr)m.access(addr,16,false);
        const auto buf=static_cast<std::uint32_t>(r[4]),len=static_cast<std::uint32_t>(r[5]);
        if(len)m.access(buf,len,false);
        if(sock==sockets.end())return -9;
        if(addr){
            const auto ip=m.load(std::uint64_t(addr)+4,4),first=ip>>24,second=(ip>>16)&255;
            const bool private_ip=first==10 || first==127 || (first==172 && second>=16 && second<=31) || (first==192 && second==168);
            // Same disconnected public-address gate as lv2_socket_native::sendto.
            // Do not invent ENETUNREACH for local/broadcast/loopback routes.
            if(!private_ip)return -49;
        }
        if(sock->second.type==1)throw std::runtime_error("offline bnet stream send error is not verified");
        if(!addr)return -39;
        throw std::runtime_error("offline bnet sendto has no verified route/backend");
    }
    case 709:
        if(static_cast<std::uint32_t>(r[5])&~0xcc0u)throw std::runtime_error("unsupported bnet sendmsg flags");
        if(sock==sockets.end())return -9;
        throw std::runtime_error("offline bnet sendmsg is unsupported");
    case 715:{
        const auto count=static_cast<std::int32_t>(r[4]);
        if(count<=0)return 0;
        if(count>1024)throw std::runtime_error("offline bnet poll exceeds supported descriptor count");
        const auto out=static_cast<std::uint32_t>(r[3]);m.access(out,count*8,true);
        // Handle the independently documented invalid/ignored descriptor
        // cases only. Readiness of a live hostless socket is not guessed.
        std::vector<std::uint16_t> events(count,0);std::int32_t ready=0;
        for(std::int32_t i=0;i<count;++i){
            const auto id=static_cast<std::int32_t>(m.load(std::uint64_t(out)+i*8,4));
            if(id>=0){
                if(sockets.count(id))throw std::runtime_error("offline bnet poll on live socket is unsupported");
                events[i]=0x20;++ready;
            }
        }
        if(!ready && static_cast<std::int32_t>(r[5])!=0)
            throw std::runtime_error("timed/blocking offline bnet poll is unsupported");
        for(std::int32_t i=0;i<count;++i)m.store(std::uint64_t(out)+i*8+6,2,events[i]);
        return ready;
    }
    case 721:{
        const auto type=static_cast<std::int32_t>(r[3]);const auto flags=static_cast<std::uint32_t>(r[5]);
        if(type<0 || type>4)return -22;
        if(type==4){
            const auto count=sockets.size();
            for(const auto& item:sockets)rsx_net_interrupt(s,item.first);
            sockets.clear();return static_cast<std::int32_t>(count);
        }
        if(type!=0)throw std::runtime_error("unsupported bnet resolver/other abort type");
        const auto id=static_cast<std::int32_t>(r[4]);if(!sockets.count(id))return -9;
        if(flags&~1u)throw std::runtime_error("unsupported bnet abort flags");
        const auto count=rsx_net_interrupt(s,id);return !count && (flags&1)?-2:0;
    }
    case 722:{
        if(static_cast<std::int32_t>(r[3])!=6)throw std::runtime_error("unsupported bnet infoctl command "+std::to_string(fd));
        const auto arg=static_cast<std::uint32_t>(r[4]);if(!arg)return -22;
        const auto id=static_cast<std::int32_t>(m.load(arg,4));
        const auto out=static_cast<std::uint32_t>(m.load(std::uint64_t(arg)+12,4));
        const auto count=static_cast<std::int32_t>(m.load(std::uint64_t(arg)+16,4));
        if(id==-1 && !out)return static_cast<std::int32_t>(sockets.size());
        if((id==-1 && count<0) || (id!=-1 && (!out || count<1)))return -22;
        if(id!=-1 && !sockets.count(id))return -9;
        // sys_net_sockinfo_t is 36 BE bytes. Every supported socket is
        // unbound, peerless, has empty queues and state CREATED (2).
        const auto n=id==-1?std::min<std::size_t>(sockets.size(),count):1;
        if(n)m.access(out,n*36,true);
        std::size_t i=0;
        for(const auto& item:sockets){
            if((id!=-1 && item.first!=id) || i==n)continue;
            const auto base=std::uint64_t(out)+i++*36;
            for(unsigned offset=0;offset<36;offset+=4)m.store(base+offset,4,0);
            m.store(base,4,item.first);m.store(base+4,4,item.second.protocol);m.store(base+32,4,2);
        }
        return id==-1?static_cast<std::int32_t>(n):0;
    }
    // RPCS3 supplies only TODO stubs for recvmsg, dump, control, ioctl,
    // sysctl; their zero return does not demonstrate a performed operation.
    default:throw std::runtime_error("unsupported offline bnet syscall "+std::to_string(r[11]));
    }
}
[[maybe_unused]] static bool syscall_rsx_net_ext(PpuState& state) {
    switch(state.r[11]){
    case 669:case 671:case 673:case 676:case 677:
        state.r[3]=rsx_syscall(state);return true;
    default:break;
    }
    if(state.r[11]>=707 && state.r[11]<=725){
        state.r[3]=static_cast<std::uint64_t>(std::int64_t(rsx_net_syscall(state)));return true;
    }
    return false;
}
'''
