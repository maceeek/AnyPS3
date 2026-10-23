"""3.01 CXML requests; independent button/event/ring expectations.

No game runs. Direct backend inputs and a real SDL virtual controller both
exercise NativePad::poll/sample without consuming cellPadGetData's history.
"""
import ctypes.util
import shutil
import struct
import unittest

from tests import test_runtime_objects
from tests.test_savedata_vsh import document, vector, fixture_sfo


def msg(name='dialogOpen2', **attrs):
    b = bytearray(document(name, **attrs))
    b[:4] = b'MSGD'
    return bytes(b)


def opening(kind=0x20, callback=0x10100, userdata=0xfedcba98, text='Continue?'):
    return msg(type=kind, msgString=text, callbackFunc=callback, userData=userdata)


def close(bits=0):
    b = bytearray(msg('dialogClose', delayTime=bits))
    b[96:100] = struct.pack('>I', 2)  # cxml f32, libsysutil 0x201119bc
    return bytes(b)


def save_list(mode=4, count=1, fresh=True, focus=1):
    attrs = dict(elmUtilType=mode, elmUtilOption=0, elmUserId=0,
                 elmDataListSet=struct.pack('>6I', focus, 0, count, 0x10300 if count else 0,
                                           0x10200 if fresh else 0, 0),
                 elmContainer=bytes.fromhex('ffffffff'))
    if fresh:
        attrs.update(elmNewData=struct.pack('>4I', 1, 0x10100, 0, 0), elmDirName='BCES00511_SLOT2')
    return document('openSaveList', **attrs)


HARNESS = r'''
 (void)call;using B=std::vector<std::uint8_t>;
 auto& u=s.settings.vsh_ui;
 s.events.queues.push_back({0x800,16,UINT64_C(0x8006010000000102),{}});
 s.sysutil_connections.insert(2);
 auto req=[&](const B& b){return vsh_ui::request(s,b,0);};
 auto accepted=[&](const B& b){assert(b.size()==128);assert(b[0]=='M' && b[1]=='S' && b[2]=='G' && b[3]=='D');
   assert(savedata_vsh::be(b,68)==1 && savedata_vsh::be(b,96)==1 && savedata_vsh::be(b,100)==0);
   const auto a=savedata_vsh::be(b,24);
   assert(std::string(reinterpret_cast<const char*>(b.data()+a+savedata_vsh::be(b,64)))=="result");};
 auto& q=s.events.queues[0];
 auto event=[&](std::uint64_t value){assert(!u.open && q.data.size()==1);
   assert(q.data[0][0]==0 && q.data[0][1]==258 && q.data[0][2]==UINT64_C(0xfedcba9800010100) && q.data[0][3]==value);
   q.data.clear();};
 (void)req;(void)accepted;(void)event;
'''

INPUT = r'''
 static std::array<std::uint16_t,7> digital{};
 auto& p=s.pad;p.maximum=7;
 p.update=+[]{};p.count=+[]{return 0;};p.attached=+[](void*){return 1;};
 p.close=+[](void*){};p.quit=+[](std::uint32_t){};
 p.button=+[](void* d,int b)->std::uint8_t{
   const auto index=reinterpret_cast<std::uintptr_t>(d)-1;
   return (digital[index]>>b)&1;
 };
 p.axis=+[](void*,int)->std::int16_t{return 0;};
 p.ports[0].device=reinterpret_cast<void*>(1);p.ports[0].instance=7;
 auto press=[&](unsigned bits){digital[0]=bits;vsh_ui_poll(s);};
 auto release=[&]{press(0);};
 (void)release;
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class VshUiTests(unittest.TestCase):
    def run_native(self, body):
        test_runtime_objects.RuntimeObjectsTests().run_native(HARNESS + body)

    def test_input_edges_choices_cancel_policy_and_guest_pad_history(self):
        self.run_native(INPUT +
            'accepted(req(' + vector(opening()) + '));' + r'''
 assert(u.open && q.data.empty());vsh_ui_poll(s);assert(q.data.empty());
 // The confirming CROSS closes the dialog; while it is still held the game gets
 // neutral data (len 8, no buttons), never the leaked confirmation (RPCS3 cellPad.cpp:372-375).
 press(1);event(1);
 r[3]=0;r[4]=0x10200;assert(pad_service(s,4)==0 && mem.load(0x10200,4)==8 && mem.load(0x1020a,2)==0);
''' + 'accepted(req(' + vector(opening()) + '));' + r'''
 assert(u.open);vsh_ui_poll(s);assert(q.data.empty()); // held cross cannot answer twice
 release();press(2);event(3); // circle/back on OK
 release();
''' + 'accepted(req(' + vector(opening(0x110)) + '));' + r'''
 assert(u.cursor==1);press(1);event(2);release(); // default NO
''' + 'accepted(req(' + vector(opening(0x110)) + '));' + r'''
 press(1u<<13);assert(u.cursor==0 && q.data.empty());release();press(1);event(1);release();
''' + 'accepted(req(' + vector(opening(0xa0)) + '));' + r'''
 press(2);assert(u.open && q.data.empty());release();press(1);event(1);release();
''' + 'accepted(req(' + vector(opening(0x10)) + '));press(2);event(2);release();' +
            'accepted(req(' + vector(opening()) + '));' + r'''
 press(3);assert(u.open && q.data.empty());release();press(1);event(1);
 // Another port sharing the instance id is still independently debounced.
 release();
''' + 'accepted(req(' + vector(opening()) + '));' + r'''
 p.ports[1].device=reinterpret_cast<void*>(2);p.ports[1].instance=8;digital[1]=1;
 vsh_ui_poll(s);assert(u.open);digital[1]=0;vsh_ui_poll(s);digital[1]=1;vsh_ui_poll(s);event(1);
''')

    def test_progress_wait_close_abort_and_exactly_once_events(self):
        self.run_native(INPUT + 'accepted(req(' + vector(opening(0x2000)) + '));' + r'''
 press(1);assert(u.open && q.data.empty());release();press(2);assert(u.open && q.data.empty());
''' + 'accepted(req(' + vector(msg('dialogPbInc', index=1, value=37)) + '));assert(u.progress[1]==37);' +
            'accepted(req(' + vector(msg('dialogPbSetMsg', index=1, msgString='Working')) + '));assert(u.progress_message[1]=="Working");' +
            'accepted(req(' + vector(msg('dialogPbReset', index=1)) + '));assert(u.progress[1]==0);' +
            'accepted(req(' + vector(msg('dialogPbInc', index=0, value=356)) + '));assert(u.progress[0]==100);assert(u.open && q.data.empty());' +
            'auto invalid=req(' + vector(msg('dialogPbInc', index=2, value=1)) + ');assert(savedata_vsh::be(invalid,100)==0x8002b301);' +
            'accepted(req(' + vector(close()) + '));assert(u.open && q.data.empty());vsh_ui_poll(s);event(UINT64_MAX);' + r'''
 vsh_ui_poll(s);vsh_ui_poll(s);assert(q.data.empty());release();
''' + 'accepted(req(' + vector(opening()) + '));' +
            'accepted(req(' + vector(msg('dialogAbort')) + '));press(1);assert(!u.open && q.data.empty());' +
            'accepted(req(' + vector(msg('dialogAbort')) + '));' +
            'auto closed=req(' + vector(close()) + ');assert(savedata_vsh::be(closed,100)==0x8002b302);release();' +
            'accepted(req(' + vector(opening()) + '));press(1);assert(q.data.size()==1);' +
            'fault([&]{req(' + vector(msg('dialogAbort')) + ');});event(1);release();' +
            'accepted(req(' + vector(opening()) + '));accepted(req(' + vector(close(0x447a0000)) + '));' + r'''
 // 1000ms is pending. Deterministically advance host deadline, without sleeping.
 press(1);assert(u.open && q.data.empty());
 u.deadline=std::chrono::steady_clock::time_point{};vsh_ui_poll(s);event(UINT64_MAX);
''')

    def test_transport_be_event_backpressure_and_malformed_requests(self):
        self.run_native(INPUT + r'''
 const auto ready=req;
 (void)ready;
 s.mmapper.objects.push_back({1,0x10000,0,false,false,std::make_shared<GuestBacking>(0x10000)});
 auto& raw=*s.mmapper.objects[0].bytes;
 auto put=[&](unsigned a,unsigned v){for(unsigned j=0;j<4;++j)raw[a+j]=v>>(24-8*j);};
''' + 'auto b=' + vector(opening()) + r''';
 put(0,0x400);put(4,0x7c00);put(8,b.size());put(12,0);put(16,b.size());put(20,2);put(24,1);
 std::copy(b.begin(),b.end(),raw.begin()+0x400);
 assert(service_sysutil_connect(s,0x86000000));assert(raw[27]==2 && q.data.empty() && u.open);
 const unsigned at=b.size();assert(raw[0x400+at]=='M' && raw[0x401+at]=='S');
 q.capacity=0;press(1);assert(q.data.empty() && s.events.deferred.size()==1 && !u.open);
 vsh_ui_poll(s);assert(s.events.deferred.size()==1);
 q.capacity=1;s.events.flush();event(1);assert(s.events.deferred.empty());release();
''' + 'b=' + vector(opening()) + r''';
 b[99]=3;fault([&]{req(b);});assert(!u.open);
''' + 'b=' + vector(opening()) + r''';
 b[120]=1;fault([&]{req(b);});assert(!u.open);
''' + 'fault([&]{vsh_ui::request(s,' + vector(opening()) + ',1);});' +
            'auto bad=req(' + vector(opening(0x1020)) + ');assert(savedata_vsh::be(bad,100)==0x8002b301 && !u.open);' +
            'bad=req(' + vector(opening(text='x' * 512)) + ');assert(savedata_vsh::be(bad,100)==0x8002b301);' +
            'accepted(req(' + vector(opening(callback=0, text='')) + '));press(1);' + r'''
 assert(q.data.size()==1 && q.data[0][2]==UINT64_C(0xfedcba9800000000));q.data.clear();release();
''' + 'b=' + vector(msg('oskDialogLoad')) + ';b[0]=\'O\';b[1]=\'S\';b[2]=\'K\';b[3]=\'D\';fault([&]{req(b);});')

    def test_savedata_list_overwrite_confirmation_new_slot_and_real_write(self):
        row = b'BCES00511_SLOT1'.ljust(32, b'\0') + b'SLOT1\0\0\0' + bytes(8)
        param = bytearray(1552)
        param[:5] = b'Title'
        param[128:136] = b'New slot'
        param[1288:1292] = b'NEW2'
        stat = document('getSaveDataStat', elmUtilType=4, elmUtilOption=0, elmUserId=0,
                        elmDirName='BCES00511_SLOT2', elmFileListNumMax=0)
        opn = document('openSaveData', elmUtilType=4, elmUserId=0, elmDirName='BCES00511_SLOT2',
                       elmDataStat=struct.pack('>3I', 0x10100, 0, 0), elmProgress=0, elmSysFileParam=param)
        file_set = struct.pack('>3I', 1, 0, 1) + bytes(16) + struct.pack('>5I', 0x10100, 0, 3, 3, 0x10200)
        write = document('writeData', elmDataFileSet=file_set, elmFileType=1, elmFileName='DATA.BIN', elmProgress=0)
        abort = document('abort', elmAbortParam=bytes(64))
        self.run_native(INPUT + r'''
 char hdd[]="/tmp/anyps3-vsh-ui-XXXXXX";assert(::mkdtemp(hdd));s.fs.configure_hdd(hdd);
 auto& d=s.settings.savedata;d.owner=1;d.modules=true;d.mode=4;
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 s.events.queues.push_back({0x900,16,UINT64_C(0x8006010000000100),{}});
 const auto root=std::filesystem::path(hdd)/"home/00000001/savedata";
 const auto original=root/"BCES00511_SLOT1";std::filesystem::create_directories(original);
 auto disk=[&](const std::filesystem::path& p,const B& b){std::ofstream f(p,std::ios::binary);f.write(reinterpret_cast<const char*>(b.data()),b.size());assert(f.good());};
''' + 'disk(original/"PARAM.SFO",' + vector(fixture_sfo()) + ');disk(original/"DATA.BIN",B{65,66,67});' +
            'savedata_vsh::request(s,' + vector(save_list()) + ');assert(u.open && u.save_raw_expected==48 && u.choices.empty());' + r'''
 assert(d.replies.size()==1);savedata_vsh::Document accept(d.replies[0].bytes);
 assert(accept.name=="resOpenSaveList" && accept.integer("elmResult")==0);d.replies.clear();
''' + 'B rows=' + vector(row) + ';vsh_ui::save_raw(s,B(rows.begin(),rows.begin()+17),false);assert(u.choices.empty());' + r'''
 vsh_ui::save_raw(s,B(rows.begin()+17,rows.end()),true);
 assert(u.open && u.choices.size()==2 && u.cursor==0 && d.replies.empty());
 assert(u.choices[0].directory=="BCES00511_SLOT1" && !u.choices[0].fresh && u.choices[1].fresh);
 press(1);assert(u.save_confirm && d.replies.empty());release();
 press(1);assert(!u.save_confirm && u.open && d.replies.empty()); // default NO returns to list
 release();press(1);assert(u.save_confirm);release();press(1u<<13);release();press(1);
 assert(!u.open && d.replies.size()==1);savedata_vsh::Document selected(d.replies[0].bytes);
 assert(selected.name=="resOpenSaveList" && selected.integer("elmResult")==0 && selected.integer("elmMrID")==0);
 assert(selected.string("elmDirName")=="BCES00511_SLOT1");d.replies.clear();
 assert(savedata_vsh::read(original/"DATA.BIN")==B({65,66,67}));
 release();
''' + 'savedata_vsh::request(s,' + vector(save_list()) + ');d.replies.clear();vsh_ui::save_raw(s,rows,true);' + r'''
 press(1u<<12);assert(u.cursor==1 && d.replies.empty());release();press(1);
 assert(!u.open && !u.save_confirm && d.replies.size()==1);
 savedata_vsh::Document fresh(d.replies[0].bytes);assert(fresh.integer("elmResult")==0 && fresh.string("elmDirName")=="BCES00511_SLOT2");
 d.replies.clear();assert(!std::filesystem::exists(root/"BCES00511_SLOT2"));
''' + 'savedata_vsh::request(s,' + vector(stat) + ');assert(d.replies.size()==2);d.replies.clear();' +
            'savedata_vsh::request(s,' + vector(opn) + ');assert(d.opened && d.replies.size()==1);d.replies.clear();' +
            'savedata_vsh::request(s,' + vector(write) + ');assert(d.writing && d.replies.size()==1);d.replies.clear();' +
            'savedata_vsh::raw_write(d,B{88,89,90},true);assert(d.replies.size()==1);d.replies.clear();' +
            'savedata_vsh::request(s,' + vector(abort) + ');assert(d.replies.size()==1 && d.owner==0);d.replies.clear();' + r'''
 assert(savedata_vsh::read(root/"BCES00511_SLOT2/DATA.BIN")==B({88,89,90}));
 assert(savedata_vsh::read(original/"DATA.BIN")==B({65,66,67}));
 assert(s.events.queues[0].data.empty() && s.events.queues[1].data.empty());
 std::filesystem::remove_all(hdd);
''')

    def test_savedata_cancel_empty_list_and_raw_protocol_errors(self):
        self.run_native(INPUT + r'''
 char hdd[]="/tmp/anyps3-vsh-ui-XXXXXX";assert(::mkdtemp(hdd));s.fs.configure_hdd(hdd);
 auto& d=s.settings.savedata;d.owner=1;d.modules=true;d.mode=5;
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 s.events.queues.push_back({0x900,16,UINT64_C(0x8006010000000100),{}});
''' + 'savedata_vsh::request(s,' + vector(save_list(mode=5, count=0, fresh=False)) + ');d.replies.clear();' + r'''
 assert(u.open && u.choices.empty());press(1);assert(u.open && d.replies.empty());release();press(2);
 assert(!u.open && d.replies.size()==1);savedata_vsh::Document cancelled(d.replies[0].bytes);
 assert(cancelled.integer("elmResult")==1 && cancelled.string("elmDirName").empty());d.replies.clear();release();
''' + 'savedata_vsh::request(s,' + vector(save_list(mode=5, fresh=False)) + ');d.replies.clear();' + r'''
 fault([&]{vsh_ui::save_raw(s,B{0},true);});assert(u.open && d.replies.empty());
 assert(!std::filesystem::exists(std::filesystem::path(hdd)/"home"));
 std::filesystem::remove_all(hdd);
''')

    def test_savedata_error_dialog_waits_for_input_and_rolls_back(self):
        abort_param = struct.pack('>7I', 0, 0, 0x8002b401, 0xfffffffc, 0, 0, 1) + bytes(36)
        abort = document('abort', elmAbortParam=abort_param, elmInvalidMsg='Cannot save')
        self.run_native(INPUT + r'''
 char hdd[]="/tmp/anyps3-vsh-ui-XXXXXX";assert(::mkdtemp(hdd));s.fs.configure_hdd(hdd);
 auto& d=s.settings.savedata;d.owner=1;d.modules=true;d.mode=4;
 d.staging=std::filesystem::path(hdd)/".staging";std::filesystem::create_directory(d.staging);
 const auto stage=d.staging;
''' + 'savedata_vsh::request(s,' + vector(abort) + ');' + r'''
 assert(u.open && u.kind==NativeVshUi::SaveError && u.message=="Cannot save" && d.replies.empty());
 assert(std::filesystem::exists(stage));vsh_ui_poll(s);assert(d.replies.empty());
 press(1);assert(!u.open && !d.owner && d.replies.size()==1 && !std::filesystem::exists(stage));
 savedata_vsh::Document done(d.replies[0].bytes);assert(done.name=="resAbort" && done.integer("elmResult")==0);
 assert(!std::filesystem::exists(std::filesystem::path(hdd)/"home"));d.replies.clear();
 std::filesystem::remove_all(hdd);
''')

    def test_open_save_list_parks_packet_read_until_host_input_produces_reply(self):
        # FIX7 (run 805au1): PacketRead on channel 1 must block while the SAVE list waits for the user, as VSH
        # signals the condition only when it writes a reply; the former immediate return spun the reader and starved
        # the game (60 -> 7 FPS, most cellAudio blocks unmixed). Expectations from the protocol: the reader blocks
        # on condition 0x86000400 and releases mutex 0x85000100; another thread runs; with no input it stays
        # blocked; CIRCLE (cancel) queues resOpenSaveList(elmResult=1), the reader is woken through its condition
        # (resume_cond), reacquires the mutex, and its next wait publishes that reply in the ring.
        row = b'BCES00511_SLOT1'.ljust(32, b'\0') + b'SLOT1\0\0\0' + bytes(8)
        self.run_native(INPUT + r'''
 char hdd[]="/tmp/anyps3-vsh-park-XXXXXX";assert(::mkdtemp(hdd));s.fs.configure_hdd(hdd);
 auto& d=s.settings.savedata;d.owner=1;d.modules=true;d.mode=4;
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 s.events.queues.push_back({0x900,16,UINT64_C(0x8006010000000100),{}});
 const auto original=std::filesystem::path(hdd)/"home/00000001/savedata/BCES00511_SLOT1";std::filesystem::create_directories(original);
 {std::ofstream f(original/"PARAM.SFO",std::ios::binary);''' + 'const B sfo=' + vector(fixture_sfo()) + r''';
  f.write(reinterpret_cast<const char*>(sfo.data()),sfo.size());assert(f.good());}
''' + 'savedata_vsh::request(s,' + vector(save_list(fresh=False)) + ');d.replies.clear();' +
            'B rows=' + vector(row) + ';vsh_ui::save_raw(s,rows,true);' + r'''
 assert(u.open && u.kind==NativeVshUi::SaveList && u.choices.size()==1 && d.replies.empty());
 auto& raw=*s.mmapper.objects.front().bytes;const unsigned base=0x8000,cap=0x7c00;
 auto word=[&](unsigned a){return(unsigned(raw[a])<<24)|(unsigned(raw[a+1])<<16)|(unsigned(raw[a+2])<<8)|raw[a+3];};
 auto put=[&](unsigned a,unsigned v){for(unsigned j=0;j<4;++j)raw[a+j]=v>>(24-8*j);};
 put(base,1024);put(base+4,cap);
 const std::uint64_t cond=0x86000400,mutex_id=0x85000100;
 auto mutex=[&]()->KernelSync::Mutex&{for(auto& m:s.kernel.mutexes)if(m.id==mutex_id)return m;assert(false);std::abort();};
 for(auto& c:s.kernel.conditions)if(c.id==cond)c.handles=1;
 s.ppu_threads.push_back(static_cast<PpuContext&>(s));PpuContext peer;peer.thread_id=2;s.ppu_threads.push_back(peer);
 mutex().owner=1;
 bool blocked=false;try{call(107,cond);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==4 && s.wait_id==cond && mutex().owner==0);
 assert(u.parked.size()==1 && u.parked[0].first==1 && u.parked[0].second==cond);
 ppu_schedule(s,true);assert(s.thread_id==2);
 u.park_poll={};ppu_schedule(s);assert(s.thread_id==2 && u.open && d.replies.empty() && u.parked.size()==1);
 digital[0]=2;u.park_poll={};ppu_schedule(s); // the scheduler polls the open UI; CIRCLE cancels the list
 assert(!u.open && d.replies.size()==1 && u.parked.empty());
 const auto& reader=s.thread_id==1?static_cast<PpuContext&>(s):s.ppu_threads[0];
 assert(reader.wait_kind==0 && reader.resume_cond==cond);
 if(s.thread_id!=1){ppu_schedule(s);assert(s.thread_id==1);}
 assert(call(107,cond)==0 && mutex().owner==1 && s.resume_cond==0);   // woken wait returns, mutex reacquired
 assert(call(107,cond)==0 && word(base+16)>0 && word(base+24)==2);  // next wait publishes the reply
 B reply;for(unsigned i=0;i<word(base+16);++i)reply.push_back(raw[base+1024+(word(base+12)+i)%cap]);
 savedata_vsh::Document done(reply);assert(done.name=="resOpenSaveList" && done.integer("elmResult")==1);
 digital[0]=0;std::filesystem::remove_all(hdd);
''')

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_real_sdl_virtual_controller_completion(self):
        self.run_native(r'''
 auto& p=s.pad;p.load();assert(p.init(0x2000)==0);
 const auto attach=p.symbol<int(*)(int,int,int,int)>("SDL_JoystickAttachVirtual");
 const auto detach=p.symbol<int(*)(int)>("SDL_JoystickDetachVirtual");
 const auto joyopen=p.symbol<void*(*)(int)>("SDL_JoystickOpen");
 const auto joyclose=p.symbol<void(*)(void*)>("SDL_JoystickClose");
 const auto set=p.symbol<int(*)(void*,int,std::uint8_t)>("SDL_JoystickSetVirtualButton");
 const auto device=attach(1,6,15,0);assert(device>=0);auto* joystick=joyopen(device);assert(joystick);
''' + 'accepted(req(' + vector(opening()) + '));assert(p.maximum==0 && u.input_observer);' + r'''
 assert(set(joystick,0,1)==0);vsh_ui_poll(s);event(1);assert(p.maximum==0);
 assert(set(joystick,0,0)==0);r[3]=7;assert(pad_service(s,0)==0);
''' + 'accepted(req(' + vector(opening()) + '));vsh_ui_poll(s);assert(u.open && q.data.empty());' + r'''
 assert(set(joystick,0,1)==0);vsh_ui_poll(s);event(1);
 vsh_ui_poll(s);assert(q.data.empty());joyclose(joystick);assert(detach(device)==0);
''')
