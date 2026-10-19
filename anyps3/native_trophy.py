"""Firmware 3.01 NPTR queries and bounded, persistent TROPUSR table 4/6 state.

Wire source: libsysutil_np_trophy 3.01, relocated at 0x20220000 in
bundle 279. Storage source: RPCS3 ffba2bf09, Loader/TROPUSR.{h,cpp}.
Unknown storage variants and unsupported NPTR shapes stop explicitly.
"""
TYPES = r'''
#include <chrono>
#include <cstring>
struct NativeTrophy {
    std::array<std::string,4> contexts{};
    std::array<bool,4> read_only{},handles{},registered{},aborted{};
};
'''
CPP = r'''
namespace native_trophy {
using Bytes=std::vector<std::uint8_t>;
static void require(bool ok,const char* why){if(!ok)throw std::runtime_error(why);}
static std::uint64_t be(const Bytes& b,std::size_t a,unsigned n=4){
    require(a<=b.size() && n<=b.size()-a,"truncated trophy BE field");
    std::uint64_t v=0;for(unsigned i=0;i<n;++i)v=(v<<8)|b[a+i];return v;
}
static void put(Bytes& b,std::size_t a,std::uint64_t v,unsigned n=4){
    require(a<=b.size() && n<=b.size()-a,"trophy output bounds");
    for(unsigned i=0;i<n;++i)b[a+i]=v>>(8*(n-i-1));
}
static Bytes read(const std::filesystem::path& path){
    std::ifstream f(path,std::ios::binary|std::ios::ate);
    require(bool(f),"trophy file open failed");const auto n=f.tellg();
    require(n>=0 && n<=1024*1024,"unsupported trophy metadata size");
    Bytes b(static_cast<std::size_t>(n));f.seekg(0);
    require(bool(f.read(reinterpret_cast<char*>(b.data()),b.size())),"trophy file read failed");return b;
}
// SFM is UTF-8 XML. Parse the actual metadata, including XML entities; never
// synthesize names or silently accept malformed/unsupported XML constructs.
struct Xml {std::string name,text;std::map<std::string,std::string> attrs;std::vector<Xml> children;};
class XmlReader {
    std::string s;std::size_t p=0;
    bool at(const char* t)const{return s.compare(p,std::strlen(t),t)==0;}
    void ws(){while(p<s.size() && (s[p]==' ' || s[p]=='\r' || s[p]=='\n' || s[p]=='\t'))++p;}
    std::string name(){const auto a=p;while(p<s.size() && ((s[p]>='a' && s[p]<='z') ||
        (s[p]>='A' && s[p]<='Z') || (s[p]>='0' && s[p]<='9') || s[p]=='-' || s[p]=='_'))++p;
        require(p>a,"invalid trophy XML name");return s.substr(a,p-a);}
    static std::string decode(const std::string& in){
        std::string out;
        for(std::size_t i=0;i<in.size();++i){
            if(in[i]!='&'){require(in[i]!=0,"NUL in trophy XML");out+=in[i];continue;}
            const auto e=in.find(';',i);require(e!=std::string::npos,"unterminated trophy XML entity");
            const auto entity=in.substr(i+1,e-i-1);i=e;
            if(entity=="amp")out+='&';else if(entity=="lt")out+='<';else if(entity=="gt")out+='>';
            else if(entity=="quot")out+='"';else if(entity=="apos")out+='\'';
            else if(!entity.empty() && entity[0]=='#'){
                const bool hex=entity.size()>1 && entity[1]=='x';std::uint32_t cp=0;
                const auto start=hex?2u:1u;require(entity.size()>start,"empty trophy XML codepoint");
                for(unsigned j=start;j<entity.size();++j){const char c=entity[j];unsigned digit=99;
                    if(c>='0' && c<='9')digit=c-'0';else if(hex && c>='a' && c<='f')digit=c-'a'+10;
                    else if(hex && c>='A' && c<='F')digit=c-'A'+10;
                    require(digit<(hex?16u:10u) && cp<=0x10ffff/(hex?16u:10u),"invalid trophy XML codepoint");cp=cp*(hex?16:10)+digit;}
                require(cp && cp<=0x10ffff && !(cp>=0xd800 && cp<=0xdfff),"invalid trophy XML codepoint");
                if(cp<0x80)out+=char(cp);else if(cp<0x800){out+=char(0xc0|(cp>>6));out+=char(0x80|(cp&63));}
                else if(cp<0x10000){out+=char(0xe0|(cp>>12));out+=char(0x80|((cp>>6)&63));out+=char(0x80|(cp&63));}
                else{out+=char(0xf0|(cp>>18));out+=char(0x80|((cp>>12)&63));out+=char(0x80|((cp>>6)&63));out+=char(0x80|(cp&63));}
            }else throw std::runtime_error("unsupported trophy XML entity");
        }return out;
    }
    void misc(){for(;;){ws();if(at("<!--")){auto e=s.find("-->",p+4);require(e!=std::string::npos,"truncated trophy XML comment");p=e+3;}
        else if(at("<?xml ")){auto e=s.find("?>",p+6);require(e!=std::string::npos,"truncated XML declaration");p=e+2;}else return;}}
    Xml element(unsigned depth){
        require(depth<8 && at("<"),"unsupported trophy XML depth/element");++p;Xml x;x.name=name();
        for(;;){ws();if(at("/>")){p+=2;return x;}if(at(">")){++p;break;}
            auto key=name();ws();require(at("="),"invalid trophy XML attribute");++p;ws();
            require(p<s.size() && (s[p]=='\'' || s[p]=='"'),"unquoted trophy XML attribute");char q=s[p++];auto e=s.find(q,p);
            require(e!=std::string::npos,"truncated trophy XML attribute");
            require(x.attrs.emplace(key,decode(s.substr(p,e-p))).second,"duplicate trophy XML attribute");p=e+1;}
        for(;;){require(p<s.size(),"unclosed trophy XML element");
            if(at("</")){p+=2;require(name()==x.name,"mismatched trophy XML element");ws();require(at(">"),"invalid XML close");++p;return x;}
            if(at("<!--")){misc();continue;}
            if(at("<")){x.children.push_back(element(depth+1));continue;}
            auto e=s.find('<',p);require(e!=std::string::npos,"truncated trophy XML text");x.text+=decode(s.substr(p,e-p));p=e;}
    }
public:
    explicit XmlReader(const Bytes& b):s(b.begin(),b.end()){}
    Xml parse(){if(at("\xef\xbb\xbf"))p=3;misc();auto x=element(0);misc();require(p==s.size(),"trailing trophy XML data");return x;}
};
static std::string attr(const Xml& x,const char* key){auto it=x.attrs.find(key);require(it!=x.attrs.end(),"missing trophy XML attribute");return it->second;}
static std::string text(const Xml& x,const char* key){
    const Xml* found=nullptr;for(const auto& c:x.children)if(c.name==key){require(!found,"duplicate trophy XML text");found=&c;}
    require(found && found->children.empty(),"missing/complex trophy XML text");return found->text;
}
static unsigned number(const std::string& s){
    require(!s.empty(),"empty trophy number");unsigned n=0;for(char c:s){require(c>='0' && c<='9' && n<1000,"invalid trophy number");n=n*10+c-'0';}return n;
}
struct Trophy {unsigned id=0,grade=0;int platinum=-1;bool hidden=false;std::string name,description;};
struct Config {std::string title,description;std::vector<Trophy> trophies;};
static Config config(const std::filesystem::path& dir,const std::string& id,unsigned language){
    const auto root=XmlReader(read(dir/"TROPCONF.SFM")).parse();
    require(root.name=="trophyconf" && attr(root,"version")=="1.0" && text(root,"npcommid")==id,"unsupported trophy configuration");
    Config c;
    for(const auto& x:root.children)if(x.name=="trophy"){
        Trophy t;t.id=number(attr(x,"id"));const auto type=attr(x,"ttype"),hidden=attr(x,"hidden"),pid=attr(x,"pid");
        t.grade=type=="P"?1:type=="G"?2:type=="S"?3:type=="B"?4:0;
        require(t.id==c.trophies.size() && t.id<128 && t.grade && (hidden=="yes" || hidden=="no"),"unsupported trophy ID/grade/hidden");
        t.hidden=hidden=="yes";t.platinum=pid=="-1"?-1:static_cast<int>(number(pid));c.trophies.push_back(t);
    }
    require(!c.trophies.empty(),"empty trophy configuration");
    for(const auto& t:c.trophies)require(t.platinum==-1 || (unsigned(t.platinum)<c.trophies.size() &&
        c.trophies[t.platinum].grade==1 && unsigned(t.platinum)!=t.id),"invalid trophy platinum link");
    char file[32];std::snprintf(file,sizeof file,"TROP_%02u.SFM",language);
    auto localized=dir/file;if(!std::filesystem::exists(localized))localized=dir/"TROP.SFM";
    const auto labels=XmlReader(read(localized)).parse();
    require(labels.name=="trophyconf" && text(labels,"npcommid")==id,"trophy localization identity mismatch");
    c.title=text(labels,"title-name");c.description=text(labels,"title-detail");
    require(c.title.size()<128 && c.description.size()<1024,"trophy game text too long");
    std::set<unsigned> seen;
    for(const auto& x:labels.children)if(x.name=="trophy"){
        const auto n=number(attr(x,"id"));require(n<c.trophies.size() && seen.insert(n).second,"invalid localized trophy ID");
        auto& t=c.trophies[n];const auto type=attr(x,"ttype");
        require(type==(t.grade==1?"P":t.grade==2?"G":t.grade==3?"S":"B") && attr(x,"hidden")== (t.hidden?"yes":"no"),"localized trophy metadata mismatch");
        t.name=text(x,"name");t.description=text(x,"detail");require(t.name.size()<128 && t.description.size()<1024,"trophy text too long");
    }
    require(seen.size()==c.trophies.size(),"incomplete trophy localization");return c;
}
// Known TROPUSR layout from RPCS3 TROPUSR.h: 0x30-byte header,
// 0x20-byte descriptors, type 4 records 0x60, type 6 records 0x70.
// Preserve bytes when reading; never reset an existing, unrecognized file.
struct State {Bytes bytes;std::vector<std::size_t> records;};
static State state(const std::filesystem::path& dir,const Config& c){
    State s;const auto path=dir/"TROPUSR.DAT";const unsigned count=c.trophies.size();
    if(!std::filesystem::exists(path)){
        const unsigned at4=0x70,at6=at4+0x60*count;s.bytes.resize(at6+0x70*count);
        put(s.bytes,0,0x818f54ad);put(s.bytes,4,0x10000);put(s.bytes,8,2);
        for(unsigned k=0;k<2;++k){const unsigned at=0x30+0x20*k;put(s.bytes,at,k?6:4);put(s.bytes,at+4,k?0x60:0x50);
            put(s.bytes,at+8,1);put(s.bytes,at+12,count);put(s.bytes,at+16,k?at6:at4,8);}
        for(unsigned i=0;i<count;++i){const auto a=at4+0x60*i,b=at6+0x70*i;
            put(s.bytes,a,4);put(s.bytes,a+4,0x50);put(s.bytes,a+8,i);put(s.bytes,a+16,i);
            put(s.bytes,a+20,c.trophies[i].grade);put(s.bytes,a+24,std::uint32_t(c.trophies[i].platinum));
            put(s.bytes,b,6);put(s.bytes,b+4,0x60);put(s.bytes,b+8,i);put(s.bytes,b+16,i);}
    }else s.bytes=read(path);
    require(be(s.bytes,0)==0x818f54ad && be(s.bytes,4)==0x10000 && be(s.bytes,8)==2,"unsupported TROPUSR header/version/tables");
    std::size_t end=0x70;s.records.resize(count);std::set<unsigned> tables;
    for(unsigned k=0;k<2;++k){const unsigned h=0x30+0x20*k;const auto type=be(s.bytes,h),entry=be(s.bytes,h+4),n=be(s.bytes,h+12),at=be(s.bytes,h+16,8);
        require((type==4 || type==6) && tables.insert(type).second && entry==(type==4?0x50u:0x60u) && n==count &&
            be(s.bytes,h+8)==1 && at>=end && at<=s.bytes.size() && n<= (s.bytes.size()-at)/(entry+16),"unsupported TROPUSR table layout");
        end=at+n*(entry+16);std::set<unsigned> ids;
        for(unsigned i=0;i<n;++i){const auto a=at+i*(entry+16),id=be(s.bytes,a+16);
            require(be(s.bytes,a)==type && be(s.bytes,a+4)==entry && be(s.bytes,a+8)==i && id<count && ids.insert(id).second,"invalid TROPUSR record");
            if(type==4)require(be(s.bytes,a+20)==c.trophies[id].grade && be(s.bytes,a+24)==std::uint32_t(c.trophies[id].platinum),"TROPUSR configuration mismatch");
            else{require(be(s.bytes,a+20)<=1,"unsupported TROPUSR unlock state");s.records[id]=a;}
        }
    }
    require(end==s.bytes.size(),"unsupported trailing TROPUSR data");return s;
}
static void save(const std::filesystem::path& dir,const Bytes& bytes){
    std::string pattern=(dir/"TROPUSR.DAT.tmp-XXXXXX").string();std::vector<char> name(pattern.begin(),pattern.end());name.push_back(0);
    const int fd=::mkstemp(name.data());require(fd>=0,"trophy state staging open failed");
    bool closed=false;
    try{std::size_t n=0;while(n<bytes.size()){const auto written=::write(fd,bytes.data()+n,bytes.size()-n);
            if(written<0 && errno==EINTR)continue;
            require(written>0,"trophy state write failed");n+=written;}
        require(::fsync(fd)==0,"trophy state fsync failed");const int result=::close(fd);closed=true;require(result==0,"trophy state close failed");
        require(::rename(name.data(),(dir/"TROPUSR.DAT").c_str())==0,"trophy state rename failed");
    }catch(...){if(!closed)::close(fd);::unlink(name.data());throw;}
}
struct Field {std::string name;unsigned type=1;std::uint32_t value=0;Bytes data;};
struct Node {std::string name;std::vector<Field> fields;std::vector<Node> children;};
static Field integer(const char* name,std::uint32_t value){return{name,1,value,{}};}
static Field binary(const char* name,Bytes value){return{name,6,0,std::move(value)};}
static Field string(const char* name,const std::string& value){return{name,3,0,Bytes(value.begin(),value.end())};}
static Bytes encode(const Node& root){
    struct Item{const Node* node;unsigned offset,parent,prev,next,first=UINT32_MAX,last=UINT32_MAX;};
    std::vector<Item> items;unsigned total=0;
    auto add=[&](auto&& self,const Node& n,unsigned parent)->unsigned{
        const unsigned i=items.size(),off=total;total+=28+16*n.fields.size();items.push_back({&n,off,parent,UINT32_MAX,UINT32_MAX});
        unsigned prev=UINT32_MAX;
        for(const auto& child:n.children){const auto c=self(self,child,off);if(prev==UINT32_MAX)items[i].first=items[c].offset;
            else{items[prev].next=items[c].offset;items[c].prev=items[prev].offset;}prev=c;items[i].last=items[c].offset;}
        return i;
    };add(add,root,UINT32_MAX);
    Bytes tree(total),names,blob;
    auto text_offset=[&](const std::string& value){const unsigned at=names.size();names.insert(names.end(),value.begin(),value.end());names.push_back(0);return at;};
    for(const auto& it:items){const auto& n=*it.node;auto at=it.offset;put(tree,at,text_offset(n.name));put(tree,at+4,n.fields.size());
        put(tree,at+8,it.parent);put(tree,at+12,it.prev);put(tree,at+16,it.next);put(tree,at+20,it.first);put(tree,at+24,it.last);
        unsigned e=it.offset+28;
        for(const auto& f:n.fields){put(tree,e,text_offset(f.name));put(tree,e+4,f.type);
            if(f.type==1)put(tree,e+8,f.value);
            else if(f.type==3){put(tree,e+8,text_offset(std::string(f.data.begin(),f.data.end())));put(tree,e+12,f.data.size());}
            else{put(tree,e+8,blob.size());put(tree,e+12,f.data.size());blob.insert(blob.end(),f.data.begin(),f.data.end());blob.resize((blob.size()+15)&~std::size_t(15));}
            e+=16;}
    }
    const unsigned str_at=(64+tree.size()+15)&~15u,binary_at=(str_at+names.size()+15)&~15u;Bytes out(binary_at+blob.size());
    put(out,0,0x4e505452);put(out,4,0x110);put(out,8,64);put(out,12,tree.size());put(out,16,str_at);put(out,24,str_at);put(out,28,names.size());
    put(out,32,binary_at);put(out,40,binary_at);put(out,48,binary_at);put(out,52,blob.size());
    std::copy(tree.begin(),tree.end(),out.begin()+64);std::copy(names.begin(),names.end(),out.begin()+str_at);std::copy(blob.begin(),blob.end(),out.begin()+binary_at);return out;
}
class Request {
    const Bytes& b;unsigned tree,strings,str_size,blob,blob_size;std::set<unsigned> visited;std::vector<std::pair<unsigned,unsigned>> ranges;std::size_t covered=0;
    std::string str(unsigned at)const{require(at<str_size,"NPTR string offset out of bounds");std::string out;
        for(;at<str_size;++at){const char c=b[strings+at];if(!c)return out;out+=c;}throw std::runtime_error("unterminated NPTR string");}
    Node node(unsigned rel,unsigned parent,unsigned prev,unsigned depth){
        require(depth<4 && !(rel&3) && rel<=tree && tree-rel>=28 && visited.insert(rel).second,"invalid NPTR child link/cycle");
        const unsigned at=64+rel,n=be(b,at+4);require(n<=(tree-rel-28)/16,"NPTR leaf count out of bounds");
        require(be(b,at+8)==parent && be(b,at+12)==prev,"invalid NPTR parent/sibling link");
        const unsigned length=28+16*n;
        for(const auto& range:ranges)require(rel+length<=range.first || rel>=range.second,"overlapping NPTR nodes");
        ranges.push_back({rel,rel+length});covered+=length;Node out{str(be(b,at)),{}, {}};std::set<std::string> keys;
        for(unsigned i=0;i<n;++i){const unsigned e=at+28+16*i,ty=be(b,e+4);const auto key=be(b,e);
            if(key==UINT32_MAX){require(!ty && !be(b,e+8) && !be(b,e+12),"nonempty unused NPTR slot");continue;}
            const auto name=str(key);require(keys.insert(name).second,"duplicate NPTR leaf");Field f{name,ty,static_cast<std::uint32_t>(be(b,e+8)),{}};
            if(ty==6){const auto offset=f.value;const auto len=be(b,e+12);require(offset<=blob_size && len<=blob_size-offset,"NPTR binary bounds");f.data=Bytes(b.begin()+blob+offset,b.begin()+blob+offset+len);}
            else require(ty==1 && !be(b,e+12),"unsupported NPTR leaf type/payload");
            out.fields.push_back(f);}
        unsigned first=be(b,at+20),last=UINT32_MAX,previous=UINT32_MAX;
        for(unsigned next=first;next!=UINT32_MAX;){out.children.push_back(node(next,rel,previous,depth+1));last=next;previous=next;next=be(b,64+next+16);}
        require(last==be(b,at+24),"invalid NPTR last child link");return out;
    }
public:
    explicit Request(const Bytes& in):b(in),tree(be(in,12)),strings(be(in,24)),str_size(be(in,28)),blob(be(in,48)),blob_size(be(in,52)){
        require(b.size()>=92 && be(b,8)==64 && tree<=b.size()-64 && strings>=64+tree && strings<=b.size() && str_size<=b.size()-strings &&
            blob>=strings+str_size && blob<=b.size() && blob_size<=b.size()-blob,"invalid NPTR tables");}
    Node parse(){auto r=node(0,UINT32_MAX,UINT32_MAX,0);require(be(b,80)==UINT32_MAX && covered==tree,"invalid NPTR root/unreferenced tree");return r;}
};
static const Field& field(const Node& n,const char* name,unsigned type){
    auto it=std::find_if(n.fields.begin(),n.fields.end(),[&](const auto& f){return f.name==name;});
    require(it!=n.fields.end() && it->type==type,"missing/wrong NPTR leaf");return *it;
}
static Node counts(const char* name,const Config& c,const State& s,bool unlocked){
    std::array<unsigned,5> n{};for(const auto& t:c.trophies)if(!unlocked || be(s.bytes,s.records[t.id]+20)){++n[0];++n[t.grade];}
    return{name,{integer("count",n[0]),integer("platinum",n[1]),integer("gold",n[2]),integer("silver",n[3]),integer("bronze",n[4])},{}};
}
// Extra queries are kept separate from the already implemented registration
// transport. Firmware AbortHandle uses channel 0; all other commands use 1.
static bool more(PpuState& s,const Bytes& bytes,unsigned channel,Bytes& response){
    if(be(bytes,0)!=0x4e505452)return false;
    const auto str_at=be(bytes,24);require(str_at<bytes.size(),"invalid NPTR command string table");
    const auto command_at=str_at+be(bytes,64);require(command_at<bytes.size(),"invalid NPTR command offset");
    const auto end=std::find(bytes.begin()+command_at,bytes.end(),0);require(end!=bytes.end(),"unterminated NPTR command");
    const std::string command(bytes.begin()+command_at,end);
    if(command!="2" && command!="3" && command!="m" && command!="q" && command!="7" && command!="abort")return false;
    const auto req=Request(bytes).parse();
    require(channel==(command=="abort"?0u:1u),"NPTR command on wrong channel");
    bool connected=false;for(auto c:s.sysutil_connections)if((c>>32)==channel)connected=true;
    require(connected,"NPTR request without connected client");
    const auto handle=field(req,"mem_context",1).value;Node reply{command,{integer("result",0)},{}};
    auto error=[&](std::uint32_t code){reply.fields[0].value=code;response=encode(reply);return true;};
    if(command=="abort"){
        require(req.fields.size()==1 && req.children.empty(),"unsupported NPTR abort shape");
        if(!handle || handle>4 || !s.trophy.handles[handle-1])return error(0x80022911);
        response=encode(reply);s.trophy.aborted[handle-1]=true;return true;
    }
    const auto context=field(req,"context",1).value;
    require(req.fields.size()==(command=="3"?4u:command=="7"?3u:2u),"unsupported NPTR query parameters");
    std::set<std::string> children;for(const auto& c:req.children){require(c.fields.empty() && c.children.empty() && children.insert(c.name).second,"unsupported NPTR query child");}
    if(command=="2")require(!children.empty() && std::all_of(children.begin(),children.end(),[](const auto& n){return n=="gameData" || n=="gameDetails";}),"unsupported NPTR game info shape");
    else if(command=="3")require(!children.empty() && std::all_of(children.begin(),children.end(),[](const auto& n){return n=="flagData" || n=="flagDetails";}),"unsupported NPTR trophy info shape");
    else require(children.empty(),"unexpected NPTR query children");
    Bytes flags;if(command=="3"){flags=field(req,"flag_array",6).data;require(flags.size()==16 && field(req,"count",1).value>0 && field(req,"count",1).value<=128,"unsupported NPTR trophy selection");}
    unsigned trophy_id=command=="7"?field(req,"flag_id",1).value:0;
    if(!context || context>4 || s.trophy.contexts[context-1].empty())return error(0x8002290a);
    if(!handle || handle>4 || !s.trophy.handles[handle-1])return error(0x80022911);
    if(s.trophy.aborted[handle-1])return error(0x80022910);
    require(!s.fs.hdd_root.empty(),"trophy query requires HDD root");
    const auto id=s.trophy.contexts[context-1];const auto dir=s.fs.hdd_root/"home/00000001/trophy"/id;
    if(!s.trophy.registered[context-1])return error(0x80022904);
    const auto c=config(dir,id,s.settings.language);auto st=state(dir,c);
    if(command=="2"){
        if(children.count("gameData"))reply.children.push_back(counts("gameData",c,st,true));
        if(children.count("gameDetails")){auto details=counts("gameDetails",c,st,false);details.fields.push_back(string("game_title",c.title));
            details.fields.push_back(string("game_description",c.description));reply.children.push_back(details);}
    }else if(command=="m"){
        Bytes bits(16);for(const auto& t:c.trophies)if(be(st.bytes,st.records[t.id]+20)){
            const auto at=(t.id/32)*4;put(bits,at,be(bits,at)|(1u<<(t.id%32)));}
        reply.fields.push_back(binary("flag_array",bits));reply.fields.push_back(integer("count",c.trophies.size()));
    }else if(command=="q"){
        // Original VSH 3.01 0x188324..0x1883a4 (called at 0x188730):
        // gold=90, silver=30, bronze=15; platinum contributes no points.
        // Truncate division, but a nonzero score is at least one percent.
        unsigned total=0,earned=0;
        for(const auto& t:c.trophies){const unsigned points=t.grade==2?90:t.grade==3?30:t.grade==4?15:0;
            total+=points;if(be(st.bytes,st.records[t.id]+20))earned+=points;}
        const unsigned percent=!total || !earned?0:std::max(1u,earned*100/total);
        reply.fields.push_back(integer("percent",percent));
    }else if(command=="3"){
        std::vector<unsigned> ids;for(unsigned i=0;i<128;++i)if(be(flags,(i/32)*4)&(1u<<(i%32)))ids.push_back(i);
        require(ids.size()==field(req,"count",1).value,"NPTR trophy selection count mismatch");
        reply.fields.push_back(integer("count",ids.size())); // VSH 3.01 0x1881c4..cc.
        for(auto i:ids){if(i>=c.trophies.size())return error(0x80022920);
            if(c.trophies[i].hidden && !be(st.bytes,st.records[i]+20))return error(0x80022913);}
        for(auto i:ids){const auto& t=c.trophies[i];const auto a=st.records[i];
            if(children.count("flagData")){Bytes tick(8);put(tick,0,be(st.bytes,a+40,8),8);
                reply.children.push_back({"flagData",{integer("flag_id",i),binary("timestamp",tick),integer("unlocked",be(st.bytes,a+20))},{}});}
            if(children.count("flagDetails"))reply.children.push_back({"flagDetails",{integer("flag_id",i),integer("flag_type",t.grade),
                string("name",t.name),string("description",t.description),integer("hidden",t.hidden)},{}});
        }
    }else{
        if(trophy_id>=c.trophies.size())return error(0x80022920);
        if(s.trophy.read_only[context-1])return error(0x8002290e);
        const auto& t=c.trophies[trophy_id];if(t.grade==1)return error(0x80022914);
        if(be(st.bytes,st.records[trophy_id]+20))return error(0x80022915);
        // CellRtcTick = microseconds since 0001-01-01, UTC.
        const auto now=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
        require(now>=0,"unsupported pre-Unix trophy clock");const std::uint64_t tick=UINT64_C(62135596800000000)+now;
        auto unlock=[&](unsigned i){const auto a=st.records[i];put(st.bytes,a+20,1);put(st.bytes,a+32,tick,8);put(st.bytes,a+40,tick,8);};
        unlock(trophy_id);std::uint32_t platinum=UINT32_MAX;
        if(t.platinum>=0 && !be(st.bytes,st.records[t.platinum]+20)){
            bool all=true;for(const auto& other:c.trophies)if(other.platinum==t.platinum && !be(st.bytes,st.records[other.id]+20))all=false;
            if(all){platinum=t.platinum;unlock(platinum);}}
        reply.children.push_back({"platinumId",{integer("flag_id",platinum)},{}});
        response=encode(reply);save(dir,st.bytes);return true;
    }
    response=encode(reply);return true;
}
} // namespace native_trophy
'''
