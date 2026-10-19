"""Firmware 3.01 GDAT VSH filesystem side; guest callbacks stay in libsysutil.

Protocol and SFO evidence: local/ratchet/codex/gamedata/{client.disasm.txt,vsh/}.
Unknown commands and unsupported categories trap before publishing a reply.
"""

CPP = r'''
namespace native_gamedata {
using Bytes=NativeSavedata::Bytes;
using Document=savedata_vsh::Document;
using Sfo=savedata_vsh::Sfo;
using savedata_vsh::be;
using savedata_vsh::put;
using savedata_vsh::text;
struct Error {std::uint32_t code;};
constexpr std::uint32_t ACCESS=0x8002b602,INTERNAL=0x8002b603,PARAM=0x8002b604,BROKEN=0x8002b606,NOSPACE=0x8002b605;
static Bytes reply(const std::string& command,std::uint32_t result=0,
                   const std::string& name="",const Bytes& blob={}) {
    auto b=savedata_vsh::reply(command,{{"elmResult",result}},name,blob);
    put(b,0,0x47444154);return b;
}
static std::filesystem::path directory(PpuState& s,const std::string& name) {
    // NativeFilesystem validates every path component, including dangling symlinks.
    return s.fs.game_directory(name);
}
static Sfo load_sfo(const std::filesystem::path& path) {
    std::ifstream input(path,std::ios::binary|std::ios::ate);
    if(!input)throw Error{INTERNAL};
    const auto size=input.tellg();if(size<20 || size>1048576)throw Error{BROKEN};
    Bytes bytes(static_cast<std::size_t>(size));input.seekg(0);
    if(!input.read(reinterpret_cast<char*>(bytes.data()),bytes.size()))throw Error{INTERNAL};
    try{return Sfo(bytes);}catch(const std::runtime_error&){throw Error{BROKEN};}
}
static std::string optional_string(const Sfo& f,const std::string& key,unsigned cap,const std::string& fallback="") {
    if(!f.values.count(key))return fallback;
    try{return f.string(key,cap);}catch(const std::runtime_error&){throw Error{BROKEN};}
}
static unsigned optional_integer(const Sfo& f,const std::string& key,unsigned fallback=0) {
    if(!f.values.count(key))return fallback;
    try{return f.integer(key);}catch(const std::runtime_error&){throw Error{BROKEN};}
}
static void atomic_sfo(const std::filesystem::path& dir,const Bytes& bytes) {
    // Unique exclusive temporary file, no symlink following, durable before rename.
    auto pattern=(dir/".PARAM.SFO.XXXXXX").string();std::vector<char> tmp(pattern.begin(),pattern.end());tmp.push_back(0);
    const int fd=::mkstemp(tmp.data());if(fd<0)throw Error{INTERNAL};
    bool open=true;
    try{
        std::size_t done=0;
        while(done<bytes.size()){
            const auto n=::write(fd,bytes.data()+done,bytes.size()-done);
            if(n<0 && errno==EINTR)continue;
            if(n<=0)throw Error{INTERNAL};
            done+=n;
        }
        if(::fchmod(fd,0666))throw Error{INTERNAL};
        if(::fsync(fd))throw Error{INTERNAL};
        const int closed=::close(fd);open=false;if(closed)throw Error{INTERNAL};
        if(::rename(tmp.data(),(dir/"PARAM.SFO").c_str()))throw Error{INTERNAL};
    }catch(...){if(open)::close(fd);::unlink(tmp.data());throw;}
}
static Bytes stat(PpuState& s,const std::string& name,unsigned category) {
    if(!s.fs.game_data.container)throw std::runtime_error("GDAT stat outside session");
    if(category>1)throw std::runtime_error("GDAT unsupported stat category");
    const auto path=directory(s,name);
    Bytes b(0x1450,0);
    put(b,0,std::min<std::uint64_t>(INT32_MAX,std::filesystem::space(s.fs.hdd_root).available/1024));
    const bool exists=std::filesystem::exists(path/"PARAM.SFO");put(b,4,!exists);
    const auto content="/dev_hdd0/game/"+name,usrdir=content+"/USRDIR";
    std::copy(content.begin(),content.end(),b.begin()+8);std::copy(usrdir.begin(),usrdir.end(),b.begin()+0x427);
    if(exists){
        struct ::stat info{};if(::stat(path.c_str(),&info))throw Error{INTERNAL};
        put(b,0x848,std::uint64_t(info.st_atime),8);
        put(b,0x850,std::uint64_t(info.st_mtime),8);
        put(b,0x858,std::uint64_t(info.st_ctime),8);
    }
    if(exists){
        const auto f=load_sfo(path/"PARAM.SFO");
        const auto cat=optional_string(f,"CATEGORY",4);
        if((category==0 && cat!="GD") || (category==1 && cat!="HG"))throw Error{BROKEN};
        auto copy_string=[&](unsigned at,unsigned cap,const std::string& key,const std::string& fallback=""){
            const auto value=optional_string(f,key,cap,fallback);std::copy(value.begin(),value.end(),b.begin()+0x860+at);
        };
        copy_string(0,128,"TITLE");
        for(unsigned i=0;i<20;++i){char key[16];std::snprintf(key,sizeof(key),"TITLE_%02u",i);copy_string(128+i*128,128,key);}
        copy_string(0xa80,10,"TITLE_ID");copy_string(0xa8c,6,"VERSION");
        put(b,0x860+0xa98,optional_integer(f,"PARENTAL_LEVEL"));
        put(b,0x860+0xa94,optional_integer(f,"ATTRIBUTE"));
        if(category==1){put(b,0x860+0xa9c,optional_integer(f,"RESOLUTION"));put(b,0x860+0xaa0,optional_integer(f,"SOUND_FORMAT"));}
    }
    if(exists && !s.fs.game_data.no_calc)throw std::runtime_error("GDAT stat size calculation not implemented");
    put(b,0x1404,exists?UINT32_MAX:0);put(b,0x1408,category?4:3);
    s.fs.game_data.directory=name;return b;
}
// basic_plugins 3.01 0x20007d4c (relocated at 0x20000000):
// strings use LE SFO values; the incoming scalar fields are BE32.
static void update(PpuState& s,const std::string& name,unsigned category,const Bytes& param) {
    if(!s.fs.game_data.container || s.fs.game_data.directory!=name)
        throw std::runtime_error("GDAT update without matching stat session");
    if(category>1)throw std::runtime_error("GDAT unsupported update category");
    const auto path=directory(s,name);
    const bool exists=std::filesystem::exists(path/"PARAM.SFO");
    Sfo f=exists?load_sfo(path/"PARAM.SFO"):Sfo{};
    auto field=[&](unsigned at,unsigned n){return text(param,at,n);};
    // Build and validate the complete metadata before any filesystem mutation.
    f.string("TITLE",field(0,128),128);
    for(unsigned i=0;i<16;++i){char key[16];std::snprintf(key,sizeof(key),"TITLE_%02u",i);f.string(key,field(128+i*128,128),128);}
    f.string("TITLE_ID",field(0xa80,10),16);f.string("VERSION",field(0xa8c,6),8);
    f.integer("PARENTAL_LEVEL",be(param,0xa98));
    if(category){f.integer("RESOLUTION",be(param,0xa9c));f.integer("SOUND_FORMAT",be(param,0xaa0));}
    f.string("CATEGORY",category?"HG":"GD",4);
    if(category)f.integer("BOOTABLE",1);
    const auto encoded=f.encode();
    if(!std::filesystem::exists(path) && std::filesystem::space(s.fs.hdd_root).available/1024<=64)throw Error{NOSPACE};
    // Mirror directory creation; game file copies remain guest filesystem I/O.
    std::filesystem::create_directory(s.fs.hdd_root/"game");
    if(std::filesystem::create_directory(path))std::filesystem::permissions(path,std::filesystem::perms::all);
    if(std::filesystem::create_directory(path/"USRDIR"))std::filesystem::permissions(path/"USRDIR",std::filesystem::perms::all);
    atomic_sfo(path,encoded);
}
static Bytes request(PpuState& s,const Bytes& bytes,unsigned channel) {
    if(channel!=1 || !s.sysutil_connections.count(UINT64_C(1)<<32))
        throw std::runtime_error("GDAT requires connected channel 1");
    const Document d(bytes,0x47444154);
    try{
        if(d.name=="initGameData"){
            d.shape({"elmMemContainer","elmNoCalcFlag"});
            const auto id=d.integer("elmMemContainer"),no_calc=d.integer("elmNoCalcFlag");
            if(no_calc>1)throw std::runtime_error("GDAT unsupported calculation flag");
            auto& data=s.fs.game_data;
            if(data.container)throw std::runtime_error("nested initGameData session");
            if(s.fs.hdd_root.empty())throw std::runtime_error("initGameData requires ANYPS3_HDD_ROOT");
            auto container=std::find_if(s.mmapper.containers.begin(),s.mmapper.containers.end(),[&](const auto& c){return c.id==id;});
            constexpr unsigned workspace=0x10000;
            if(container==s.mmapper.containers.end() || container->used>container->size || container->size-container->used<workspace)
                return reply("elmMemContainer",PARAM);
            data.workspace.resize(workspace);data.container=id;data.no_calc=no_calc;
            container->used+=workspace;return reply("elmMemContainer");
        }
        if(d.name=="endGameData"){
            d.shape({});auto& data=s.fs.game_data;
            if(!data.container)throw std::runtime_error("endGameData outside session");
            auto container=std::find_if(s.mmapper.containers.begin(),s.mmapper.containers.end(),[&](const auto& c){return c.id==data.container;});
            if(container==s.mmapper.containers.end() || container->used<data.workspace.size())throw std::runtime_error("lost game-data memory container");
            container->used-=data.workspace.size();data=NativeFilesystem::GameData{};
            return reply(d.name);
        }
        if(d.name=="getGameDataStat"){
            d.shape({"elmErrDialog","elmDirName","elmCategoryType"});
            if(d.integer("elmErrDialog"))throw std::runtime_error("GDAT error dialog requires native UI");
            return reply(d.name,0,"elmDataStat",stat(s,d.string("elmDirName"),d.integer("elmCategoryType")));
        }
        if(d.name=="updateGameData"){
            d.shape({"elmDirName","elmCategoryType","elmSysParam"});
            const auto name=d.string("elmDirName");const auto category=d.integer("elmCategoryType");
            const auto param=d.blob("elmSysParam",2980);
            update(s,name,category,param);return reply(d.name);
        }
        if(d.name=="zSetSysVersion"){
            d.shape({"elmSysVersion"});const auto version=d.string("elmSysVersion");
            if(version.size()!=7 || version[2]!='.' || version.substr(0,2).find_first_not_of("0123456789")!=std::string::npos ||
               version.substr(3).find_first_not_of("0123456789")!=std::string::npos)
                throw std::runtime_error("GDAT invalid system version");
            if(!s.fs.game_data.container || s.fs.game_data.directory.empty())
                throw std::runtime_error("GDAT system version without stat session");
            const auto path=directory(s,s.fs.game_data.directory);
            auto f=load_sfo(path/"PARAM.SFO");f.string("PS3_SYSTEM_VER",version,8);atomic_sfo(path,f.encode());
            // VSH 0x1292d8 uses getGameDataStat, despite zSetSysVersion request.
            return reply("getGameDataStat");
        }
        throw std::runtime_error("unsupported native GDAT command: "+d.name);
    }catch(const Error& error){return reply(d.name=="zSetSysVersion"?"getGameDataStat":d.name,error.code);}
    catch(const std::filesystem::filesystem_error& error){
        (void)error;return reply(d.name=="zSetSysVersion"?"getGameDataStat":d.name,INTERNAL);
    }
}
} // namespace native_gamedata
'''
