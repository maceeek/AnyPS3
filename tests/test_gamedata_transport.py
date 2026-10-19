"""GDAT 3.01 ring replies and persistent SFO, with independent wire expectations."""
from pathlib import Path
import shutil
import struct
import unittest
from tests import test_runtime_objects


def packet(command, attrs=()):
    names = bytearray(command.encode() + b'\0')
    leaves = bytearray()
    blob = bytearray()
    for name, kind, value in attrs:
        key = len(names)
        names += name.encode() + b'\0'
        if kind == 1:
            a, n = value, 0
        elif kind == 3:
            a, n = len(names), len(value)
            names += value.encode() + b'\0'
        elif kind == 6:
            a, n = len(blob), len(value)
            blob += value
        leaves += struct.pack('>4I', key, kind, a, n)
    tree = struct.pack('>7I', 0, len(attrs), *([0xffffffff] * 5)) + leaves
    strings = (64 + len(tree) + 15) & ~15
    binary = (strings + len(names) + 15) & ~15
    data = bytearray(binary + ((len(blob) + 15) & ~15))
    struct.pack_into('>16I', data, 0, 0x47444154, 0x110, 64, len(tree), strings, 0,
                     strings, len(names), binary, 0, binary, 0, binary, len(blob), 0, 0)
    data[64:64+len(tree)] = tree
    data[strings:strings+len(names)] = names
    data[binary:binary+len(blob)] = blob
    return bytes(data)


def cpp(data):
    return '{' + ','.join(str(x) for x in data) + '}'


# Original VSH CXML constructor / SetInt / writer, not this module's encoder.
# Reproduce with local/ratchet/codex/gamedata/reply_oracle.py.
UPDATE_REPLY = bytes.fromhex(
    '4744415400000110000000400000002c00000070000000000000007000000019'
    '0000009000000000000000900000000000000090000000000000000000000000'
    '0000000000000001ffffffffffffffffffffffffffffffffffffffff0000000f'
    '0000000100000000000000000000000075706461746547616d65446174610065'
    '6c6d526573756c740000000000000000'
)


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class GameDataTransportTests(unittest.TestCase):
    def test_install_reload_merge_and_exact_reply(self):
        params = bytearray(2980)
        params[:10] = b'Test game\0'
        params[128:136] = b'English\0'
        params[128+19*128:128+19*128+8] = b'Ignored\0'
        params[0xa80:0xa8a] = b'BCES00511\0'
        params[0xa8c:0xa92] = b'01.00\0'
        struct.pack_into('>4I', params, 0xa94, 0x87654321, 7, 0x19, 3)
        req = packet('updateGameData', [('elmDirName',3,'BCES00511'),
            ('elmCategoryType',1,0), ('elmSysParam',6,params)])
        stat = packet('getGameDataStat', [('elmErrDialog',1,0),
            ('elmDirName',3,'BCES00511'), ('elmCategoryType',1,0)])
        hgstat = packet('getGameDataStat', [('elmErrDialog',1,0),
            ('elmDirName',3,'BCES00511'), ('elmCategoryType',1,1)])
        hgreq = packet('updateGameData', [('elmDirName',3,'BCES00511'),
            ('elmCategoryType',1,1), ('elmSysParam',6,params)])
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char parent[]="/tmp/anyps3-gdat-install-XXXXXX";assert(::mkdtemp(parent));
 s.fs.configure_hdd(parent);s.fs.game_data.container=1;s.fs.game_data.no_calc=true;
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 using B=std::vector<std::uint8_t>;
 auto& ring=*s.mmapper.objects.front().bytes;
 auto putring=[&](unsigned a,unsigned v){for(unsigned j=0;j<4;++j)ring[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(ring[a])<<24)|(std::uint32_t(ring[a+1])<<16)|(std::uint32_t(ring[a+2])<<8)|ring[a+3];};
 auto send=[&](const B& packet,unsigned start=0x7bf0){
   putring(0x8000,1024);putring(0x8004,0x7c00);putring(0x800c,start);putring(0x8010,packet.size());putring(0x8014,2);putring(0x8018,1);
   for(unsigned i=0;i<packet.size();++i)ring[0x8400+(start+i)%0x7c00]=packet[i];
   assert(service_sysutil_connect(s,0x86000400));B reply(word(0x8010));
   for(unsigned i=0;i<reply.size();++i)reply[i]=ring[0x8400+(word(0x800c)+i)%0x7c00];
   return reply;
 };
 const B req='''+cpp(req)+r''',statreq='''+cpp(stat)+r''',expected='''+cpp(UPDATE_REPLY)+r''';
 auto b=send(statreq);auto be=[&](const B& b,unsigned a){return(std::uint32_t(b[a])<<24)|(std::uint32_t(b[a+1])<<16)|(std::uint32_t(b[a+2])<<8)|b[a+3];};
 assert(be(b,100)==0 && be(b,120)==0x1450);auto binary=be(b,48);
 assert(be(b,binary+4)==1 && be(b,binary+0x1404)==0 && be(b,binary+0x1408)==3);
 assert(!std::filesystem::exists(std::filesystem::path(parent)/"game"));
 assert(send(req)==expected);
 const auto path=std::filesystem::path(parent)/"game/BCES00511";
 assert(std::filesystem::is_directory(path/"USRDIR"));
 struct ::stat hostinfo{};assert(!::stat((path/"PARAM.SFO").c_str(),&hostinfo) && (hostinfo.st_mode&0777)==0666);
 assert(!::stat((path/"USRDIR").c_str(),&hostinfo) && (hostinfo.st_mode&0777)==0777);
 auto read=[](const std::filesystem::path& p){std::ifstream f(p,std::ios::binary);return B(std::istreambuf_iterator<char>(f),{});};
 auto raw=read(path/"PARAM.SFO");
 assert(raw.size()>2000 && raw[0]==0 && raw[1]==0x50 && raw[2]==0x53 && raw[3]==0x46);
 auto le=[&](unsigned a,unsigned n=4){unsigned v=0;for(unsigned j=0;j<n;++j)v|=unsigned(raw[a+j])<<(8*j);return v;};
 // Independent file decoding: fixed 21 keys, field capacities and LE scalar.
 assert(le(16)==21);bool parental=false,title=false,category=false;
 for(unsigned i=0;i<21;++i){const unsigned at=20+16*i;const auto key=le(8)+le(at,2),value=le(12)+le(at+12);
   const std::string name(reinterpret_cast<const char*>(raw.data()+key));
   assert(name!="ATTRIBUTE" && name!="BOOTABLE" && name!="TITLE_16" && name!="TITLE_19" && name!="PS3_SYSTEM_VER");
   if(name=="PARENTAL_LEVEL"){parental=true;assert(le(at+2,2)==0x404 && le(at+4)==4 && le(value)==7);}
   if(name=="TITLE_ID"){title=true;assert(le(at+8)==16 && std::string(reinterpret_cast<const char*>(raw.data()+value))=="BCES00511");}
   if(name=="CATEGORY"){category=true;assert(le(at+8)==4 && std::string(reinterpret_cast<const char*>(raw.data()+value))=="GD");}
   if(name=="VERSION")assert(le(at+8)==8);
 }
 assert(parental && title && category);
 // Fresh filesystem/session objects, same disk: no in-memory metadata shortcut.
 s.fs=NativeFilesystem{};s.fs.configure_hdd(parent);s.fs.game_data.container=2;s.fs.game_data.no_calc=true;
 const struct timespec times[2]={{1700000000,0},{1700000123,0}};assert(!::utimensat(AT_FDCWD,path.c_str(),times,0));
 b=send(statreq);binary=be(b,48);
 assert(be(b,binary+0x848)==0 && be(b,binary+0x84c)==1700000000 && be(b,binary+0x850)==0 && be(b,binary+0x854)==1700000123);
 assert(be(b,100)==0 && be(b,binary+4)==0 && be(b,binary+0x860+0xa94)==0 && be(b,binary+0x860+0xa98)==7);
 assert(std::string(reinterpret_cast<const char*>(b.data()+binary+0x860))=="Test game");
 // Preserve unknown metadata and real installed files on merge/repeated install.
 auto f=savedata_vsh::Sfo(raw);f.string("CUSTOM","kept",16);f.string("TITLE_19","legacy",128);
 auto with_extra=f.encode();{std::ofstream out(path/"PARAM.SFO",std::ios::binary);out.write(reinterpret_cast<const char*>(with_extra.data()),with_extra.size());}
 {std::ofstream out(path/"USRDIR/asset.bin");out<<"real installed asset";}
 assert(send(req)==expected);auto after=savedata_vsh::Sfo(read(path/"PARAM.SFO"));
 assert(after.string("CUSTOM",16)=="kept" && after.string("TITLE_19",128)=="legacy");
 assert(std::filesystem::file_size(path/"USRDIR/asset.bin")==20);
 b=send(statreq);assert(std::string(reinterpret_cast<const char*>(b.data()+be(b,48)+0x860+128+19*128))=="legacy");
 // Category 1 produces HG, BOOTABLE and display/sound fields.
 const B hgreq='''+cpp(hgreq)+r''',hgstat='''+cpp(hgstat)+r''';
 assert(send(hgreq)==expected);b=send(hgstat);binary=be(b,48);assert(be(b,100)==0 && be(b,binary+0x1408)==4);
 assert(be(b,binary+0x860+0xa9c)==0x19 && be(b,binary+0x860+0xaa0)==3);
 after=savedata_vsh::Sfo(read(path/"PARAM.SFO"));assert(after.string("CATEGORY",4)=="HG" && after.integer("BOOTABLE")==1);
 // Incompatible SFO category: truthful BROKEN, not a successful new install.
 b=send(statreq);assert(be(b,100)==0x8002b606);
 std::filesystem::remove_all(parent);
''')

    def test_malformed_requests_and_filesystem_errors_do_not_report_success(self):
        params = bytearray(2980)
        params[:5] = b'Game\0'
        params[0xa80:0xa8a] = b'TEST00001\0'
        params[0xa8c:0xa92] = b'01.00\0'
        valid = packet('updateGameData', [('elmDirName',3,'TEST00001'),
            ('elmCategoryType',1,0), ('elmSysParam',6,params)])
        invalid = [valid[:60],
            packet('updateGameData',[('elmDirName',3,'TEST00001'),('elmCategoryType',1,0),('elmSysParam',6,params[:-1])]),
            packet('updateGameData',[('elmDirName',3,'TEST00001'),('elmCategoryType',1,2),('elmSysParam',6,params)]),
            packet('updateGameData',[('elmDirName',3,'TEST00001'),('elmCategoryType',3,'0'),('elmSysParam',6,params)]),
            packet('updateGameData',[('elmDirName',3,'TEST00001'),('elmCategoryType',1,0),('elmSysParam',6,params),('extra',1,0)]),
            packet('updateGameData',[('elmDirName',3,'TEST00001'),('elmDirName',3,'TEST00001'),('elmCategoryType',1,0),('elmSysParam',6,params)]),
            packet('getGameDataStat',[('elmErrDialog',1,1),('elmDirName',3,'TEST00001'),('elmCategoryType',1,0)])]
        bad = bytearray(valid);struct.pack_into('>I',bad,84,0);invalid.append(bad)
        bad = bytearray(valid);struct.pack_into('>I',bad,116,0xffffffff);invalid.append(bad)
        bad = bytearray(valid);struct.pack_into('>I',bad,132,0xffffffff);invalid.append(bad)
        for cmd in ['unknown','showDialog','endDialog','ps1End','vmcAssign','discInfo','setEjectMode','cacheInit','cacheClear']:
            invalid.append(packet(cmd))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char parent[]="/tmp/anyps3-gdat-errors-XXXXXX";assert(::mkdtemp(parent));
 s.fs.configure_hdd(parent);s.fs.game_data.container=1;s.fs.game_data.no_calc=true;s.fs.game_data.directory="TEST00001";
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 using B=std::vector<std::uint8_t>;const B valid='''+cpp(valid)+r''';
 auto& ring=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,unsigned v){for(unsigned j=0;j<4;++j)ring[a+j]=v>>(24-8*j);};
 auto load=[&](const B& b){std::fill(ring.begin(),ring.end(),0);put(0x8000,1024);put(0x8004,0x7c00);put(0x8010,b.size());put(0x8014,2);put(0x8018,1);std::copy(b.begin(),b.end(),ring.begin()+0x8400);};
 const std::vector<B> bad={'''+','.join(cpp(x) for x in invalid)+r'''};
 for(const auto& b:bad){load(b);const auto before=ring;fault([&]{service_sysutil_connect(s,0x86000400);});assert(ring==before);}
 assert(!std::filesystem::exists(std::filesystem::path(parent)/"game"));
 load(valid);auto before=ring;s.sysutil_connections.clear();fault([&]{service_sysutil_connect(s,0x86000400);});assert(ring==before);
 s.sysutil_connections.insert(UINT64_C(1)<<32);s.fs.game_data.directory.clear();
 load(valid);before=ring;fault([&]{service_sysutil_connect(s,0x86000400);});assert(ring==before);
 s.fs.game_data.directory="TEST00001";
 // A host file obstructs creation: VSH maps LV2 host errors to INTERNAL.
 {std::ofstream f(std::filesystem::path(parent)/"game");f<<"obstruction";}
 const auto error=native_gamedata::request(s,valid,1);assert(error[100]==0x80 && error[101]==2 && error[102]==0xb6 && error[103]==3);
 std::filesystem::remove(std::filesystem::path(parent)/"game");
 std::filesystem::create_directory(std::filesystem::path(parent)/"outside");
 std::filesystem::create_directory_symlink(std::filesystem::path(parent)/"outside",std::filesystem::path(parent)/"game");
 load(valid);before=ring;fault([&]{service_sysutil_connect(s,0x86000400);});assert(ring==before);
 assert(std::filesystem::is_empty(std::filesystem::path(parent)/"outside"));std::filesystem::remove(std::filesystem::path(parent)/"game");
 std::filesystem::create_directories(std::filesystem::path(parent)/"game/TEST00001/USRDIR");
 std::filesystem::create_symlink(std::filesystem::path(parent)/"outside/missing",std::filesystem::path(parent)/"game/TEST00001/PARAM.SFO");
 load(valid);before=ring;fault([&]{service_sysutil_connect(s,0x86000400);});assert(ring==before);
 std::filesystem::remove(std::filesystem::path(parent)/"game/TEST00001/PARAM.SFO");
 {std::ofstream f(std::filesystem::path(parent)/"game/TEST00001/PARAM.SFO");f<<"corrupt";}
 const auto broken=native_gamedata::request(s,valid,1);assert(broken[100]==0x80 && broken[101]==2 && broken[102]==0xb6 && broken[103]==6);
 assert(std::filesystem::file_size(std::filesystem::path(parent)/"game/TEST00001/PARAM.SFO")==7);
 std::filesystem::remove_all(parent);
''')

    def test_system_version_is_persisted_with_original_reply_alias(self):
        req = packet('zSetSysVersion',[('elmSysVersion',3,'03.0100')])
        bad = packet('zSetSysVersion',[('elmSysVersion',3,'../../x')])
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;char parent[]="/tmp/anyps3-gdat-version-XXXXXX";assert(::mkdtemp(parent));
 s.fs.configure_hdd(parent);s.fs.game_data.container=1;s.fs.game_data.no_calc=true;s.fs.game_data.directory="TEST00001";
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 const auto dir=std::filesystem::path(parent)/"game/TEST00001";std::filesystem::create_directories(dir);
 savedata_vsh::Sfo f;f.string("CATEGORY","GD",4);f.string("TITLE","preserved",128);auto raw=f.encode();
 {std::ofstream out(dir/"PARAM.SFO",std::ios::binary);out.write(reinterpret_cast<const char*>(raw.data()),raw.size());}
 const std::vector<std::uint8_t> req='''+cpp(req)+r''',bad='''+cpp(bad)+r''';
 fault([&]{native_gamedata::request(s,bad,1);});auto b=native_gamedata::request(s,req,1);
 assert(savedata_vsh::be(b,100)==0 && savedata_vsh::Document(b,0x47444154).name=="getGameDataStat");
 f=savedata_vsh::Sfo(savedata_vsh::read(dir/"PARAM.SFO"));
 assert(f.string("PS3_SYSTEM_VER",8)=="03.0100" && f.values.at("PS3_SYSTEM_VER").capacity==8 && f.string("TITLE",128)=="preserved");
 std::filesystem::remove_all(parent);
''')
