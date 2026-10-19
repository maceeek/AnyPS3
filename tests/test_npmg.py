"""Firmware 3.01 NPMG wire fixtures; no PSN service or synthetic login events."""
import shutil
import struct
import unittest

from tests import test_runtime_objects

# Exact 128 B captured in codex/npmg/trace-268.txt (managerRegisterCallbackExt).
REGISTER = bytes.fromhex(
    '4e504d4700000110000000400000001c0000006000000000000000600000001b'
    '0000008000000000000000800000000000000080000000000000000000000000'
    '0000000000000000ffffffffffffffffffffffffffffffffffffffff00000000'
    '6d616e61676572526567697374657243616c6c6261636b457874000000000000')

# Golden replies from the firmware parser's leaf contract. No runtime serializer
# is imported to calculate expected bytes. status is a ROOT int, OFFLINE = -1.
REPLIES = {
    'managerRegisterCallbackExt': bytes.fromhex(
        '4e504d4700000110000000400000002c00000070000000000000007000000022'
        '000000a000000000000000a000000000000000a0000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff0000001b'
        '000000010000000000000000000000006d616e61676572526567697374657243'
        '616c6c6261636b45787400726573756c74000000000000000000000000000000'
    ),
    'managerUnregisterCallbackExt': bytes.fromhex(
        '4e504d4700000110000000400000002c00000070000000000000007000000024'
        '000000a000000000000000a000000000000000a0000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff0000001d'
        '000000010000000000000000000000006d616e61676572556e72656769737465'
        '7243616c6c6261636b45787400726573756c7400000000000000000000000000'
    ),
    'managerGetStatus': bytes.fromhex(
        '4e504d4700000110000000400000003c0000008000000000000000800000001f'
        '000000a000000000000000a000000000000000a0000000000000000000000000'
        '0000000000000002ffffffffffffffffffffffffffffffffffffffff00000011'
        '0000000100000000000000000000001800000001ffffffff0000000000000000'
        '6d616e6167657247657453746174757300726573756c74007374617475730000'
    ),
    'managerGetNpId': bytes.fromhex(
        '4e504d4700000110000000400000002c00000070000000000000007000000016'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff0000000f'
        '000000018002aa0c00000000000000006d616e616765724765744e7049640072'
        '6573756c740000000000000000000000'
    ),
    'managerGetSubHandle': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001b'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000014'
        '000000018002aa0c00000000000000006d616e6167657247657453756248616e'
        '646c6500726573756c74000000000000'
    ),
    'managerGetUserIconURL': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001d'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000016'
        '000000018002aa0c00000000000000006d616e61676572476574557365724963'
        '6f6e55524c00726573756c7400000000'
    ),
    'managerGetMyLanguages': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001d'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000016'
        '000000018002aa0c00000000000000006d616e616765724765744d794c616e67'
        '756167657300726573756c7400000000'
    ),
    'managerGetNetworkTime': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001d'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000016'
        '000000018002aa0c00000000000000006d616e616765724765744e6574776f72'
        '6b54696d6500726573756c7400000000'
    ),
    'managerGetPsHandle': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001a'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000013'
        '000000018002aa0c00000000000000006d616e61676572476574507348616e64'
        '6c6500726573756c7400000000000000'
    ),
    'managerGetSubjectStatus': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001f'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000018'
        '000000018002aa0c00000000000000006d616e616765724765745375626a6563'
        '7453746174757300726573756c740000'
    ),
    'managerGetSubjectRegion': bytes.fromhex(
        '4e504d4700000110000000400000002c0000007000000000000000700000001f'
        '0000009000000000000000900000000000000090000000000000000000000000'
        '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000018'
        '000000018002aa0c00000000000000006d616e616765724765745375626a6563'
        '74526567696f6e00726573756c740000'
    ),
}

UNSUPPORTED = ['managerGetTicketEntById', 'managerLoadCachedInfo', 'managerGetTicket', 'managerGetTicketParam', 'managerGetTicketEntIdListBuf', 'managerGetSigninTicket', 'managerGetEnvnamae', 'managerGetSigninId', 'managerSubSignin', 'managerSubSignout', 'managerSubSigninAbort', 'managerSubRetrieveCb', 'managerRequestTicket']


def request(command):
    """Other no-argument requests use the same 0x201e229c builder as REGISTER."""
    name = command.encode() + b'\0'
    end = (96 + len(name) + 15) & ~15
    data = bytearray(REGISTER[:96] + name.ljust(end - 96, b'\0'))
    for at, value in ((28, len(name)), (32, end), (40, end), (48, end)):
        struct.pack_into('>I', data, at, value)
    return bytes(data)


def cpp_bytes(data):
    return '{' + ','.join(str(b) for b in data) + '}'


HARNESS = r'''
 (void)call;
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 const unsigned capacity=0x7c00;
 auto load=[&](const std::vector<std::uint8_t>& data,unsigned start=0){
   put(0,0x400);put(4,capacity);put(12,start);put(16,data.size());put(20,2);put(24,1);
   for(unsigned i=0;i<data.size();++i)bytes[0x400+(start+i)%capacity]=data[i];
 };
 auto expect=[&](const std::vector<std::uint8_t>& data,const std::vector<std::uint8_t>& reply,unsigned start=0){
   load(data,start);assert(service_sysutil_connect(s,0x86000000));
   const unsigned next=(start+data.size())%capacity;
   assert(word(12)==next && word(8)==(next+reply.size())%capacity);
   assert(word(16)==reply.size() && word(20)==2 && word(24)==2);
   for(unsigned i=0;i<reply.size();++i)assert(bytes[0x400+(next+i)%capacity]==reply[i]);
 };
 auto connect=[&](unsigned port){
   s.sysutil_connections.insert(port);
   s.events.queues.push_back({0x8d000000+port,4,UINT64_C(0x8006010000000100)|port,{}});
 };
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class NpManagerTests(unittest.TestCase):
    def test_exact_capture_callback_lifetime_status_and_offline_replies(self):
        self.assertEqual(request('managerRegisterCallbackExt'), REGISTER)
        body = HARNESS + 'connect(2);\n'
        for name in REPLIES:
            body += f'expect({cpp_bytes(request(name))},{cpp_bytes(REPLIES[name])});\n'
            if name == 'managerRegisterCallbackExt':
                body += 'assert(s.settings.np_manager_callbacks[2]);\n'
                body += f'expect({cpp_bytes(REGISTER)},{cpp_bytes(REPLIES[name])},capacity-64);\n'
                body += 's.trophy=NativeTrophy{};assert(s.settings.np_manager_callbacks[2]);\n'
            if name == 'managerUnregisterCallbackExt':
                body += 'assert(!s.settings.np_manager_callbacks[2]);\n'
        register_reply = REPLIES['managerRegisterCallbackExt']
        body += f'expect({cpp_bytes(REGISTER)},{cpp_bytes(register_reply)},capacity-{len(REGISTER)});\n'
        body += r'''
 assert(s.settings.np_manager_callbacks[2]);
 assert(!s.settings.np_manager_callbacks[0] && !s.settings.np_manager_callbacks[1] && !s.settings.np_manager_callbacks[3]);
 PpuState other;assert(!other.settings.np_manager_callbacks[2]);
 assert(s.events.deferred.empty());for(const auto& q:s.events.queues)assert(q.data.empty());
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body)

    def test_malformed_shapes_and_connections_fault_without_writes(self):
        body = HARNESS + f'const std::vector<std::uint8_t> valid={cpp_bytes(REGISTER)};\n'
        body += r'''
 (void)expect;(void)connect;
 auto reject=[&]{
   const auto before=bytes;const auto callbacks=s.settings.np_manager_callbacks;
   fault([&]{service_sysutil_connect(s,0x86000000);});
   assert(bytes==before && s.settings.np_manager_callbacks==callbacks);
   assert(s.events.deferred.empty());for(const auto& q:s.events.queues)assert(q.data.empty());
 };
 load(valid);reject(); // No connection.
 s.sysutil_connections.insert(2);load(valid);reject(); // No event queue.
 s.sysutil_connections.clear();connect(2);
 // Every header/tree word has an independently checked meaning; dirty padding
 // is accepted, while links, types, leaves and binary tables are not ignored.
 for(unsigned at:{4u,8u,12u,16u,20u,24u,28u,32u,36u,40u,44u,48u,52u,56u,60u,64u,68u,72u,76u,80u,84u,88u}){
   load(valid);put(0x400+at,word(0x400+at)^1);reject();
 }
 load(valid);bytes[0x400+122]='x';reject(); // Unterminated command.
 auto truncated=valid;truncated.resize(80);load(truncated);reject();
 load(valid);put(0x400+92,0x12345678);assert(service_sysutil_connect(s,0x86000000)); // Alignment padding.
 assert(s.settings.np_manager_callbacks[2]);
 load(valid);s.sysutil_connections.insert(1);reject(); // Ambiguous port.
 s.sysutil_connections.erase(1);load(valid);
 // Channel 1 cannot process a manager registration, even with a valid client.
 std::copy_n(bytes.begin(),0x8000,bytes.begin()+0x8000);auto before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before);
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body)

    def test_unsupported_commands_name_the_stop_without_writes(self):
        body = HARNESS + 'connect(0);(void)expect;\n'
        for name in UNSUPPORTED + ['managerUnknown']:
            body += f'load({cpp_bytes(request(name))});\n'
            body += '{const auto before=bytes;bool hit=false;try{service_sysutil_connect(s,0x86000000);}'
            body += 'catch(const std::runtime_error& e){hit=true;assert(std::string(e.what())=='
            body += f'"unsupported native NPMG command: {name}");}}'
            body += 'assert(hit && bytes==before && !s.settings.np_manager_callbacks[0]);}\n'
        test_runtime_objects.RuntimeObjectsTests().run_native(body)


if __name__ == '__main__':
    unittest.main()
