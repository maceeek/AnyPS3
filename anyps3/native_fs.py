"""Native disc mount and metadata, shared by sysutil and filesystem services."""

TYPES = r'''
#include <cstdlib>
#include <filesystem>
#include <sys/stat.h>
#include <cerrno>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <map>
#include <memory>
struct NativeFilesystem {
    struct Error {std::uint32_t code;};
    static std::uint32_t host_error(int code){
        switch(code){
        case ENOENT:return 0x80010006;case EPERM:return 0x80010009;case EACCES:return 0x80010029;
        case ENOTDIR:return 0x8001002e;case EISDIR:return 0x80010012;
        case EEXIST:return 0x80010014;case EBADF:return 0x8001002a;
        case ENOSPC:return 0x80010023;case EROFS:return 0x80010026;
        case EMFILE:return 0x8001002c;case ENFILE:return 0x80010022;case EINVAL:return 0x80010002;
        case EFBIG:return 0x80010020;case ENAMETOOLONG:return 0x80010034;
        case EXDEV:return 0x80010030;case ENOTEMPTY:return 0x80010036;
        case EBUSY:return 0x8001000a;case EOVERFLOW:return 0x80010039;
        default:return 0x8001002b;
        }
    }
    struct Path {
        std::filesystem::path host;
        std::uint32_t block=0,sector=0;
        bool readonly=false,no_uid=false;
    };
    struct File {
        int fd;Path path;std::uint32_t flags;
        std::string guest;
        File(int descriptor,Path resolved,std::uint32_t mode):fd(descriptor),path(std::move(resolved)),flags(mode){}
        ~File(){if(fd>=0)::close(fd);}
        File(const File&)=delete;File& operator=(const File&)=delete;
    };
    std::map<std::uint32_t,std::unique_ptr<File>> files;
    std::filesystem::path disc;
    bool disc_casefold=false;
    std::filesystem::path cache_root,cache;
    std::filesystem::path hdd_root;
    struct GameData {
        std::uint32_t container=0;
        bool no_calc=false;
        std::vector<std::uint8_t> workspace;
        std::string directory;
    } game_data;
    std::string cache_id;
    std::array<std::uint8_t,32> boot_info{};
    void check_mutable_path(const Path& path)const{
        auto normalized=path.host.lexically_normal();
        while(normalized.has_relative_path() && normalized.filename().empty())normalized=normalized.parent_path();
        // Virtual mount roots must never be removed or renamed on the host.
        for(const auto& root:{disc,cache,hdd_root})
            if(!root.empty() && normalized==root.lexically_normal())throw Error{0x80010009};
    }
    Path resolve(const std::string& guest)const{
        if(guest.empty() || guest[0]!='/')throw Error{0x80010006};
        const auto slash=guest.find('/',1);const auto mount=guest.substr(0,slash);
        Path result;
        if(mount=="/dev_bdvd")result={disc,2048,2048,true,true};
        else if(mount=="/dev_hdd1")result={cache,32768,512,false,true};
        else if(mount=="/dev_hdd0")result={hdd_root,4096,512,false,false};
        else throw Error{0x8001003a};
        if(result.host.empty())throw Error{0x8001003a};
        if(slash!=std::string::npos){
            for(const auto& component:std::filesystem::path(guest.substr(slash+1))){
                if(component==".." || component.has_root_path())throw Error{0x80010009};
                if(component=="." || component.empty())continue;
                auto child=result.host/component;
                // Optional host-layout adaptation for primary ISO9660 names.
                // HDD/cache paths retain their normal case-sensitive lookup.
                if(disc_casefold && mount=="/dev_bdvd"){
                    std::error_code lookup_error;
                    const auto status=std::filesystem::symlink_status(child,lookup_error);
                    if(lookup_error && lookup_error!=std::errc::no_such_file_or_directory)throw Error{host_error(lookup_error.value())};
                    if(status.type()==std::filesystem::file_type::not_found){
                        auto fold=[](std::string name){for(auto& ch:name)if(ch>='A' && ch<='Z')ch+=char('a'-'A');return name;};
                        const auto wanted=fold(component.string());bool found=false;
                        lookup_error.clear();
                        for(std::filesystem::directory_iterator it(result.host,lookup_error),end;!lookup_error && it!=end;it.increment(lookup_error)){
                            if(fold(it->path().filename().string())!=wanted)continue;
                            if(found)throw std::runtime_error("ambiguous case-folded disc path: "+guest);
                            child=it->path();found=true;
                        }
                        if(lookup_error)throw Error{host_error(lookup_error.value())};
                    }
                }
                result.host=child;
                std::error_code error;
                if(std::filesystem::is_symlink(std::filesystem::symlink_status(result.host,error)))throw Error{0x80010009};
                if(error && error!=std::errc::no_such_file_or_directory)throw Error{host_error(error.value())};
            }
        }
        if(guest.back()=='/' || (guest.size()>=2 && guest.substr(guest.size()-2)=="/."))result.host/=".";
        return result;
    }
    void configure_hdd(const char* root){
        if(root && *root){
            const auto path=std::filesystem::canonical(root);
            if(!std::filesystem::is_directory(path))throw std::runtime_error("ANYPS3_HDD_ROOT must be a directory");
            hdd_root=path;
        }
    }
    std::filesystem::path game_directory(const std::string& name)const{
        if(hdd_root.empty())throw std::runtime_error("game data requires ANYPS3_HDD_ROOT");
        if(name.empty() || name.size()>31 || name.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos)
            throw std::runtime_error("invalid game-data directory name");
        const auto parent=hdd_root/"game",path=parent/name;
        for(const auto& p:{parent,path,path/"PARAM.SFO",path/"USRDIR"})
            if(std::filesystem::is_symlink(std::filesystem::symlink_status(p)))throw std::runtime_error("game-data symlink is unsupported");
        return path;
    }
    std::vector<std::uint8_t> game_data_stat(const std::string& name){
        if(!game_data.container)throw std::runtime_error("game-data stat outside session");
        const auto path=game_directory(name);
        if(std::filesystem::exists(path/"PARAM.SFO"))throw std::runtime_error("existing game-data SFO metadata is not implemented");
        // Firmware 3.01's private wire blob is 0x1450, not the 0x1448 public
        // CellGameDataStatGet. The firmware converts it before calling the game.
        std::vector<std::uint8_t> result(0x1450,0);
        auto put=[&](unsigned a,std::uint64_t v,unsigned n=4){for(unsigned j=0;j<n;++j)result[a+j]=v>>((n-j-1)*8);};
        const auto free_kb=std::min<std::uint64_t>(INT32_MAX,std::filesystem::space(hdd_root).available/1024);
        put(0,free_kb);put(4,1);
        const std::string content="/dev_hdd0/game/"+name,usrdir=content+"/USRDIR";
        std::copy(content.begin(),content.end(),result.begin()+8);
        std::copy(usrdir.begin(),usrdir.end(),result.begin()+0x427);
        if(std::filesystem::exists(path)){
            struct stat info{};if(::stat(path.c_str(),&info))throw std::runtime_error("game-data stat failed");
            put(0x848,std::uint64_t(info.st_atime)*1000000,8);
            put(0x850,std::uint64_t(info.st_mtime)*1000000,8);
            put(0x858,std::uint64_t(info.st_ctime)*1000000,8);
        }
        if(!game_data.no_calc)throw std::runtime_error("game-data size calculation is not implemented");
        put(0x1404,UINT32_MAX); // Private wire sizeKB; no calculation requested.
        game_data.directory=name;
        std::fprintf(stderr,"native game-data stat name=%s new=1 free_kb=%llu\n",name.c_str(),static_cast<unsigned long long>(free_kb));
        return result;
    }
    void configure_cache(const char* root){
        if(root && *root)cache_root=std::filesystem::canonical(root);
    }
    std::uint32_t clear_cache(){
        if(cache.empty())return 0x8002bc04;
        std::error_code error;
        for(std::filesystem::directory_iterator it(cache,error),end;!error && it!=end;it.increment(error)){
            std::filesystem::remove_all(it->path(),error);if(error)break;
        }
        return error?0x8002bc01:0;
    }
    std::uint32_t mount_cache(const std::string& name){
        if(name.size()>31 || name.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos)return 0x8002bc03;
        if(cache_root.empty())throw std::runtime_error("cache mount requires explicit ANYPS3_CACHE_ROOT directory");
        if(!name.empty() && !cache.empty() && name==cache_id)return 1;
        // A fresh host directory belongs exclusively to this runtime session.
        // Cross-session persistence is not advertised; first mount is cleared.
        auto pattern=(cache_root/"anyps3-XXXXXX").string();
        if(!::mkdtemp(pattern.data()))return 0x8002bc01;
        const auto previous=cache;
        if(!previous.empty()){
            const auto result=clear_cache();
            if(result){std::error_code ignored;std::filesystem::remove(pattern,ignored);return result;}
            std::error_code ignored;std::filesystem::remove(previous,ignored);
        }
        cache=pattern;cache_id=name;
        std::fprintf(stderr,"native cache mounted id=%s guest=/dev_hdd1 host=%s cleared=1\n",name.c_str(),cache.c_str());
        return 0;
    }
    void mount(const char* root) {
        if(!root || !*root) return;
        const auto* fold=std::getenv("ANYPS3_DISC_CASEFOLD");
        if(fold && std::string(fold)!="0" && std::string(fold)!="1")throw std::runtime_error("ANYPS3_DISC_CASEFOLD must be 0 or 1");
        disc_casefold=fold && std::string(fold)=="1";
        const auto path=std::filesystem::canonical(root);
        std::ifstream file(path/"PS3_GAME/PARAM.SFO",std::ios::binary|std::ios::ate);
        if(!file || file.tellg()<20 || file.tellg()>1048576) throw std::runtime_error("invalid disc PARAM.SFO size");
        std::vector<std::uint8_t> data(static_cast<std::size_t>(file.tellg()));
        file.seekg(0);if(!file.read(reinterpret_cast<char*>(data.data()),data.size())) throw std::runtime_error("disc PARAM.SFO read failed");
        auto le=[&](std::size_t at,unsigned n)->std::uint32_t {
            if(at>data.size() || n>data.size()-at) throw std::runtime_error("truncated disc SFO");
            std::uint32_t value=0;for(unsigned j=0;j<n;++j)value|=std::uint32_t(data[at+j])<<(8*j);return value;
        };
        const auto keys=le(8,4),values=le(12,4),count=le(16,4);
        if(le(0,4)!=0x46535000 || le(4,4)!=0x101 || 20+std::uint64_t(count)*16>keys || keys>values || values>data.size())
            throw std::runtime_error("invalid disc SFO tables");
        std::array<std::uint8_t,32> info{};std::set<std::string> seen;bool title=false,parental=false,disc_game=false;
        for(unsigned i=0;i<count;++i) {
            const auto e=20+i*16,k=le(e,2),fmt=le(e+2,2),size=le(e+4,4),capacity=le(e+8,4),offset=le(e+12,4);
            if(k>=values-keys || offset>data.size()-values || capacity>data.size()-values-offset || size>capacity)
                throw std::runtime_error("disc SFO value out of bounds");
            unsigned end=keys+k;while(end<values && data[end])++end;
            if(end==values || end==keys+k)throw std::runtime_error("invalid disc SFO key");
            const std::string name(data.begin()+keys+k,data.begin()+end);
            if(!seen.insert(name).second)throw std::runtime_error("duplicate disc SFO key");
            const auto at=values+offset;
            if(name=="TITLE_ID") {
                if(fmt!=0x204 || size!=10 || data[at+9])throw std::runtime_error("invalid disc title ID");
                for(unsigned j=0;j<9;++j)if(!((data[at+j]>='A' && data[at+j]<='Z') || (data[at+j]>='0' && data[at+j]<='9')))
                    throw std::runtime_error("invalid disc title ID character");
                std::copy_n(data.begin()+at,10,info.begin());title=true;
            } else if(name=="PARENTAL_LEVEL") {
                if(fmt!=0x404 || size!=4 || capacity!=4)throw std::runtime_error("invalid parental level");
                const auto level=le(at,4);for(unsigned j=0;j<4;++j)info[12+j]=level>>(24-j*8);parental=true;
            } else if(name=="CATEGORY") {
                disc_game=fmt==0x204 && size==3 && data[at]=='D' && data[at+1]=='G' && data[at+2]==0;
            }
        }
        if(!title || !parental || !disc_game)throw std::runtime_error("mount requires disc game metadata");
        boot_info=info;disc=path;
        std::fprintf(stderr,"native disc mounted title=%s\n",reinterpret_cast<const char*>(boot_info.data()));
    }
};
'''

CPP=r'''
[[maybe_unused]] static std::string native_fs_path(PpuState& s,std::uint32_t address){
    if(!address)throw NativeFilesystem::Error{0x8001000d};
    std::string result;
    for(unsigned i=0;i<1024;++i){const auto ch=s.memory.load(std::uint64_t(address)+i,1);if(!ch)return result;result+=char(ch);}
    throw NativeFilesystem::Error{0x80010034};
}
[[maybe_unused]] static void native_fs_stat(PpuState& s,std::uint32_t output,const NativeFilesystem::Path& path,const struct stat& st){
    auto& mem=s.memory;
    if(!S_ISDIR(st.st_mode) && !S_ISREG(st.st_mode))throw std::runtime_error("unsupported native filesystem object type");
    const auto mode=(S_ISDIR(st.st_mode)?0040777:0100666)&(path.readonly?~0222:~0);
    mem.store(output,4,mode);mem.store(output+4,4,path.no_uid?UINT32_MAX:0);mem.store(output+8,4,path.no_uid?UINT32_MAX:0);
    mem.store(output+12,8,st.st_atime);mem.store(output+20,8,st.st_mtime);mem.store(output+28,8,st.st_ctime);
    mem.store(output+36,8,S_ISDIR(st.st_mode)?path.block:st.st_size);mem.store(output+44,8,path.block);
}
[[maybe_unused]] static std::uint32_t native_fs_syscall(PpuState& s){
    auto& r=s.r;auto& mem=s.memory;
    try{
    if(r[11]==822 || r[11]==840){
        const auto guest=native_fs_path(s,r[3]);
        if(guest.empty())return 0x80010002;
        if(r[11]==840 && guest.size()>31)return 0x80010034;
        if(r[11]==840 && guest=="/")return 0x80010037;
        const auto path=s.fs.resolve(r[11]==840?guest.substr(0,guest.find('/',1)):guest);
        if(!r[4] || !r[5] || (r[11]==822 && !r[6]))return 0x8001000d;
        mem.access(r[4],8,true);mem.access(r[5],8,true);
        if(r[11]==822){
            mem.access(r[6],8,true);
            struct stat st{};if(::stat(path.host.c_str(),&st))return NativeFilesystem::host_error(errno);
            mem.store(r[4],8,path.sector);mem.store(r[5],8,path.block);mem.store(r[6],8,path.sector);
        }else{
            std::error_code error;const auto space=std::filesystem::space(path.host,error);
            if(error)return NativeFilesystem::host_error(error.value());
            mem.store(r[4],8,path.readonly?0:space.capacity);mem.store(r[5],8,path.readonly?0:space.available);
        }
        std::fprintf(stderr,"native fs %s path=%s\n",r[11]==822?"block-size":"disk-free",guest.c_str());return 0;
    }
    if(r[11]==801){
        const auto guest=native_fs_path(s,r[3]);const auto path=s.fs.resolve(guest);
        const auto flags=std::uint32_t(r[4]);
        std::fprintf(stderr,"native fs open path=%s flags=%x extra_size=%llu\n",guest.c_str(),flags,static_cast<unsigned long long>(r[8]));
        if(r[8])throw std::runtime_error("extended/encrypted filesystem open is not implemented");
        if((flags&3)==3 || flags&~std::uint32_t(3|0100|0200|01000|02000))return 0x80010002;
        if(path.readonly && (flags&(3|0100|01000|02000)))return 0x80010026;
        if(!r[5])return 0x8001000d;
        mem.access(r[5],4,true);
        unsigned id=3;while(id<256 && (s.fs.files.count(id) || s.fs_ext.directories.count(id)))++id;
        if(id==256)return 0x8001002c;
        int host_flags=O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK;
        host_flags|=(flags&3)==0?O_RDONLY:(flags&3)==1?O_WRONLY:O_RDWR;
        if(flags&0100)host_flags|=O_CREAT;
        if(flags&0200)host_flags|=O_EXCL;
        if(flags&01000)host_flags|=O_TRUNC;
        if(flags&02000)host_flags|=O_APPEND;
        const auto fd=::open(path.host.c_str(),host_flags,0600);
        if(fd<0)return NativeFilesystem::host_error(errno);
        auto file=std::make_unique<NativeFilesystem::File>(fd,path,flags);
        file->guest=guest;
        struct stat st{};if(::fstat(fd,&st))return NativeFilesystem::host_error(errno);
        if(S_ISDIR(st.st_mode))return 0x80010012;
        if(!S_ISREG(st.st_mode))throw std::runtime_error("opening non-regular host files is unsupported");
        s.fs.files.emplace(id,std::move(file));mem.store(r[5],4,id);return 0;
    }
    if(r[11]==802 || r[11]==803 || r[11]==804 || r[11]==809 || r[11]==818 || r[11]==819 || r[11]==820){
        const auto it=s.fs.files.find(std::uint32_t(r[3]));if(it==s.fs.files.end())return 0x8001002a;
        auto& file=*it->second;
        if(r[11]==819 || r[11]==820){
            if(!(file.flags&3))return 0x8001002a;
            int result;do{result=r[11]==819?::fdatasync(file.fd) : ::fsync(file.fd);}while(result<0 && errno==EINTR);
            return result?NativeFilesystem::host_error(errno):0;
        }
        if(r[11]==804){const auto result=::close(file.fd);const auto error=errno;file.fd=-1;s.fs.files.erase(it);return result?NativeFilesystem::host_error(error):0;}
        if(r[11]==809){
            if(!r[4])return 0x8001000d;
            mem.access(r[4],52,true);struct stat st{};
            if(::fstat(file.fd,&st))return NativeFilesystem::host_error(errno);
            native_fs_stat(s,r[4],file.path,st);return 0;
        }
        if(r[11]==818){
            if(r[5]>2)return 0x80010002;
            if(!r[6])return 0x8001000d;
            mem.access(r[6],8,true);
            const auto position=::lseek(file.fd,std::int64_t(r[4]),r[5]==0?SEEK_SET:r[5]==1?SEEK_CUR:SEEK_END);
            if(position<0)return NativeFilesystem::host_error(errno);
            mem.store(r[6],8,position);return 0;
        }
        const bool writing=r[11]==803;
        if((writing && !(file.flags&3)) || (!writing && (file.flags&3)==1))return 0x8001002a;
        if(!r[6] || (!r[4] && r[5]))return 0x8001000d;
        mem.access(r[6],8,true);
        if(r[5]>16*1024*1024)throw std::runtime_error("filesystem transfer exceeds native diagnostic budget");
        // The guest buffer may span separately mapped but adjacent regions.
        std::vector<std::uint8_t> bytes(r[5]);
        if(writing && r[5])mem.transfer(r[4],bytes.data(),unsigned(r[5]),false);
        ssize_t count;do{count=writing ? ::write(file.fd,bytes.data(),r[5]) : ::read(file.fd,bytes.data(),r[5]);}while(count<0 && errno==EINTR);
        if(count<0)return NativeFilesystem::host_error(errno);
        if(!writing && count)mem.transfer(r[4],bytes.data(),unsigned(count),true);
        mem.store(r[6],8,count);
        std::fprintf(stderr,"native fs %s fd=%u bytes=%lld\n",writing?"write":"read",std::uint32_t(r[3]),static_cast<long long>(count));return 0;
    }
    if(r[11]==808){
        const auto guest=native_fs_path(s,r[3]);const auto path=s.fs.resolve(guest);
        if(!r[4])return 0x8001000d;
        mem.access(r[4],52,true);
        struct stat st{};const auto result=::stat(path.host.c_str(),&st);const auto error=errno;
        std::fprintf(stderr,"native fs stat path=%s result=%d\n",guest.c_str(),result);
        if(result)return NativeFilesystem::host_error(error);
        native_fs_stat(s,r[4],path,st);return 0;
    }
    if(r[11]==817 && std::uint32_t(r[4])==0xc0000006){
        const auto arg=std::uint32_t(r[5]);
        if(r[6]!=32)return 0x80010002;
        mem.access(arg,32,true);
        if(mem.load(arg,4)!=32 || mem.load(arg+4,4)!=16 || mem.load(arg+8,4)!=24)return 0x80010002;
        const auto size=mem.load(arg+12,4),name=mem.load(arg+16,4);
        if(size>1024)return 0x80010002;
        const auto* bytes=mem.access(name,size,false);
        std::fprintf(stderr,"native fs mount capability path=%.*s unsupported\n",int(size),reinterpret_cast<const char*>(bytes));
        // No special device capability is advertised. Firmware uses ordinary
        // filesystem syscalls after the embedded ENOTSUP response.
        mem.store(arg+24,4,0x80010037);mem.store(arg+28,4,0);return 0;
    }
    throw std::runtime_error("unimplemented filesystem syscall "+std::to_string(r[11])+" operation "+std::to_string(std::uint32_t(r[4])));
    }catch(const NativeFilesystem::Error& e){return e.code;}
}
[[maybe_unused]] static std::uint32_t native_cache_mount(PpuState& s){
    const auto param=s.r[3];if(!param)return 0x8002bc03;
    std::string name;
    for(unsigned i=0;i<32;++i){const auto ch=s.memory.load(param+i,1);if(!ch)break;name.push_back(static_cast<char>(ch));}
    if(name.size()==32)return 0x8002bc03;
    auto* output=s.memory.access(param+32,1055,true);
    if(s.memory.load(param+1088,4))throw std::runtime_error("unsupported cache mount reserved parameter");
    const auto result=s.fs.mount_cache(name);if(result>1)return result;
    std::fill_n(output,1055,0);std::copy_n("/dev_hdd1",10,output);return result;
}
'''
