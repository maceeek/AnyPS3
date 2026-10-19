"""Derived firmware 3.01 SAVE requests and independent BE/SFO expectations.

Sources: libsysutil 0x2011ce74..d16c (list), d38c..d534 (stat),
e9cc..ec98 (write handshake), VSH 0x133974/1365e8/136b60 (replies).
These are protocol/native backend tests, not evidence of ACIT callbacks/menu.
"""
from pathlib import Path
import shutil
import os
import struct
import subprocess
import tempfile
import unittest

from anyps3.sfo import parse_sfo


def document(name, **attrs):
    """Fixture transcription of cxml 1.10: one root, no struct children."""
    strings = bytearray(name.encode() + b'\0')
    records, blobs = [], bytearray()
    for key, value in attrs.items():
        key_at = len(strings)
        strings += key.encode() + b'\0'
        if isinstance(value, int):
            records += [(key_at, 1, value & 0xffffffff, 0)]
        elif isinstance(value, str):
            at = len(strings)
            strings += value.encode() + b'\0'
            records += [(key_at, 3, at, len(value.encode()))]
        else:
            records += [(key_at, 6, len(blobs), len(value))]
            blobs += value
            blobs += b'\0' * (-len(blobs) % 16)
    tree = struct.pack('>7I', 0, len(records), *([0xffffffff] * 5))
    tree += b''.join(struct.pack('>4I', *r) for r in records)
    strings_at = (64 + len(tree) + 15) // 16 * 16
    binary_at = (strings_at + len(strings) + 15) // 16 * 16
    out = bytearray(binary_at + len(blobs))
    out[:64] = struct.pack('>16I', 0x53415645, 0x110, 64, len(tree), strings_at, 0,
                           strings_at, len(strings), binary_at, 0, binary_at, 0,
                           binary_at, len(blobs), 0, 0)
    out[64:64 + len(tree)] = tree
    out[strings_at:strings_at + len(strings)] = strings
    out[binary_at:] = blobs
    return bytes(out)


def vector(b):
    return 'B{' + ','.join(str(x) for x in b) + '}'


def fixture_sfo(directory='BCES00511_SLOT1', subtitle='Slot one', list_param='SLOT1'):
    """Independent little-endian PARAM.SFO fixture, no runtime SFO encoder."""
    values = {'TITLE': 'Ratchet test', 'SUB_TITLE': subtitle, 'DETAIL': 'Existing data',
              'SAVEDATA_DIRECTORY': directory, 'SAVEDATA_LIST_PARAM': list_param,
              'ATTRIBUTE': 0x12345678, 'PARENTAL_LEVEL': 7}
    keys, data, entries = bytearray(), bytearray(), []
    for key, value in values.items():
        key_at = len(keys)
        keys += key.encode() + b'\0'
        if isinstance(value, int):
            fmt, encoded = 0x404, struct.pack('<I', value)
        else:
            fmt, encoded = 0x204, value.encode() + b'\0'
        entries += [struct.pack('<HHIII', key_at, fmt, len(encoded), len(encoded), len(data))]
        data += encoded
        data += b'\0' * (-len(data) % 4)
    key_at = 20 + len(entries) * 16
    value_at = (key_at + len(keys) + 3) // 4 * 4
    result = struct.pack('<5I', 0x46535000, 0x101, key_at, value_at, len(entries))
    result += b''.join(entries) + keys
    result += b'\0' * (value_at - len(result)) + data
    assert parse_sfo(result)['ATTRIBUTE'] == 0x12345678
    return result


HARNESS = r'''
 using B=std::vector<std::uint8_t>;
 char hdd[]="/tmp/anyps3-save-test-XXXXXX";assert(::mkdtemp(hdd));s.fs.configure_hdd(hdd);
 auto& raw=*s.mmapper.objects.front().bytes;auto& d=s.settings.savedata;
 const unsigned base=0x8000,cap=0x7c00;const auto cond=0x86000400u;
 auto word=[&](unsigned a){return(unsigned(raw[a])<<24)|(unsigned(raw[a+1])<<16)|(unsigned(raw[a+2])<<8)|raw[a+3];};
 auto put=[&](unsigned a,unsigned v){for(unsigned j=0;j<4;++j)raw[a+j]=v>>(24-8*j);};
 auto be=[](const B& b,unsigned a){assert(a+4<=b.size());return(unsigned(b[a])<<24)|(unsigned(b[a+1])<<16)|(unsigned(b[a+2])<<8)|b[a+3];};
 auto str=[](const B& b,unsigned a){assert(a<b.size());return std::string(reinterpret_cast<const char*>(b.data()+a));};
 auto send=[&](const B& b,unsigned at=0,unsigned end=2){
   assert(word(base+16)==0);assert(b.size()<=cap);put(base,1024);put(base+4,cap);put(base+12,at);put(base+8,(at+b.size())%cap);
   put(base+16,b.size());put(base+20,end);put(base+24,1);
   for(unsigned i=0;i<b.size();++i)raw[base+1024+(at+i)%cap]=b[i];
   assert(service_sysutil_connect(s,cond));
 };
 auto take=[&]{
   B b;for(;;){assert(word(base+24)==2 && word(base+16)>0);const auto n=word(base+16),at=word(base+12),end=word(base+20);
     for(unsigned i=0;i<n;++i)b.push_back(raw[base+1024+(at+i)%cap]);
     put(base+12,(at+n)%cap);put(base+16,0);
     if(end==2){put(base+20,0);put(base+24,0);return b;}
     assert(end==1);assert(service_sysutil_connect(s,cond));}
 };
 auto next=[&]{assert(service_sysutil_connect(s,cond));return take();};
 auto check=[&](const B& b,const char* root,unsigned result){assert(be(b,0)==0x53415645 && be(b,4)==0x110 && be(b,8)==64);
   assert(str(b,be(b,24)+be(b,64))==root);assert(str(b,be(b,24)+be(b,92))=="elmResult");assert(be(b,96)==1 && be(b,100)==result);};
 (void)next;(void)check;put(base,1024);put(base+4,cap);
 s.sysutil_connections.insert(UINT64_C(1)<<32);
'''


def owner(mode=3, options=0):
    return document('getOwnerShip', elmUtilType=mode, elmUtilOption=options)


def load(mode=3):
    return document('loadModules', elmUtilType=mode, elmContainer=bytes.fromhex('ffffffff'))


def listing(maximum=10, user=0, sort=1, order=1):
    return document('getDirList', elmUserId=user, elmSearchType=0, elmSortType=sort,
                    elmSortOrder=order, elmDirNamePlefix='BCES00511', elmDirListNumMax=maximum)


def stat_request(directory='BCES00511_SLOT1', mode=3, maximum=10):
    return document('getSaveDataStat', elmUtilType=mode, elmUtilOption=0,
                    elmUserId=0, elmDirName=directory, elmFileListNumMax=maximum)


def abort(api=0, cb=1, options=0):
    b = struct.pack('>6I', options, 0, api, cb & 0xffffffff, 0, 0) + bytes(40)
    return document('abort', elmAbortParam=b)


def open_request(mode=3, param=None, recreate=0):
    attrs = dict(elmUtilType=mode, elmUserId=1, elmDirName='BCES00511_SLOT1',
                 elmDataStat=struct.pack('>3I', 0x10000 if param else 0, recreate, 0), elmProgress=0)
    if param is not None:
        attrs['elmSysFileParam'] = param
    return document('openSaveData', **attrs)


def file_request(command='readData', op=0, name='DATA.BIN', filetype=1, offset=2, size=3):
    b = struct.pack('>3I', op, 0, filetype) + bytes(16)
    b += struct.pack('>5I', 0x10100, offset, size, size, 0x10200)
    assert len(b) == 48
    return document(command, elmDataFileSet=b, elmFileType=filetype, elmFileName=name, elmProgress=0)


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++')
class SaveDataVshTests(unittest.TestCase):
    def run_native(self, body, *, with_pad=False, restart=False):
        # Isolate the owned service from other batch owners' in-flight syscall
        # implementations. Production GuestMemory, filesystem, IPC and the full
        # sysutil transport are compiled unchanged; no SAVE code is mocked.
        from anyps3.runtime import MEMORY_CPP
        from anyps3.native_fs import TYPES as FS_TYPES
        from anyps3.mmapper import MMAPPER_TYPES
        from anyps3.events import TYPES as EVENT_TYPES
        from anyps3.sysutil_transport import TYPES, CPP
        from anyps3.native_pad import TYPES as PAD_TYPES, CPP as PAD_CPP
        source = '\n'.join([
            '#include <cstdint>\n#include <cstdio>\n#include <cinttypes>\n#include <set>\n'
            '#include <array>\n#include <cassert>\n#include <iterator>',
            MEMORY_CPP, MMAPPER_TYPES, EVENT_TYPES, TYPES, FS_TYPES, PAD_TYPES if with_pad else "",
            'struct PpuState { GuestMemory memory; NativeFilesystem fs; Mmapper mmapper; Events events; '
            'NativeSystemSettings settings; NativeVideo video; NativeTrophy trophy; ' +
            ('NativePad pad;std::array<std::uint64_t,32> r{};unsigned sdk_version=0x3010000;' if with_pad else '') +
            'std::set<std::uint64_t> sysutil_connections; std::uint64_t thread_id=1; '
            'struct {bool vsync=false;std::array<unsigned,2> flip_modes{};} rsx; };',
            CPP, PAD_CPP if with_pad else "",
            'template<class F> void fault(F f){bool hit=false;try{f();}catch(const std::runtime_error&){hit=true;}assert(hit);}',
            'int main(){PpuState s;',
            HARNESS.replace('char hdd[]="/tmp/anyps3-save-test-XXXXXX";assert(::mkdtemp(hdd));',
                            'const char* hdd=std::getenv("ANYPS3_TEST_SAVE_ROOT");assert(hdd);') if restart else HARNESS, body,
            'assert(d.replies.empty());' + ('' if restart else 'std::filesystem::remove_all(hdd);') + '}', 
        ])
        with tempfile.TemporaryDirectory() as temp:
            source_path = Path(temp) / 'save.cpp'
            executable = Path(temp) / 'save'
            source_path.write_text(source)
            build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    '-Wno-unused-function', str(source_path), '-o', str(executable)],
                                   capture_output=True, text=True, timeout=60)
            self.assertEqual(build.returncode, 0, build.stderr)
            output=[]
            for phase in range(2 if restart else 1):
                env=dict(os.environ,ANYPS3_TEST_SAVE_ROOT=str(Path(temp)/'hdd'),ANYPS3_TEST_SAVE_PHASE=str(phase))
                if restart:(Path(temp)/'hdd').mkdir(exist_ok=True)
                run = subprocess.run([str(executable)], env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
                output.append(run.stdout)
            return ''.join(output)

    def test_first_run_empty_list_new_stat_and_both_callback_cleanup_branches(self):
        expected = document('resGetDirList', elmResult=0, elmDirNum=0, elmListedDirNum=0)
        self.run_native(
            'send(' + vector(owner()) + ');check(take(),"resGetOwnerShip",0);'
            'send(' + vector(load()) + ');check(take(),"resLoadModules",0);'
            'send(' + vector(listing()) + ',cap-40);auto list=take();assert(list==' + vector(expected) + ');'
            r'''assert(d.replies.empty());assert(!std::filesystem::exists(std::filesystem::path(hdd)/"home"));
'''
            'send(' + vector(stat_request()) + ');auto first=take();check(first,"resGetSaveDataStat",0);'
            r'''assert(be(first,68)==2 && be(first,112)==1 && be(first,116)==1);
 auto second=next();assert(be(second,68)==1 && be(second,96)==6 && be(second,104)==1704);
 const auto data=be(second,48)+be(second,100);
 assert(be(second,data)>0 && be(second,data+4)==1);
 assert(str(second,data+32)=="BCES00511_SLOT1");
 for(unsigned i=8;i<32;++i)assert(second[data+i]==0);
 assert(be(second,data+1624)==35);
 for(unsigned i=64;i<1704;++i)if(i<1624 || i>=1628)assert(second[data+i]==0);
 assert(!std::filesystem::exists(std::filesystem::path(hdd)/"home"));
'''
            'send(' + vector(abort(api=0x8002b401, cb=-4)) + ');check(take(),"resAbort",0);assert(d.owner==0);'
            'send(' + vector(owner()) + ');take();send(' + vector(load()) + ');take();'
            'send(' + vector(listing()) + ');take();send(' + vector(abort()) + ');check(take(),"resAbort",0);'
            r'''assert(!std::filesystem::exists(std::filesystem::path(hdd)/"home"));''')

    def test_real_sfo_lists_metadata_file_read_and_missing_file_error(self):
        self.run_native(r'''
 const auto dir=std::filesystem::path(hdd)/"home/00000001/savedata/BCES00511_SLOT1";
 std::filesystem::create_directories(dir);
 auto disk=[&](const char* name,const B& b){std::ofstream f(dir/name,std::ios::binary);f.write(reinterpret_cast<const char*>(b.data()),b.size());assert(f.good());};
''' + 'disk("PARAM.SFO",' + vector(fixture_sfo()) + ');disk("DATA.BIN",B{97,98,99,100,101,102});disk("ICON0.PNG",B{1,2,3,4});'
            'send(' + vector(owner()) + ');take();send(' + vector(load()) + ');take();'
            'send(' + vector(listing(maximum=1, user=1)) + ');auto list=take();check(list,"resGetDirList",0);'
            r'''assert(be(list,116)==1 && be(list,132)==1);auto rows=next();assert(rows.size()==48);
 assert(str(rows,0)=="BCES00511_SLOT1" && str(rows,32)=="SLOT1");
''' + 'send(' + vector(stat_request()) + ');auto header=take();check(header,"resGetSaveDataStat",0);assert(be(header,116)==0);'
            r'''auto fs=next();assert(fs.size()==112);assert(be(fs,0)==1 && be(fs,12)==6 && str(fs,40)=="DATA.BIN");
 assert(be(fs,56)==2 && be(fs,68)==4 && str(fs,96)=="ICON0.PNG");
 auto metadata=next();const auto data=be(metadata,48);assert(be(metadata,data+4)==0);
 assert(str(metadata,data+64)=="Ratchet test" && str(metadata,data+192)=="Slot one");
 assert(be(metadata,data+1344)==0x12345678 && be(metadata,data+1348)==7);
 assert(be(metadata,data+1620)==37 && be(metadata,data+1624)==35);
 assert(be(metadata,data+1628)==2 && be(metadata,data+1632)==2 && be(metadata,data+1636)==0);
''' + 'send(' + vector(open_request()) + ');check(take(),"resOpenSaveData",0);'
            'send(' + vector(file_request()) + ');auto rd=take();check(rd,"resReadData",0);assert(be(rd,116)==3);assert(next()==B({99,100,101}));'
            'send(' + vector(file_request(offset=6)) + ');auto eof=take();check(eof,"resReadData",0);assert(be(eof,116)==0 && d.replies.empty());'
            'send(' + vector(file_request(name='MISSING.BIN')) + ');check(take(),"resReadData",0x80029204);assert(d.replies.empty());'
            'send(' + vector(abort(api=0x8002b407, cb=-2)) + ');take();'
            r'''assert(std::filesystem::file_size(dir/"DATA.BIN")==6);''')

    def test_save_raw_handshake_truncate_notrunc_commit_and_rollback(self):
        param = bytearray(1552)
        param[:8] = b'Test PS3'
        param[128:132] = b'Slot'
        param[256:262] = b'Detail'
        param[1280:1288] = struct.pack('>2I', 0x12345678, 7)
        param[1288:1293] = b'SLOT1'
        begin = ('send(' + vector(owner(mode=2)) + ');take();send(' + vector(load(mode=2)) + ');take();'
                 'send(' + vector(stat_request(mode=2, maximum=0)) + ');take();next();'
                 'send(' + vector(open_request(mode=2, param=param)) + ');check(take(),"resOpenSaveData",0);')
        output = self.run_native(begin +
            'send(' + vector(file_request('writeData', 1, offset=0, size=6)) + ');auto accept=take();check(accept,"resWriteData",0);assert(be(accept,68)==1);'
            r'''assert(d.writing);send(B{97,98,99,100,101,102});auto done=take();check(done,"resWriteData",0);assert(be(done,116)==6);assert(!d.writing);
''' + 'send(' + vector(file_request('writeData', 3, offset=2, size=2)) + ');take();send(B{88,89});take();'
            'send(' + vector(abort()) + ');check(take(),"resAbort",0);'
            r'''const auto dir=std::filesystem::path(hdd)/"home/00000001/savedata/BCES00511_SLOT1";
 auto read_disk=[&](const char* name){std::ifstream f(dir/name,std::ios::binary);return B(std::istreambuf_iterator<char>(f),{});};
 assert(read_disk("DATA.BIN")==B({97,98,88,89,101,102}));
 auto sfo=read_disk("PARAM.SFO");assert(sfo[0]==0 && sfo[1]==0x50 && sfo[2]==0x53 && sfo[3]==0x46);
''' + begin +
            'send(' + vector(file_request('writeData', 1, offset=2, size=2)) + ');take();send(B{81,82});take();'
            'send(' + vector(abort(api=0x8002b401, cb=-2)) + ');take();assert(read_disk("DATA.BIN")==B({97,98,88,89,101,102}));'
            + begin +
            'send(' + vector(file_request('writeData', 1, offset=2, size=2)) + ');take();send(B{81,82});take();'
            'send(' + vector(abort()) + ');take();assert(read_disk("DATA.BIN")==B({97,98,81,82}));'
            r'''for(auto b:read_disk("PARAM.SFO"))std::printf("%02x",unsigned(b));std::printf("\n");''')
        sfo = parse_sfo(bytes.fromhex(output.strip()))
        self.assertEqual(sfo['TITLE'], 'Test PS3')
        self.assertEqual(sfo['ATTRIBUTE'], 0x12345678)
        self.assertEqual(sfo['PARENTAL_LEVEL'], 7)
        self.assertEqual(sfo['SAVEDATA_DIRECTORY'], 'BCES00511_SLOT1')
        self.assertEqual(sfo['SAVEDATA_LIST_PARAM'], 'SLOT1')

    def test_two_real_directories_are_sorted_before_limiting(self):
        self.run_native(r'''
 const auto parent=std::filesystem::path(hdd)/"home/00000001/savedata";
 auto create=[&](const char* name,const B& sfo,time_t when){const auto p=parent/name;std::filesystem::create_directories(p);
   {std::ofstream f(p/"PARAM.SFO",std::ios::binary);f.write(reinterpret_cast<const char*>(sfo.data()),sfo.size());assert(f.good());}
   timespec times[2]={{when,0},{when,0}};assert(::utimensat(AT_FDCWD,p.c_str(),times,0)==0);
 };
''' + 'create("BCES00511_SLOT1",' + vector(fixture_sfo(subtitle='Z last')) + ',100);'
            'create("BCES00511_SLOT2",' + vector(fixture_sfo(directory='BCES00511_SLOT2', subtitle='A first', list_param='SLOT2')) + ',200);'
            'send(' + vector(owner()) + ');take();send(' + vector(load()) + ');take();'
            'send(' + vector(listing(maximum=1, sort=1, order=1)) + ');auto h=take();assert(be(h,116)==2 && be(h,132)==1);'
            'auto rows=next();assert(rows.size()==48 && str(rows,0)=="BCES00511_SLOT2");'
            'send(' + vector(listing(maximum=1, sort=0, order=0)) + ');take();rows=next();assert(str(rows,0)=="BCES00511_SLOT2");'
            'send(' + vector(listing(maximum=0)) + ');h=take();assert(be(h,116)==2 && be(h,132)==0 && d.replies.empty());'
            'send(' + vector(abort()) + ');take();')

    def test_rejects_bad_types_paths_duplicates_secure_and_interactive_ui(self):
        self.run_native(
            'fault([&]{savedata_vsh::request(s,' + vector(document('openDeleteList', elmUtilType=8)) + ');});'
            'auto req=' + vector(owner()) + ';auto bad=req;bad[99]=3;'
            r'''fault([&]{savedata_vsh::request(s,bad);});assert(!d.owner && d.replies.empty());
 auto dup=req;for(unsigned j=0;j<4;++j)dup[108+j]=dup[92+j];
 fault([&]{savedata_vsh::request(s,dup);});assert(!d.owner);
'''
            'send(req);take();send(' + vector(load()) + ');take();'
            'fault([&]{savedata_vsh::request(s,' + vector(document('openSaveList')) + ');});'
            'fault([&]{savedata_vsh::request(s,' + vector(stat_request('../escape')) + ');});'
            r'''auto& owner=d.owner;auto id=owner;owner=99;fault([&]{savedata_vsh::request(s,req);});owner=id;
 std::filesystem::create_directories(std::filesystem::path(hdd)/"home/00000001");
 std::filesystem::create_directory_symlink("/tmp",std::filesystem::path(hdd)/"home/00000001/savedata");
'''
            'fault([&]{savedata_vsh::request(s,' + vector(stat_request()) + ');});'
            r'''std::filesystem::remove(std::filesystem::path(hdd)/"home/00000001/savedata");
 d.opened=true;d.directory="BCES00511_SLOT1";
'''
            # SECUREFILE is implemented (host plaintext + ID checks); covered by test_savedata_full.
            'fault([&]{savedata_vsh::request(s,' + vector(file_request(name='PARAM.SFO')) + ');});'
            'send(' + vector(abort()) + ');take();')

    def test_chunked_raw_write_and_large_reply_preserve_packet_end(self):
        # Transport fragmentation is independent of SaveData file semantics.
        self.run_native(r'''
 B payload(40000);for(unsigned i=0;i<payload.size();++i)payload[i]=i*7;
 d.replies.push_back({payload,0});assert(service_sysutil_connect(s,cond));assert(word(base+20)==1);
 assert(take()==payload && d.replies.empty());
 d.owner=s.thread_id;d.modules=true;d.writing=true;d.file_size=40000;d.file_offset=0;d.operation=1;d.file="DATA.BIN";
 d.staging=std::filesystem::path(hdd)/"staging";std::filesystem::create_directory(d.staging);
 fault([&]{savedata_vsh::raw_write(d,B{1},true);});assert(!std::filesystem::exists(d.staging/"DATA.BIN"));d.incoming.clear();
 send(B(payload.begin(),payload.begin()+cap),0,1);assert(word(base+16)==0 && d.writing && d.replies.empty());
 send(B(payload.begin()+cap,payload.end()));auto result=take();check(result,"resWriteData",0);assert(be(result,116)==40000);
 std::ifstream f(d.staging/"DATA.BIN",std::ios::binary);B disk(std::istreambuf_iterator<char>(f),{});assert(disk==payload);
 savedata_vsh::cleanup(d);
''')


if __name__ == '__main__':
    unittest.main()
