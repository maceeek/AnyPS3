"""LV2 filesystem extensions over the explicitly mounted native filesystem.

ABI evidence: RPCS3 ffba2bf09ef3980638f9327b25240d81414034ab,
Emu/Cell/lv2/sys_fs.h:77-110 and sys_fs.cpp:1132-1169,1739-1990,
2159-2446,3222-3249,3302-3411,3568-3745. Firmware 3.01 libfs in
native-bundle-271/program.elf confirms syscall numbers and argument registers.
Stub-only reference services deliberately stop rather than claim success.
"""

TYPES = r'''
struct Lv2FsExt {
    struct Entry {std::string name;std::uint8_t type;};
    struct Directory {
        std::string guest;
        bool hdd1=false;
        std::vector<Entry> entries;
        std::size_t position=0;
    };
    std::map<std::uint32_t,Directory> directories;
};
'''

CPP = r'''
[[maybe_unused]] static std::uint32_t fs_ext_operation(PpuState& s) {
    auto& r=s.r;auto& mem=s.memory;const auto number=r[11];
    if(number==800){
        // libfs 0x2018d850: r3=6, r4=0, r5=&fd, r6=4,
        // r7=name buffer, r8=capacity. Other test subcommands are unknown.
        if(r[3]!=6 || r[4]!=0 || r[6]!=4)
            throw std::runtime_error("unsupported sys_fs_test subcommand");
        if(!r[5] || (!r[7] && r[8]))return 0x8001000d;
        const auto fd=std::uint32_t(mem.load(std::uint32_t(r[5]),4));
        const auto file=s.fs.files.find(fd);const auto dir=s.fs_ext.directories.find(fd);
        if(file==s.fs.files.end() && dir==s.fs_ext.directories.end())return 0x8001002a;
        const auto name=file!=s.fs.files.end()?file->second->guest:dir->second.guest;
        // Copy before touching output: the output may alias &fd.
        if(r[8]){
            const auto count=std::min<std::uint64_t>(r[8],name.size()+1);
            auto* out=mem.access(std::uint32_t(r[7]),count,true);
            std::copy_n(name.c_str(),count,out);out[count-1]=0;
        }
        return 0;
    }
    if(number==805){
        const auto guest=native_fs_path(s,r[3]);const auto path=s.fs.resolve(guest);
        if(!r[4])return 0x8001000d;
        mem.access(std::uint32_t(r[4]),4,true);
        unsigned fd=3;while(fd<256 && (s.fs.files.count(fd) || s.fs_ext.directories.count(fd)))++fd;
        if(fd==256)return 0x8001002c;
        const auto host=::opendir(path.host.c_str());
        if(!host)return NativeFilesystem::host_error(errno);
        const std::unique_ptr<DIR,int(*)(DIR*)> handle(host,::closedir);
        Lv2FsExt::Directory directory;directory.guest=guest;
        if(directory.guest.back()!='/')directory.guest+='/';
        directory.hdd1=guest.substr(0,guest.find('/',1))=="/dev_hdd1";
        directory.entries={{".",1},{"..",1}};
        for(;;){
            errno=0;const auto* entry=::readdir(host);
            if(!entry){if(errno)return NativeFilesystem::host_error(errno);break;}
            const std::string name=entry->d_name;if(name=="." || name=="..")continue;
            struct stat info{};
            if(::fstatat(::dirfd(host),name.c_str(),&info,AT_SYMLINK_NOFOLLOW))return NativeFilesystem::host_error(errno);
            if(!S_ISREG(info.st_mode) && !S_ISDIR(info.st_mode))
                throw std::runtime_error("unsupported native directory entry type: "+name);
            if(name.size()>255)throw std::runtime_error("host directory entry exceeds CellFsDirent name limit");
            directory.entries.push_back({name,std::uint8_t(S_ISDIR(info.st_mode)?1:2)});
        }
        std::sort(directory.entries.begin()+2,directory.entries.end(),[](const auto& a,const auto& b){return a.name<b.name;});
        s.fs_ext.directories.emplace(fd,std::move(directory));mem.store(std::uint32_t(r[4]),4,fd);return 0;
    }
    if(number==806 || number==807){
        if(number==806 && (!r[4] || !r[5]))return 0x8001000d;
        const auto it=s.fs_ext.directories.find(std::uint32_t(r[3]));
        if(it==s.fs_ext.directories.end())return 0x8001002a;
        if(number==807){s.fs_ext.directories.erase(it);return 0;}
        // Both ranges are checked before consuming an entry. nread is stored
        // last, as on LV2, including when it overlaps CellFsDirent.
        mem.access(std::uint32_t(r[4]),258,true);mem.access(std::uint32_t(r[5]),8,true);
        auto& dir=it->second;const bool eof=dir.position==dir.entries.size();
        const auto& entry=dir.entries[eof?dir.entries.size()-1:dir.position];
        std::array<std::uint8_t,258> out{};
        out[0]=entry.type;out[1]=entry.name.size();std::copy(entry.name.begin(),entry.name.end(),out.begin()+2);
        // RPCS3 documents HDD1 retaining the last entry at EOF, while HDD0
        // clears its first three bytes (the rest still contains the last name).
        if(eof && !dir.hdd1)out[0]=out[1]=out[2]=0;
        std::copy(out.begin(),out.end(),mem.access(std::uint32_t(r[4]),258,true));
        mem.store(std::uint32_t(r[5]),8,eof?0:258);
        if(!eof)++dir.position;
        return 0;
    }
    if(number==821 || number==832){
        const auto it=s.fs.files.find(std::uint32_t(r[3]));if(it==s.fs.files.end())return 0x8001002a;
        const auto& file=*it->second;
        if(number==832){
            if(!(file.flags&3))return 0x8001002a;
            if(file.path.readonly)return 0x80010026;
            if(r[4]>INT64_MAX)return 0x80010002;
            return ::ftruncate(file.fd,static_cast<off_t>(r[4]))?NativeFilesystem::host_error(errno):0;
        }
        if(!r[4] || !r[5] || !r[6] || !r[7])return 0x8001000d;
        mem.access(std::uint32_t(r[4]),8,true);mem.access(std::uint32_t(r[5]),8,true);
        mem.access(std::uint32_t(r[6]),8,true);mem.access(std::uint32_t(r[7]),4,true);
        mem.store(std::uint32_t(r[4]),8,file.path.sector);mem.store(std::uint32_t(r[5]),8,file.path.block);
        mem.store(std::uint32_t(r[6]),8,file.path.sector);mem.store(std::uint32_t(r[7]),4,file.flags);return 0;
    }
    // These reference implementations are TODO stubs or have an unknown wire
    // protocol. In particular truncate2 is NOT known to alias ftruncate.
    if(number==823 || number==824 || (number>=825 && number<=830) || number==845 || number==846 || number==847)
        throw std::runtime_error("unsupported sys_fs protocol syscall "+std::to_string(number));

    if(number==833){
        // r3 is a target string (possibly relative), r4 is its destination.
        // The existing resolver rejects symlinks, so creating a host link
        // would not implement guest link traversal.
        (void)native_fs_path(s,r[3]);
        const auto target=s.fs.resolve(native_fs_path(s,r[4]));
        if(target.readonly)return 0x80010026;
        throw std::runtime_error("sys_fs_symbolic_link requires guest symlink semantics");
    }
    const auto guest=native_fs_path(s,r[3]);const auto path=s.fs.resolve(guest);
    if(number==816){
        // Only existence checking is documented sufficiently by the available
        // sources. Do not invent R/W/X mode bits from host unistd.h.
        if(std::int32_t(r[4])!=0)throw std::runtime_error("unsupported sys_fs_access mode");
        struct stat info{};return ::stat(path.host.c_str(),&info)?NativeFilesystem::host_error(errno):0;
    }
    if(number==810 || number==812){
        const auto destination=native_fs_path(s,r[4]);const auto target=s.fs.resolve(destination);
        if(guest.substr(0,guest.find('/',1))!=destination.substr(0,destination.find('/',1)))return 0x80010030;
        if(path.readonly || target.readonly)return 0x80010026;
        s.fs.check_mutable_path(path);s.fs.check_mutable_path(target);
        if(number==810)return ::link(path.host.c_str(),target.host.c_str())?NativeFilesystem::host_error(errno):0;
        // LV2 rename does not replace an existing destination (RPCS3 passes
        // overwrite=false). Same spelling of an existing source is a no-op.
        struct stat info{};if(::stat(path.host.c_str(),&info))return NativeFilesystem::host_error(errno);
        if(path.host.lexically_normal()==target.host.lexically_normal())return 0;
        if(::lstat(target.host.c_str(),&info)==0)return 0x80010014;
        if(errno!=ENOENT)return NativeFilesystem::host_error(errno);
        return ::rename(path.host.c_str(),target.host.c_str())?NativeFilesystem::host_error(errno):0;
    }
    if(path.readonly)return 0x80010026;
    if(number==834 || number==835)
        throw std::runtime_error("unsupported sys_fs permission/guest ownership semantics syscall "+std::to_string(number));
    if(number==811)return ::mkdir(path.host.c_str(),std::uint32_t(r[4])&0777)?NativeFilesystem::host_error(errno):0;
    if(number==813){s.fs.check_mutable_path(path);return ::rmdir(path.host.c_str())?NativeFilesystem::host_error(errno):0;}
    if(number==814){
        struct stat info{};if(::stat(path.host.c_str(),&info))return NativeFilesystem::host_error(errno);
        if(S_ISDIR(info.st_mode))return 0x80010012;
        s.fs.check_mutable_path(path);
        return ::unlink(path.host.c_str())?NativeFilesystem::host_error(errno):0;
    }
    if(number==831){
        if(r[4]>INT64_MAX)return 0x80010002;
        return ::truncate(path.host.c_str(),static_cast<off_t>(r[4]))?NativeFilesystem::host_error(errno):0;
    }
    if(number==815){
        if(!r[4])return 0x8001000d;
        mem.access(std::uint32_t(r[4]),16,false);
        const struct timespec times[2]={{static_cast<time_t>(mem.load(std::uint32_t(r[4]),8)),0},
                                      {static_cast<time_t>(mem.load(std::uint32_t(r[4])+8,8)),0}};
        return ::utimensat(AT_FDCWD,path.host.c_str(),times,AT_SYMLINK_NOFOLLOW)?NativeFilesystem::host_error(errno):0;
    }
    throw std::runtime_error("unimplemented fs extension syscall "+std::to_string(number));
}
[[maybe_unused]] static bool syscall_fs_ext(PpuState& s) {
    switch(s.r[11]){
    case 800:case 805:case 806:case 807:case 810:case 811:case 812:case 813:case 814:case 815:case 816:
    case 821:case 823:case 824:case 825:case 826:case 827:case 828:case 829:case 830:
    case 831:case 832:case 833:case 834:case 835:case 845:case 846:case 847:
        try{s.r[3]=fs_ext_operation(s);}catch(const NativeFilesystem::Error& e){s.r[3]=e.code;}
        return true;
    default:return false;
    }
}
'''
