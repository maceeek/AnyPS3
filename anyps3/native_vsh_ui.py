"""Input-driven host VSH MSGD front end for original libsysutil 3.01.

The transport acknowledgement is never a button. Completion is queued for
the guest's dispatcher 258; callers must pump vsh_ui_poll on event reads.
Unknown OSKD modes stop rather than inventing text or completion events.
"""

from .vsh_overlay import TYPES as OVERLAY_TYPES

TYPES = OVERLAY_TYPES + r'''
#include <chrono>
#include <cmath>
#include <cstring>
#include <type_traits>
#include <ctime>
struct NativeVshUi {
    enum Kind { Message, SaveList, SaveError, SaveDelete, SaveIndicator } kind=Message;
    bool open=false, closing=false,pad_release_guard=false;
    unsigned type=0,callback=0,userdata=0,port=0,cursor=0;
    std::string message;
    std::array<unsigned,2> progress{};
    std::array<std::string,2> progress_message{};
    std::array<std::uint32_t,7> held{};
    std::array<int,7> instances{{-1,-1,-1,-1,-1,-1,-1}};
    // UI may precede cellPadInit. Keep a separate NativePad observer then,
    // without changing the guest library lifecycle or its port history.
    std::shared_ptr<void> input_observer;
    std::chrono::steady_clock::time_point deadline{};
    struct Choice {
        std::string directory,title,detail;std::vector<std::uint8_t> picture;
        std::int64_t mtime=0;bool fresh=false;
        Choice(std::string dir,std::string name,std::int64_t time,bool is_new):directory(std::move(dir)),title(std::move(name)),mtime(time),fresh(is_new){}
    };
    std::vector<Choice> choices;
    std::vector<std::uint8_t> save_request,save_raw;
    unsigned save_raw_expected=0;
    bool save_confirm=false;
    unsigned raw_phase=0;
    std::vector<std::uint8_t> icon;
    std::array<std::chrono::steady_clock::time_point,7> repeat_deadlines{};
};
'''

CPP = r'''
// Integration hook: call in pad_service before reporting GetData and GetInfo2.
// See INTEGRATION.md. Template keeps minimal non-pad service harnesses valid.
template<class State> [[maybe_unused]] static bool vsh_ui_pad_intercept(State& s,unsigned operation){
    auto& ui=s.settings.vsh_ui;
    if(operation!=4 || !s.r[4])return false;
    if(ui.open && ui.kind!=NativeVshUi::SaveIndicator){s.memory.store(s.r[4],4,0);return true;}
    if(!ui.pad_release_guard)return false;
    // After closing, keep a held confirmation from leaking into the game.
    // RPCS3 cellPad.cpp pad_get_data distinguishes interception (NO_CHANGE)
    // from post-dialog ignore_input (neutral data, until physical release).
    bool held=false;s.pad.poll();
    for(unsigned i=0;i<std::min(s.pad.maximum,7u);++i)if(s.pad.ports[i].device){
        const auto data=s.pad.sample(i);held|=data[2] || data[3] || data[6]<48 || data[6]>207 || data[7]<48 || data[7]>207;
    }
    ui.pad_release_guard=held;
    const auto at=s.r[4];const auto index=unsigned(s.r[3]);const auto length=index<7 && (s.pad.ports[index].setting&2)?20u:8u;
    s.memory.access(at,44,true);s.memory.store(at,4,length);
    for(unsigned i=0;i<20;++i)s.memory.store(at+4+2*i,2,i==1?(0x70|(length/2)):i>=4 && i<8?128:0);
    return true;
}
[[maybe_unused]] static unsigned vsh_ui_pad_system_info(const NativeVshUi& ui){
    return ui.open && ui.kind!=NativeVshUi::SaveIndicator?1u:0u;
}
namespace vsh_ui {
using Bytes=NativeSavedata::Bytes;
using Document=savedata_vsh::Document;
static void save_input(PpuState&,std::uint32_t);
template<class State,class=void> struct HasPad:std::false_type {};
template<class State> struct HasPad<State,std::void_t<decltype(std::declval<State&>().pad)>>:std::true_type {};
static void overlay(const NativeVshUi& u) {
    anyps3_vsh_overlay::Snapshot snap;snap.visible=u.open;
    if((u.kind==NativeVshUi::SaveList || u.kind==NativeVshUi::SaveDelete) && !u.choices.empty()){
        const auto& c=u.choices.at(u.cursor);snap.icon=anyps3_vsh_overlay::decode_icon(c.picture);
    }else snap.icon=anyps3_vsh_overlay::decode_icon(u.icon);
    snap.lines.push_back(u.message.empty()?"SAVED DATA":u.message);
    if((u.kind==NativeVshUi::SaveList || u.kind==NativeVshUi::SaveDelete) && !u.save_confirm){
        const unsigned first=u.cursor>5?u.cursor-5:0;
        for(unsigned i=first;i<std::min<unsigned>(u.choices.size(),first+12);++i){
            const auto& c=u.choices[i];snap.lines.push_back((i==u.cursor?"> ":"  ")+c.title+" - "+c.directory);
            if(i==u.cursor)snap.selected=snap.lines.size()-1;
        }
        if(u.choices.empty())snap.lines.push_back("NO SAVED DATA");
        else{
            const auto& c=u.choices.at(u.cursor);
            snap.lines.push_back(c.detail);
            std::time_t time=c.mtime;std::tm tm{};char date[64]{};
            if(c.mtime && localtime_r(&time,&tm))std::strftime(date,sizeof(date),"%Y-%m-%d %H:%M:%S",&tm);
            snap.lines.push_back(date);
        }
    }else if(u.save_confirm || ((u.type>>4)&7)==1){
        auto cursor=u.save_confirm?((u.type>>8)&1):u.cursor;
        snap.lines.push_back(cursor?"  YES     > NO":"> YES       NO");snap.selected=1;
    }
    if(u.kind==NativeVshUi::SaveIndicator || ((u.type>>12)&3))
        snap.lines.push_back("PROGRESS: "+std::to_string(u.progress[0])+"%");
    if(u.kind!=NativeVshUi::SaveIndicator)snap.lines.push_back("D-PAD / STICK: SELECT   ENTER / BACK");
    anyps3_vsh_overlay::publish(std::move(snap));
}
static void display(const NativeVshUi& u) {
    overlay(u);
    const auto buttons=(u.type>>4)&7;
    const auto cursor=u.save_confirm?((u.type>>8)&1):u.cursor;
    std::fprintf(stderr,"native VSH dialog: %s\n",u.message.c_str());
    if(buttons==1)
        std::fprintf(stderr,"  %sYES  %sNO [cross=confirm, circle=NO, arrows=select]%s\n",
                     cursor==0?"> ":"  ",cursor==1?"> ":"  ",(u.type&0x80)?" (cancel disabled)":"");
    else if(buttons==2)
        std::fprintf(stderr,"  > OK [cross=OK%s]\n",(u.type&0x80)?"":", circle=back");
    else std::fprintf(stderr,"  wait/progress [closed only by guest Close/Abort]\n");
}
// Sample without touching cellPadGetData's previous/reported state. A newly
// connected device, or a held button at Open, must first release before it
// can answer a dialog. Each physical port has its own edge history.
template<class State> static std::uint32_t input(State& s,bool baseline=false) {
    if constexpr(!HasPad<State>::value){
        (void)s;(void)baseline;throw std::runtime_error("VSH UI requires NativePad backend");
    }else{
    auto& u=s.settings.vsh_ui;
    using Pad=std::remove_reference_t<decltype(s.pad)>;
    Pad* backend=&s.pad;
    if(!backend->maximum){
        if(!u.input_observer){
            auto observer=std::make_shared<Pad>();observer->load();
            if(observer->init(0x2000))throw std::runtime_error("VSH UI SDL input initialization failed");
            observer->maximum=7;u.input_observer=observer;
        }
        backend=std::static_pointer_cast<Pad>(u.input_observer).get();
    }
    auto& p=*backend;
    p.poll();std::uint32_t edges=0;
    for(unsigned i=0;i<7;++i){
        const auto& port=p.ports[i];
        if(!port.device){u.held[i]=0;u.instances[i]=-1;continue;}
        const auto data=p.sample(i);
        auto now=std::uint32_t(data[2])|(std::uint32_t(data[3])<<16);
        if(data[6]<48)now|=0x80;else if(data[6]>207)now|=0x20;
        if(data[7]<48)now|=0x10;else if(data[7]>207)now|=0x40;
        if(!baseline && u.instances[i]==port.instance){
            edges|=now&~u.held[i];
            const auto time=std::chrono::steady_clock::now();const auto directions=now&0xf0;
            if(directions && directions==(u.held[i]&0xf0)){
                if(time>=u.repeat_deadlines[i]){edges|=directions;u.repeat_deadlines[i]=time+std::chrono::milliseconds(100);}
            }else u.repeat_deadlines[i]=time+std::chrono::milliseconds(500);
        }else u.repeat_deadlines[i]=std::chrono::steady_clock::now()+std::chrono::milliseconds(500);
        u.held[i]=now;u.instances[i]=port.instance;
    }
    return edges;
    }
}
static std::uint64_t key(PpuState& s,unsigned channel) {
    unsigned port=4;
    for(auto connection:s.sysutil_connections)if((connection>>32)==channel){
        if(port!=4)throw std::runtime_error("VSH UI ambiguous sysutil client");
        port=static_cast<unsigned>(connection);
    }
    if(port>3)throw std::runtime_error("VSH UI requires connected sysutil client");
    const auto k=UINT64_C(0x8006010000000100)|port;
    const auto q=std::find_if(s.events.queues.begin(),s.events.queues.end(),[&](const auto& v){return v.key==k;});
    if(q==s.events.queues.end() || q->type!=1)throw std::runtime_error("VSH UI event queue absent/wrong type");
    return k;
}
static void complete(PpuState& s,std::int32_t button) {
    auto& u=s.settings.vsh_ui;
    const auto k=UINT64_C(0x8006010000000100)|u.port;
    // libsysutil 3.01 0x201244b0..2452c, CheckCallback 0x20127e18.
    // Even a null callback requires the dispatcher to clear the guest state.
    s.events.post(k,{0,258,(std::uint64_t(u.userdata)<<32)|u.callback,
                            static_cast<std::uint64_t>(static_cast<std::int64_t>(button))});
    std::fprintf(stderr,"native VSH dialog: completed button=%d\n",button);
    u.open=false;u.closing=false;u.pad_release_guard=true;overlay(u);
}
static void poll(PpuState& s) {
    auto& u=s.settings.vsh_ui;
    if(!u.open)return;
    if(u.kind==NativeVshUi::SaveIndicator)return;
    if(u.kind!=NativeVshUi::Message){save_input(s,input(s));return;}
    if(u.closing){
        if(std::chrono::steady_clock::now()>=u.deadline)complete(s,-1);
        return;
    }
    const auto edges=input(s);const auto buttons=(u.type>>4)&7;
    // The task deliberately disallows user completion of wait/progress UI.
    if(!buttons)return;
    if(buttons==1){
        if(edges&0x90){u.cursor=0;display(u);} // up/left
        else if(edges&0x60){u.cursor=1;display(u);} // right/down
    }
    // Opposing confirm/back is ambiguous; require another distinct press.
    const bool cross=edges&(s.settings.enter_button?0x400000:0x200000),
               circle=edges&(s.settings.enter_button?0x200000:0x400000);
    if(cross==circle)return;
    if(cross)complete(s,buttons==1 && u.cursor?2:1);
    else if(!(u.type&0x80))complete(s,buttons==1?2:3);
}
static Bytes result(unsigned code) {
    // VSH 0x12cc30: the response ROOT is "result", with a leaf "result".
    auto b=savedata_vsh::reply("result",{{"result",code}});
    savedata_vsh::put(b,0,0x4d534744);return b;
}
static Bytes request(PpuState& s,const Bytes& bytes,unsigned channel) {
    const auto tag=savedata_vsh::be(bytes,0);
    if(tag==0x4f534b44)
        throw std::runtime_error("OSKD requires verified on-screen text entry; pad-only text composition is not implemented");
    if(channel!=0 || tag!=0x4d534744)throw std::runtime_error("MSGD on wrong channel/tag");
    const auto k=key(s,channel);Document q(bytes,tag,true);auto& u=s.settings.vsh_ui;
    if(q.name=="dialogOpen2"){
        q.shape({"type","msgString","callbackFunc","userData"});
        const auto type=q.integer("type"),buttons=(type>>4)&7,progress=(type>>12)&3,cursor=(type>>8)&3;
        const auto text=q.string("msgString");const auto callback=q.integer("callbackFunc"),userdata=q.integer("userData");
        // Original Open2 validation: 0x20123f7c..fdc, 243a4..c0,24454..68.
        if((type&~0x33f7u) || buttons>2 || cursor>(buttons==1?1u:0u) || progress>2 ||
           (buttons && progress) || text.size()>511)return result(0x8002b301);
        if(u.open)throw std::runtime_error("MSGD nested dialogOpen2: busy result not verified for VSH 3.01");
        u.kind=NativeVshUi::Message;u.icon.clear();u.type=type;u.message=text;u.callback=callback;u.userdata=userdata;u.port=k&3;
        u.cursor=cursor;u.progress.fill(0);u.progress_message.fill("");u.closing=false;
        input(s,true);u.open=true;display(u);return result(0);
    }
    if(u.open && u.kind!=NativeVshUi::Message)
        throw std::runtime_error("MSGD command during SAVE UI");
    if(q.name=="dialogAbort"){
        q.shape({});
        // No user button/callback is fabricated. An already posted completion
        // cannot be silently recalled; that race remains an explicit stop.
        auto pending=[&](const auto& event){return event[1]==258 &&
            event[2]==((std::uint64_t(u.userdata)<<32)|u.callback);};
        for(const auto& queue:s.events.queues)if(queue.key==(UINT64_C(0x8006010000000100)|u.port))
            for(const auto& event:queue.data)if(pending(event))throw std::runtime_error("MSGD Abort races queued completion");
        for(const auto& event:s.events.deferred)if(event.first==(UINT64_C(0x8006010000000100)|u.port) && pending(event.second))
            throw std::runtime_error("MSGD Abort races deferred completion");
        u.open=false;u.closing=false;u.pad_release_guard=true;overlay(u);std::fprintf(stderr,"native VSH dialog: aborted\n");return result(0);
    }
    if(q.name=="dialogClose"){
        q.shape({"delayTime"});const auto bits=q.attr("delayTime",2).value;
        float delay;std::memcpy(&delay,&bits,4);
        if(!std::isfinite(delay))throw std::runtime_error("MSGD non-finite Close delay");
        if(!u.open)return result(0x8002b302);
        // Public API and reference cellMsgDialogClose use milliseconds.
        if(delay>86400000)throw std::runtime_error("MSGD Close delay exceeds host backend bound");
        u.deadline=std::chrono::steady_clock::now()+std::chrono::microseconds(static_cast<std::int64_t>(std::max(delay,0.f)*1000.));
        u.closing=true;return result(0);
    }
    if(q.name=="dialogPbSetMsg" || q.name=="dialogPbReset" || q.name=="dialogPbInc"){
        if(q.name=="dialogPbSetMsg")q.shape({"index","msgString"});
        else if(q.name=="dialogPbInc")q.shape({"index","value"});
        else q.shape({"index"});
        const auto index=q.integer("index");
        if(!u.open)return result(0x8002b302);
        if(index>=((u.type>>12)&3))return result(0x8002b301);
        if(q.name=="dialogPbSetMsg"){
            const auto text=q.string("msgString");
            if(text.size()>63)return result(0x8002b301);
            u.progress_message[index]=text;
        }else if(q.name=="dialogPbReset")u.progress[index]=0;
        else{
            // VSH 0x12d178..18c narrows both index and increment to u8.
            const auto value=q.integer("value")&255;
            u.progress[index]=std::min(100u,u.progress[index]+value);
        }
        std::fprintf(stderr,"native VSH dialog: progress[%u]=%u%% %s\n",index,u.progress[index],u.progress_message[index].c_str());
        overlay(u);return result(0);
    }
    throw std::runtime_error("unsupported MSGD command: "+q.name);
}
static void save_display(const NativeVshUi& u) {
    overlay(u);
    if(u.save_confirm){display(u);return;}
    std::fprintf(stderr,"native VSH dialog: savedata [arrows=select, cross=confirm, circle=back]\n");
    for(unsigned i=0;i<u.choices.size();++i)
        std::fprintf(stderr,"  %s%s%s (%s)\n",i==u.cursor?"> ":"  ",
                     u.choices[i].fresh?"nová data: ":"",u.choices[i].title.c_str(),u.choices[i].directory.c_str());
    if(u.choices.empty())std::fprintf(stderr,"  no savedata; circle=back\n");
}
static void save_finish(PpuState& s,bool cancel) {
    auto& u=s.settings.vsh_ui;auto& d=s.settings.savedata;
    // VSH 0x135fc8/136240/136268/13628c: second resOpenSaveList
    // packet contains result, selected directory and MrID (no extra guest call).
    // CELL_CANCEL=1 is returned directly by the 3.01 client at 0x2011fac0.
    if(u.kind==NativeVshUi::SaveDelete){
        if(!cancel){
            const auto target=savedata_vsh::path(s,u.choices.at(u.cursor).directory);
            // Validate all paths before the atomic removal from the visible namespace.
            (void)savedata_vsh::metadata(s,u.choices.at(u.cursor).directory);
            (void)savedata_vsh::files(s,u.choices.at(u.cursor).directory);
            auto pattern=(target.parent_path()/".anyps3-save-delete-XXXXXX").string();
            if(!::mkdtemp(pattern.data()))throw std::runtime_error("SAVE delete staging allocation failed");
            const std::filesystem::path tomb=pattern;
            std::filesystem::rename(target,tomb/"data");
            const int fd=::open(target.parent_path().c_str(),O_RDONLY|O_DIRECTORY);
            if(fd<0)throw std::runtime_error("SAVE delete parent open failed");
            const int sync=::fsync(fd),closed=::close(fd);
            if(sync || closed)throw std::runtime_error("SAVE delete parent flush failed");
            std::filesystem::remove_all(tomb);
        }
        savedata_vsh::result(d,"resOpenDeleteList",cancel?1:0);
    }else{
    if(!cancel)d.selected_list=true;
    savedata_vsh::enqueue(d,savedata_vsh::reply("resOpenSaveList",{{"elmResult",cancel?1u:0u},{"elmMrID",0}},
                           "",{},"elmDirName",cancel?"":u.choices.at(u.cursor).directory));
    }
    u.open=false;u.pad_release_guard=true;u.save_confirm=false;u.save_request.clear();u.save_raw.clear();u.save_raw_expected=0;u.icon.clear();overlay(u);
}
static void save_input(PpuState& s,std::uint32_t edges) {
    auto& u=s.settings.vsh_ui;
    if(u.save_raw_expected)return; // raw list must be complete before showing a choice
    const bool cross=edges&(s.settings.enter_button?0x400000:0x200000),
               circle=edges&(s.settings.enter_button?0x200000:0x400000);
    if(u.kind==NativeVshUi::SaveError){
        if(cross!=circle){
            auto& d=s.settings.savedata;
            savedata_vsh::cleanup(d);savedata_vsh::result(d,"resAbort");
            u.open=false;u.pad_release_guard=true;u.save_request.clear();overlay(u);
        }
        return;
    }
    if(u.save_confirm){
        if(edges&0x90){u.type&=~0x100;display(u);}
        else if(edges&0x60){u.type|=0x100;display(u);}
        // In confirmation we keep the chosen slot in cursor; bit8 holds YES/NO.
        if(cross!=circle){
            if(cross && !(u.type&0x100)){save_finish(s,false);return;}
            u.save_confirm=false;save_display(u);input(s,true);
        }
        return;
    }
    if(!u.choices.empty()){
        if(edges&0x90){if(u.cursor) --u.cursor;save_display(u);}
        else if(edges&0x60){if(u.cursor+1<u.choices.size())++u.cursor;save_display(u);}
    }
    if(cross==circle)return;
    if(circle){save_finish(s,true);return;}
    if(u.choices.empty())return;
    auto& choice=u.choices[u.cursor];
    if((s.settings.savedata.mode==4 || u.kind==NativeVshUi::SaveDelete) && !choice.fresh){
        u.message=(u.kind==NativeVshUi::SaveDelete?"Delete savedata ":"Overwrite savedata ")+choice.directory+"?";u.type=0x110;u.save_confirm=true;
        display(u);return;
    }
    save_finish(s,false);
}
static void save_ready(PpuState& s) {
    auto& u=s.settings.vsh_ui;auto& d=s.settings.savedata;Document q(u.save_request);
    const auto set=q.blob("elmDataListSet",24);const auto focus=savedata_vsh::be(set,0);
    u.choices.clear();
    for(unsigned i=0;i<u.save_raw.size();i+=48){
        const auto dir=savedata_vsh::text(u.save_raw,i,32);savedata_vsh::component(dir,31);
        for(unsigned j=40;j<48;++j)if(u.save_raw[i+j])throw std::runtime_error("SAVE nonzero list reserved byte");
        const auto target=savedata_vsh::path(s,dir);
        if(!std::filesystem::exists(target))continue; // fixedList filters actual saves; does not create one
        const auto f=savedata_vsh::metadata(s,dir);struct stat st{};
        if(::stat(target.c_str(),&st))throw std::runtime_error("SAVE list stat failed");
        if(std::any_of(u.choices.begin(),u.choices.end(),[&](const auto& c){return c.directory==dir;}))
            throw std::runtime_error("SAVE duplicate fixedList directory");
        u.choices.push_back({dir,f.string("SUB_TITLE",128),st.st_mtime,false});
        u.choices.back().detail=f.string("DETAIL",1024);
        const auto icon=savedata_vsh::path(s,dir,"ICON0.PNG");
        if(std::filesystem::exists(icon))u.choices.back().picture=savedata_vsh::read(icon,0x37000);
    }
    // Preserve callback-provided fixedList order. Sort was applied by getDirList,
    // openSaveList has no sort/search attributes in the original client.
    if(q.attrs.count("elmNewData")){
        const auto fresh=q.blob("elmNewData",16);const auto position=savedata_vsh::be(fresh,0);
        if(position>1 || savedata_vsh::be(fresh,12))throw std::runtime_error("SAVE unsupported newData position/reserved");
        const auto dir=q.string("elmDirName");savedata_vsh::component(dir,31);
        if(std::filesystem::exists(savedata_vsh::path(s,dir)))
            throw std::runtime_error("SAVE newData aliases existing slot");
        NativeVshUi::Choice c{dir,q.attrs.count("elmNewDataIconTitleName")?q.string("elmNewDataIconTitleName"):"NEW DATA",0,true};
        if(position)u.choices.push_back(c);else u.choices.insert(u.choices.begin(),c);
    }
    u.cursor=0;
    if(!u.choices.empty()){
        if(focus==0){
            const auto dir=q.string("elmFocusDirName");
            const auto found=std::find_if(u.choices.begin(),u.choices.end(),[&](const auto& c){return c.directory==dir;});
            if(found==u.choices.end())throw std::runtime_error("SAVE missing focused directory");
            u.cursor=found-u.choices.begin();
        }else if(focus==2)u.cursor=u.choices.size()-1;
        else if(focus==3 || focus==4){
            bool found=false;
            for(unsigned i=0;i<u.choices.size();++i)if(!u.choices[i].fresh){
                if(!found || (focus==3?u.choices[i].mtime>u.choices[u.cursor].mtime:u.choices[i].mtime<u.choices[u.cursor].mtime))u.cursor=i;
                found=true;
            }
        }else if(focus==5){
            const auto found=std::find_if(u.choices.begin(),u.choices.end(),[](const auto& c){return c.fresh;});
            if(found==u.choices.end())throw std::runtime_error("SAVE NEWDATA focus without newData");
            u.cursor=found-u.choices.begin();
        }
    }
    (void)d;u.save_raw_expected=0;
    if(q.attrs.count("elmNewDataIcon")){
        const auto icon=q.blob("elmNewDataIcon",16);u.raw_phase=2;u.save_raw.clear();u.save_raw_expected=savedata_vsh::be(icon,4);
        if(u.save_raw_expected)return;
    }
    input(s,true);save_display(u);
}
static void save_raw(PpuState& s,const Bytes& bytes,bool end) {
    auto& u=s.settings.vsh_ui;
    if(!u.open || !u.save_raw_expected ||
       bytes.size()>u.save_raw_expected-u.save_raw.size())throw std::runtime_error("SAVE unexpected UI raw list");
    u.save_raw.insert(u.save_raw.end(),bytes.begin(),bytes.end());
    if(end){
        if(u.save_raw.size()!=u.save_raw_expected)throw std::runtime_error("SAVE truncated fixedList packet");
        if(u.raw_phase==2){
            for(auto& choice:u.choices)if(choice.fresh)choice.picture=u.save_raw;
            u.icon=u.save_raw;u.save_raw_expected=0;input(s,true);save_display(u);}
        else if(u.raw_phase==3){u.icon=u.save_raw;u.save_raw_expected=0;savedata_vsh::result(s.settings.savedata,"resOpenSaveData");overlay(u);}
        else save_ready(s);
    }else if(u.save_raw.size()==u.save_raw_expected)throw std::runtime_error("SAVE missing fixedList end flag");
}
} // namespace vsh_ui
static void savedata_ui_open_list(PpuState& s,const std::vector<std::uint8_t>& bytes) {
    using namespace savedata_vsh;
    auto& d=s.settings.savedata;auto& u=s.settings.vsh_ui;Document q(bytes);
    q.shape({"elmUtilType","elmUtilOption","elmUserId","elmDataListSet","elmContainer"},
            {"elmFocusDirName","elmNewData","elmDirName","elmNewDataIcon","elmNewDataIconTitleName"});
    mode(d,q);user(s,q.integer("elmUserId"));
    const auto set=q.blob("elmDataListSet",24);(void)q.blob("elmContainer",4);
    const auto count=be(set,8),focus=be(set,0);
    if(u.open || d.opened || (d.mode!=4 && d.mode!=5) || q.integer("elmUtilOption")!=d.options ||
       count>2048 || focus>5 || be(set,20))throw std::runtime_error("SAVE unsupported list state/mode/fields");
    if(bool(be(set,4))!=bool(q.attrs.count("elmFocusDirName")))throw std::runtime_error("SAVE inconsistent focused name");
    if(bool(be(set,16))!=bool(q.attrs.count("elmNewData")))throw std::runtime_error("SAVE inconsistent newData");
    if(q.attrs.count("elmNewData")){
        const auto fresh=q.blob("elmNewData",16);
        if(d.mode!=4 || !be(fresh,4))throw std::runtime_error("SAVE invalid newData");
        if(bool(be(fresh,8))!=bool(q.attrs.count("elmNewDataIcon")))throw std::runtime_error("SAVE inconsistent newData icon");
        if(q.attrs.count("elmNewDataIcon")){
            const auto icon=q.blob("elmNewDataIcon",16);
            if(be(icon,4)>0x37000 || (be(icon,4) && !be(icon,8)))throw std::runtime_error("SAVE invalid newData icon size/pointer");
            if(bool(be(icon,0))!=bool(q.attrs.count("elmNewDataIconTitleName")))throw std::runtime_error("SAVE missing newData icon title");
        }
        (void)q.string("elmDirName");
    }else if(q.attrs.count("elmDirName") || q.attrs.count("elmNewDataIcon") || q.attrs.count("elmNewDataIconTitleName"))
        throw std::runtime_error("SAVE newData attributes without newData");
    if((count && !be(set,12)) || (focus==0 && !q.attrs.count("elmFocusDirName")))
        throw std::runtime_error("SAVE missing list/focus data");
    (void)vsh_ui::key(s,1);u.kind=NativeVshUi::SaveList;u.message="SAVED DATA";u.save_request=bytes;u.save_raw.clear();
    u.save_raw_expected=count*48;u.raw_phase=1;u.save_confirm=false;u.closing=false;u.open=true;
    // VSH 0x135798 acknowledges first, then reads count*48 raw bytes at
    // 0x135d1c..dac. The user choice is a separate, later packet.
    if(!count)vsh_ui::save_ready(s);
    result(d,"resOpenSaveList");
}
static void savedata_ui_error(PpuState& s,const std::vector<std::uint8_t>& bytes) {
    using namespace savedata_vsh;
    auto& u=s.settings.vsh_ui;Document q(bytes);const auto a=q.blob("elmAbortParam",64);
    if(u.open && u.kind!=NativeVshUi::SaveIndicator)throw std::runtime_error("SAVE error dialog conflicts with active VSH dialog");
    u.icon.clear();
    char message[96];std::snprintf(message,sizeof(message),"Savedata error 0x%08x (callback %d)",be(a,8),static_cast<std::int32_t>(be(a,12)));
    if(q.attrs.count("elmInvalidMsg"))u.message=q.string("elmInvalidMsg");else u.message=message;
    u.kind=NativeVshUi::SaveError;u.save_request=bytes;u.type=0x20;u.cursor=0;
    u.save_raw_expected=0;u.save_confirm=false;u.closing=false;vsh_ui::input(s,true);u.open=true;
    vsh_ui::display(u);
}
[[maybe_unused]] static void vsh_ui_poll(PpuState& s) { vsh_ui::poll(s); }

static void savedata_ui_close(PpuState& s){
    auto& u=s.settings.vsh_ui;
    if(u.kind==NativeVshUi::SaveIndicator){u.open=false;u.save_raw_expected=0;u.icon.clear();vsh_ui::overlay(u);}
}
static void savedata_ui_indicator(PpuState& s,const std::vector<std::uint8_t>& bytes){
    using namespace savedata_vsh;Document q(bytes);auto& u=s.settings.vsh_ui;
    const auto indicator=q.blob("elmIndicator",24);
    if(be(indicator,0)>0x24 || be(indicator,4)>3 || be(indicator,16)>0x37000 || be(indicator,20) ||
       (be(indicator,16) && !be(indicator,12)) || bool(be(indicator,8))!=bool(q.attrs.count("elmMsg")))
        throw std::runtime_error("SAVE invalid indicator");
    u.kind=NativeVshUi::SaveIndicator;u.open=true;u.type=0;u.progress[0]=q.integer("elmProgress");
    u.message=q.attrs.count("elmMsg")?q.string("elmMsg"):((s.settings.savedata.mode&1)?"Loading saved data":"Saving data");
    u.save_raw.clear();u.icon.clear();u.raw_phase=3;u.save_raw_expected=be(indicator,16);
    vsh_ui::overlay(u);std::fprintf(stderr,"native VSH savedata indicator: %s\n",u.message.c_str());
    if(!u.save_raw_expected)result(s.settings.savedata,"resOpenSaveData");
}
static void savedata_ui_delete(PpuState& s,const std::vector<std::uint8_t>& bytes){
    using namespace savedata_vsh;Document q(bytes);auto& d=s.settings.savedata;auto& u=s.settings.vsh_ui;
    q.shape({"elmUtilType","elmUtilOption","elmContainer","elmUserId"},{"elmDirName"});
    const auto mode=q.integer("elmUtilType");(void)q.integer("elmUtilOption");(void)q.blob("elmContainer",4);
    if(mode==14)throw std::runtime_error("SAVE mode14 requires verified callback/delete handshake");
    if(mode!=8)throw std::runtime_error("SAVE unsupported delete mode "+std::to_string(mode));
    if(d.owner || u.open)throw std::runtime_error("SAVE delete busy");
    user(s,q.integer("elmUserId"));u.choices.clear();u.save_request=bytes;u.save_raw_expected=0;
    const auto parent=root(s);
    if(std::filesystem::exists(parent))for(const auto& e:std::filesystem::directory_iterator(parent)){
        const auto name=e.path().filename().string();if(name.rfind(".anyps3-save-",0)==0)continue;
        if(q.attrs.count("elmDirName") && q.string("elmDirName")!=name)continue;
        component(name,31);const auto f=metadata(s,name);struct stat st{};
        if(::stat(path(s,name).c_str(),&st))throw std::runtime_error("SAVE delete list stat failed");
        u.choices.push_back({name,f.string("SUB_TITLE",128),st.st_mtime,false});
        u.choices.back().detail=f.string("DETAIL",1024);
        const auto icon=path(s,name,"ICON0.PNG");if(std::filesystem::exists(icon))u.choices.back().picture=read(icon,0x37000);
    }
    std::sort(u.choices.begin(),u.choices.end(),[](const auto& a,const auto& b){return a.directory<b.directory;});
    u.kind=NativeVshUi::SaveDelete;u.cursor=0;u.save_confirm=false;u.type=0;u.message="DELETE SAVED DATA";
    u.open=true;vsh_ui::input(s,true);vsh_ui::save_display(u);
}
'''
