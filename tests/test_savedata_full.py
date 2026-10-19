"""SAVEFULL native protocol, persistence, malformed input and presentation vectors."""
import struct
import unittest
from pathlib import Path
from anyps3.sfo import parse_sfo, encode_sfo
from anyps3.errors import AnyPS3Error
from tests import test_savedata_vsh as backend
from tests.test_savedata_vsh import (document, vector, owner, load,
                                    stat_request, open_request, file_request, abort, fixture_sfo)
from tests.test_vsh_ui import INPUT, save_list


def param():
    b=bytearray(1552)
    b[:7]=b'Ratchet';b[128:132]=b'Slot';b[256:262]=b'Detail';b[1288:1292]=b'SAVE'
    return bytes(b)


def begin(mode=2):
    return ('send('+vector(owner(mode))+');take();send('+vector(load(mode))+');take();'
            'send('+vector(stat_request(mode=mode,maximum=0))+');take();next();'
            'send('+vector(open_request(mode,param()))+');take();')


class SfoWriterTests(unittest.TestCase):
    def test_fixed_independent_bytes_and_roundtrip(self):
        b=encode_sfo({'A':0x12345678,'B':'x'},capacities={'B':4})
        self.assertEqual(b.hex(), '00505346010100003400000038000000020000000000040404000000040000000000000002000402020000000400000004000000410042007856341278000000')
        self.assertEqual(parse_sfo(b),{'A':0x12345678,'B':'x'})
        binary=bytes(range(16))
        self.assertEqual(parse_sfo(encode_sfo({'B':binary}))['B'],{'hex':binary.hex()})
        for values, capacities in [({'X':-1},{}),({'X':True},{}),({'X':'bad\0'},{}),({'X':'Český'},{'X':4}),({'X':1},{'X':8})]:
            with self.assertRaises(AnyPS3Error):encode_sfo(values,capacities=capacities)


class SaveFullTests(unittest.TestCase):
    def run_native(self, body, **kw):
        return backend.SaveDataVshTests().run_native(body, **kw)

    def test_all_filetypes_persist_restart_read_and_delete(self):
        code=begin()
        names=['SECURE.DAT','NORMAL.DAT','ICON0.PNG','ICON1.PAM','PIC1.PNG','SND0.AT3']
        for typ,name in enumerate(names):
            code+='send('+vector(file_request('writeData',1,name,typ,0,3))+');take();send(B{0,128,255});check(take(),"resWriteData",0);'
        code+='send('+vector(abort())+');take();'
        code+=r'''const auto dir=std::filesystem::path(hdd)/"home/00000001/savedata/BCES00511_SLOT1";
 assert(std::filesystem::exists(dir/"PARAM.SFO"));
 auto meta=savedata_vsh::Sfo(savedata_vsh::read(dir/"PARAM.SFO"));assert(meta.integer("*SECURE.DAT")==1);
 assert(meta.values.at("ANYPS3_SECURE_SECURE.DAT").bytes==B(16,0));
 assert(meta.values.at("PARAMS").capacity==1024 && meta.values.at("PARAMS2").capacity==12);
 assert(!std::filesystem::exists(dir/"PARAM.PFD"));
 // Fresh backend state, same filesystem (no process-local savedata cache).
 s.settings.savedata=NativeSavedata{};
'''
        code+='send('+vector(owner(3))+');take();send('+vector(load(3))+');take();send('+vector(stat_request(maximum=6))+');take();'
        code+='auto rows=next();assert(rows.size()==336 && be(rows,0)==2);next();'
        code+='send('+vector(open_request(3))+');take();'
        for typ,name in enumerate(names):
            code+='send('+vector(file_request('readData',0,name,typ,0,3))+');check(take(),"resReadData",0);assert(next()==B({0,128,255}));'
        code+='send('+vector(abort())+');take();'+begin()
        for typ,name in enumerate(names):
            code+='send('+vector(file_request('deleteData',2,name,typ,0,0))+');check(take(),"resDeleteData",0);'
        code+='send('+vector(abort())+');take();'
        for name in names:code+=f'assert(!std::filesystem::exists(dir/"{name}"));'
        self.run_native(code)

    def test_secure_id_mismatch_leaves_committed_data_and_plaintext_binding_rejected(self):
        code=begin()+ 'send('+vector(file_request('writeData',1,'SECRET.DAT',0,0,3))+');take();send(B{1,2,3});take();send('+vector(abort())+');take();'
        code+='send('+vector(owner(3))+');take();send('+vector(load(3))+');take();send('+vector(stat_request(maximum=0))+');take();next();send('+vector(open_request(3))+');take();'
        wrong=bytearray(file_request('readData',0,'SECRET.DAT',0,0,3))
        # Mutate the secureFileId bytes in the original fixture's binary FileSet.
        binary=struct.unpack_from('>I',wrong,48)[0];wrong[binary+12]=0x80
        code+='fault([&]{savedata_vsh::request(s,'+vector(wrong)+');});assert(d.replies.empty());send('+vector(abort())+');take();'
        code+=r'''const auto p=savedata_vsh::path(s,"BCES00511_SLOT1","PARAM.PFD");
 savedata_vsh::write_file(p,B{1});fault([&]{savedata_vsh::metadata(s,"BCES00511_SLOT1");});'''
        self.run_native(code)

    def test_get_list_item_exact_layout_nodata_and_user_isolation(self):
        code=begin()+'send('+vector(abort())+');take();'
        item=document('getListItem',elmUserId=0,elmDirName='BCES00511_SLOT1')
        code+='send('+vector(item)+');auto response=take();check(response,"resGetListItem",0);'
        code+=r'''savedata_vsh::Document item(response);auto data=item.blob("elmListItem",1616);
 assert(str(data,24)=="BCES00511_SLOT1" && str(data,56)=="Ratchet" && str(data,184)=="Slot");
 assert(be(data,1608)==0 && be(data,1612)==35);
'''
        missing=document('getListItem',elmUserId=0,elmDirName='ABSENT')
        code+='send('+vector(missing)+');check(take(),"resGetListItem",0x80029262);'
        code+='send('+vector(owner(3))+');take();send('+vector(load(3))+');take();'
        exact=document('getDirList',elmUserId=2,elmSearchType=1,elmSortType=0,elmSortOrder=0,elmDirNamePlefix='BCES00511_SLOT1',elmDirListNumMax=1)
        code+='send('+vector(exact)+');auto empty=take();check(empty,"resGetDirList",0x8002926a);send('+vector(abort())+');take();'
        self.run_native(code)

    def test_prefixes_exact_lookup_and_zero_length_raw_file_list(self):
        code=begin()+'send('+vector(abort())+');take();send('+vector(owner(3))+');take();send('+vector(load(3))+');take();'
        for prefix,search,num in [('ZZ|BCES00511',0,1),('BCES00511',1,0),('BCES00511_SLOT1',1,1)]:
            q=document('getDirList',elmUserId=0,elmSearchType=search,elmSortType=0,elmSortOrder=0,elmDirNamePlefix=prefix,elmDirListNumMax=1)
            code+='send('+vector(q)+f');auto h{search}{num}=take();assert(be(h{search}{num},116)=={num});'
            if num:code+='assert(next().size()==48);'
        code+='send('+vector(stat_request(maximum=1))+');take();'
        code+=r'''assert(service_sysutil_connect(s,cond));assert(word(base+16)==0 && word(base+20)==2 && word(base+24)==2);
 // An unread empty packet cannot be overwritten by the next CXML document.
 service_sysutil_connect(s,cond);assert(word(base+16)==0 && word(base+24)==2);
 put(base+20,0);put(base+24,0);auto stat=next();assert(be(stat,104)==1704);
'''
        code+='send('+vector(abort())+');take();'
        self.run_native(code)

    def test_genuine_io_errors_and_incomplete_raw_do_not_commit(self):
        code=begin()
        code+=r'''savedata_vsh::write_file(d.staging/"DATA.BIN",B{97,98,99});
 assert(savedata_vsh::io_result(std::system_error(ENOSPC,std::generic_category()))==0x80029202);
 assert(savedata_vsh::io_result(std::system_error(EACCES,std::generic_category()))==0x80029201);
'''
        code+='send('+vector(file_request('writeData',1,offset=0,size=4))+');take();'
        code+='fault([&]{savedata_vsh::raw_write(d,B{1,2},true);});assert(d.replies.empty());'
        code+='savedata_vsh::cleanup(d);assert(!std::filesystem::exists(savedata_vsh::path(s,"BCES00511_SLOT1")));'
        self.run_native(code)

    def test_firmware_list_shape_pad_delete_and_overlay_pixels(self):
        # NativePad samples actual SDL button mapping, no VSH input replacement.
        code=INPUT+begin()+'send('+vector(abort())+');take();'
        code+='send('+vector(owner(4))+');take();send('+vector(load(4))+');take();'
        row=b'BCES00511_SLOT1'.ljust(32,b'\0')+b'SAVE\0\0\0\0'+bytes(8)
        code+=r'''auto& u=s.settings.vsh_ui;s.events.queues.push_back({0x900,16,UINT64_C(0x8006010000000100),{}});'''
        code+='send('+vector(save_list(mode=4,fresh=False))+');take();send('+vector(row)+');'
        code+=r'''assert(u.open && u.choices.size()==1);auto panel=anyps3_vsh_overlay::raster(640,480);
 assert(!panel.pixels.empty() && panel.width==608 && panel.x==16);
 press(2);assert(!u.open);auto cancelled=next();check(cancelled,"resOpenSaveList",1);release();'''
        code+='send('+vector(abort())+');take();'
        delete=document('openDeleteList',elmUtilType=8,elmUtilOption=0,elmContainer=bytes.fromhex('ffffffff'),elmUserId=0)
        code+='send('+vector(delete)+');assert(u.open && d.replies.empty());'
        code+=r'''assert(std::filesystem::exists(savedata_vsh::path(s,"BCES00511_SLOT1")));
 s.settings.enter_button=0;press(2);assert(u.save_confirm);release();press(1u<<13);release();press(2);
 assert(!u.open && !std::filesystem::exists(savedata_vsh::path(s,"BCES00511_SLOT1")));
 auto deleted=next();check(deleted,"resOpenDeleteList",0);assert(anyps3_vsh_overlay::raster(640,480).pixels.empty());
 s.memory.map(0x10000,0x1000,6,nullptr,0);s.r[4]=0x10100;
 assert(u.pad_release_guard && vsh_ui_pad_intercept(s,4));assert(s.memory.load(0x10100,4)==8 && s.memory.load(0x10108,2)==0);
 release();assert(vsh_ui_pad_intercept(s,4) && !u.pad_release_guard);assert(!vsh_ui_pad_intercept(s,4));

'''
        self.run_native(code,with_pad=True)

    def test_indicator_raw_picture_precedes_acceptance_and_closes_on_abort(self):
        opn=document('openSaveData',elmUtilType=2,elmUserId=1,elmDirName='BCES00511_SLOT1',
                     elmDataStat=struct.pack('>3I',0x10000,0,0),elmProgress=7,elmSysFileParam=param(),
                     elmIndicator=struct.pack('>6I',3,0,0x10100,0x10200,3,0),elmMsg='Saving')
        code='send('+vector(owner(2))+');take();send('+vector(load(2))+');take();send('+vector(stat_request(mode=2,maximum=0))+');take();next();'
        code+='send('+vector(opn)+');assert(d.opened && d.replies.empty() && s.settings.vsh_ui.save_raw_expected==3);'
        code+='send(B{1,2,3});check(take(),"resOpenSaveData",0);assert(s.settings.vsh_ui.icon==B({1,2,3}));'
        code+='send('+vector(abort())+');check(take(),"resAbort",0);assert(!s.settings.vsh_ui.open);'
        self.run_native(code)

    def test_corrupt_sfo_is_broken_and_recreate_discards_old_content(self):
        code=begin()+'send('+vector(file_request('writeData',1,offset=0,size=3))+');take();send(B{1,2,3});take();send('+vector(abort())+');take();'
        code+='send('+vector(owner(2))+');take();send('+vector(load(2))+');take();send('+vector(stat_request(mode=2,maximum=0))+');take();next();'
        code+='send('+vector(open_request(2,param(),recreate=2))+');take();assert(!std::filesystem::exists(d.staging/"DATA.BIN"));send('+vector(abort())+');take();'
        code+='savedata_vsh::write_file(savedata_vsh::path(s,"BCES00511_SLOT1","PARAM.SFO"),B(20,0));'
        code+='send('+vector(document('getListItem',elmUserId=0,elmDirName='BCES00511_SLOT1'))+');check(take(),"resGetListItem",0x80029203);'
        self.run_native(code)

    def test_real_process_restart_and_crash_before_commit_preserve_previous_save(self):
        create=begin()+'send('+vector(file_request('writeData',1,offset=0,size=3))+');take();send(B{65,66,67});take();send('+vector(abort())+');take();'
        update=begin()+'send('+vector(file_request('writeData',1,offset=0,size=3))+');take();send(B{88,89,90});take();::_exit(0);'
        read='send('+vector(owner(3))+');take();send('+vector(load(3))+');take();send('+vector(stat_request(maximum=0))+');take();next();send('+vector(open_request(3))+');take();'
        read+='send('+vector(file_request('readData',0,offset=0,size=3))+');take();assert(next()==B({65,66,67}));send('+vector(abort())+');take();'
        read+='auto meta=savedata_vsh::metadata(s,"BCES00511_SLOT1");assert(meta.string("TITLE",128)=="Ratchet");'
        self.run_native('if(std::string(std::getenv("ANYPS3_TEST_SAVE_PHASE"))=="0"){'+create+update+'}else{'+read+'}',restart=True)

    def test_timestamps_use_seconds_in_big_endian_stat_structures(self):
        code=begin()+'send('+vector(abort())+');take();'
        code+=r'const auto dir=savedata_vsh::path(s,"BCES00511_SLOT1");timespec t[2]={{0x12345678,0},{0x23456789,0}};assert(::utimensat(AT_FDCWD,dir.c_str(),t,0)==0);'
        code+='send('+vector(document('getListItem',elmUserId=0,elmDirName='BCES00511_SLOT1'))+');auto b=take();savedata_vsh::Document q(b);auto data=q.blob("elmListItem",1616);'
        code+='assert(be(data,0)==0 && be(data,4)==0x12345678 && be(data,8)==0 && be(data,12)==0x23456789);'
        self.run_native(code)

    def test_game_sized_newdata_icon_chunks_wake_on_space_condition(self):
        # ACIT NEW GAME (run 340dump): openSaveList with a 105015-byte PNG icon. libsysutil PacketWrite
        # fills the 0x7c00 ring and waits for space on channel 1's second condition 0x86000500
        # (THREAD id=27 pc=0x201142e8 lr=0x20113630); a non-system condition must not touch the ring.
        size=105015
        attrs=dict(elmUtilType=4,elmUtilOption=2,elmUserId=0,elmContainer=bytes.fromhex('ffffffff'),
                   elmDataListSet=struct.pack('>6I',5,0,0,0,0x10200,0),
                   elmNewData=struct.pack('>4I',0,0x10100,0x10300,0),elmDirName='BCES00511_SAVE_1',
                   elmNewDataIcon=struct.pack('>4I',0x10400,size,0x10500,0),elmNewDataIconTitleName='Create a new saved game.')
        code=INPUT+'auto& u=s.settings.vsh_ui;s.events.queues.push_back({0x900,16,UINT64_C(0x8006010000000100),{}});'
        code+='send('+vector(owner(4,2))+');take();send('+vector(load(4))+');take();'
        code+='send('+vector(document('openSaveList',**attrs))+');check(take(),"resOpenSaveList",0);'
        code+=f'assert(u.save_raw_expected=={size} && u.raw_phase==2);B icon({size});'
        code+=r'''
 for(unsigned i=0;i<icon.size();++i)icon[i]=static_cast<std::uint8_t>(i*13+7);
 unsigned at=0,pos=0,chunks=0;
 while(pos<icon.size()){
   const unsigned n=std::min<unsigned>(cap,icon.size()-pos);const bool last=pos+n==icon.size();
   assert(word(base+16)==0);put(base+12,at);put(base+8,(at+n)%cap);put(base+16,n);put(base+20,last?2:1);put(base+24,1);
   for(unsigned i=0;i<n;++i)raw[base+1024+(at+i)%cap]=icon[pos+i];
   assert(!service_sysutil_connect(s,0x86000900u) && word(base+16)==n); // not a sysutil condition
   assert(service_sysutil_connect(s,0x86000500u));                      // PacketWrite space wait
   assert(word(base+16)==0 && word(base+20)==0 && word(base+24)==0);
   at=(at+n)%cap;pos+=n;++chunks;
 }
 assert(chunks==4 && u.save_raw_expected==0 && u.choices.size()==1 && u.choices[0].fresh);
 assert(u.choices[0].picture==icon && u.choices[0].title=="Create a new saved game.");
 press(1);check(next(),"resOpenSaveList",0);release();send('''+vector(abort())+r''');take();
'''
        self.run_native(code,with_pad=True)

    def test_newdata_icon_packet_stick_and_autorepeat(self):
        attrs=dict(elmUtilType=4,elmUtilOption=0,elmUserId=0,elmContainer=bytes.fromhex('ffffffff'),
                   elmDataListSet=struct.pack('>6I',1,0,0,0,0x10200,0),
                   elmNewData=struct.pack('>4I',1,0x10100,0x10300,0),elmDirName='NEW_SLOT',
                   elmNewDataIcon=struct.pack('>4I',0x10400,3,0x10500,0),elmNewDataIconTitleName='NEW SAVE')
        code=INPUT+'auto& u=s.settings.vsh_ui;s.events.queues.push_back({0x900,16,UINT64_C(0x8006010000000100),{}});'
        code+='send('+vector(owner(4))+');take();send('+vector(load(4))+');take();send('+vector(document('openSaveList',**attrs))+');check(take(),"resOpenSaveList",0);'
        code+='assert(u.save_raw_expected==3 && u.raw_phase==2);send(B{1,2,3});assert(u.choices.size()==1 && u.choices[0].picture==B({1,2,3}) && u.choices[0].title=="NEW SAVE");'
        code+='press(1);check(next(),"resOpenSaveList",0);release();send('+vector(abort())+');take();'
        code+=r'''
 u.kind=NativeVshUi::SaveList;u.open=true;u.save_confirm=false;u.cursor=0;u.save_raw_expected=0;
 u.choices.clear();u.choices.push_back({"ONE","ONE",0,false});u.choices.push_back({"TWO","TWO",0,false});u.choices.push_back({"THREE","THREE",0,false});
 press(1u<<12);assert(u.cursor==1);vsh_ui_poll(s);assert(u.cursor==1);
 u.repeat_deadlines[0]=std::chrono::steady_clock::time_point{};vsh_ui_poll(s);assert(u.cursor==2);release();
 p.axis=+[](void*,int axis)->std::int16_t{return axis==1?-32768:0;};vsh_ui_poll(s);assert(u.cursor==1);
 u.open=false;vsh_ui::overlay(u);
'''
        self.run_native(code,with_pad=True)

    def test_original_common_abort_then_type14_unload_sequence(self):
        unload=document('unloadModules',elmUtilType=14)
        code=begin()+'send('+vector(abort())+');take();assert(d.owner==0 && d.retired_owner==s.thread_id);'
        code+='send('+vector(unload)+');check(take(),"resUnloadModules",0);assert(!d.retired_owner);'
        code+='fault([&]{savedata_vsh::request(s,'+vector(unload)+');});'
        code+='send('+vector(owner(3))+');take();send('+vector(load(3))+');take();send('+vector(abort(api=0x8002b401,cb=-4))+');take();'
        code+='send('+vector(unload)+');check(take(),"resUnloadModules",0);'
        self.run_native(code)

    def test_missing_search_rejected_for_dirlist_but_original_list_shape_accepted(self):
        q=document('getDirList',elmUserId=0,elmSortType=0,elmSortOrder=0,elmDirNamePlefix='',elmDirListNumMax=1)
        self.run_native('send('+vector(owner(2))+');take();send('+vector(load(2))+');take();fault([&]{savedata_vsh::request(s,'+vector(q)+');});send('+vector(abort())+');take();')


if __name__=='__main__':unittest.main()
