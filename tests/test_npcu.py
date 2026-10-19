"""Firmware 3.01 NPCU fixtures: custom-menu termination through the real ring."""
import shutil
import struct
import unittest

from tests import test_runtime_objects


# Exact 112 bytes from stop 298. Independent of the implementation's encoder.
TERM = bytes.fromhex(
    '4e50435500000110000000400000001c00000060000000000000006000000007'
    '0000007000000000000000700000000000000070000000000000000000000000'
    '0000000000000000ffffffffffffffffffffffffffffffffffffffff00000000'
    '636d5465726d00000000000000000000')

# VSH 3.01 0xf1d08/0xf1dbc: root "result", one type-1 "result" = 0.
# Both names share offset 0 (CXML interning, 0x16ab8c). Empty remaining tables.
# Independently reproduced by executing the original VSH cleanup and CXML
# serializer (codex/npcu/vsh_oracle.py); this is not a physical PS3 capture.
REPLY = bytes.fromhex(
    '4e50435500000110000000400000002c00000070000000000000007000000007'
    '0000008000000000000000800000000000000080000000000000000000000000'
    '0000000000000001ffffffffffffffffffffffffffffffffffffffff00000000'
    '00000001000000000000000000000000726573756c7400000000000000000000')


def cpp_bytes(data):
    return '{' + ','.join(str(x) for x in data) + '}'


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class NpcuTests(unittest.TestCase):
    def test_capture_reply_ring_wrap_and_strict_failures(self):
        self.assertEqual((len(TERM), len(REPLY)), (112, 128))
        body = r'''
 (void)call;
 auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned at,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[at+j]=v>>(24-j*8);};
 auto word=[&](unsigned at){return(std::uint32_t(bytes[at])<<24)|(std::uint32_t(bytes[at+1])<<16)|(std::uint32_t(bytes[at+2])<<8)|bytes[at+3];};
 constexpr unsigned capacity=0x7c00;
 auto load=[&](const std::vector<std::uint8_t>& data,unsigned start=0){
   put(0,0x400);put(4,capacity);put(8,(start+data.size())%capacity);
   put(12,start);put(16,data.size());put(20,2);put(24,1);
   for(unsigned i=0;i<data.size();++i)bytes[0x400+(start+i)%capacity]=data[i];
 };
'''
        body += f' const std::vector<std::uint8_t> term={cpp_bytes(TERM)},reply={cpp_bytes(REPLY)};\n'
        body += r'''
 auto reject=[&]{
   const auto before=bytes;
   const auto callbacks=s.settings.np_manager_callbacks;
   fault([&]{service_sysutil_connect(s,0x86000000);});
   assert(bytes==before && callbacks==s.settings.np_manager_callbacks);
 };
 load(term);reject(); // Missing connection must not consume the request.
 s.sysutil_connections.insert(4);load(term);reject(); // Invalid client port.
 s.sysutil_connections.clear();s.sysutil_connections.insert(2);
 s.settings.np_manager_callbacks[2]=true;
 auto expect=[&](const std::vector<std::uint8_t>& request,unsigned start){
   load(request,start);assert(service_sysutil_connect(s,0x86000000));
   const unsigned next=(start+request.size())%capacity;
   assert(word(12)==next && word(8)==(next+128)%capacity);
   assert(word(16)==128 && word(20)==2 && word(24)==2);
   for(unsigned i=0;i<reply.size();++i)assert(bytes[0x400+(next+i)%capacity]==reply[i]);
   assert(s.settings.np_manager_callbacks[2]); // cmTerm is not NPMG signout.
   assert(s.events.deferred.empty()); // No invented custom-menu callback.
 };
 // Request wrap, reply wrap, exact end, repeated idempotent termination.
 for(unsigned start:{0u,capacity-40,capacity-150,capacity-112,0u})expect(term,start);
 for(unsigned at:{4u,8u,12u,16u,20u,24u,28u,32u,36u,40u,44u,48u,52u,56u,60u,64u,68u,72u,76u,80u,84u,88u}){
   load(term);put(0x400+at,word(0x400+at)^1);reject();
 }
 load(term);put(0x400+68,0xffffffff);reject(); // Oversized leaf count.
 load(term);put(0x400+24,0xfffffffc);reject(); // String offset overflow.
 load(term);put(0x400+64,0xffffffff);reject(); // Root name out of bounds.
 auto bad=term;bad[102]='x';load(bad);reject(); // Unterminated name.
 bad=term;bad.resize(67);load(bad);reject();
 bad=term;bad.resize(111);load(bad);reject();
 bad=term;bad.push_back(0);load(bad);reject();
 auto padding=term;padding[92]=0xa5;padding[103]=0x5a;padding[111]=0xff;
 expect(padding,capacity-40); // CXML alignment padding has no semantics.
 s.sysutil_connections.insert(1);load(term);reject(); // Ambiguous client.
 s.sysutil_connections.erase(1);
 load(term);std::copy_n(bytes.begin(),0x8000,bytes.begin()+0x8000);
 s.sysutil_connections.insert(UINT64_C(1)<<32);
 auto before=bytes;fault([&]{service_sysutil_connect(s,0x86000400);});assert(bytes==before);
'''
        # No registrations/UI implementation: all five other 3.01 commands,
        # plus unknown commands, must stop instead of returning fake OFFLINE.
        for command in ('cmRetrieveEvent', 'cmSetActivation', 'cmRegistExcept',
                        'cmRegistExceptItem', 'cmRegistActions', 'cmUnknown'):
            name = command.encode() + b'\0'
            end = (96 + len(name) + 15) & ~15
            request = bytearray(TERM[:96] + name.ljust(end - 96, b'\0'))
            for at, value in ((28, len(name)), (32, end), (40, end), (48, end)):
                struct.pack_into('>I', request, at, value)
            body += f'load({cpp_bytes(request)});{{const auto before=bytes;bool hit=false;'
            body += 'try{service_sysutil_connect(s,0x86000000);}catch(const std::runtime_error& e){hit=true;'
            body += f'assert(std::string(e.what())=="unsupported native NPCU command: {command}");}}'
            body += 'assert(hit && bytes==before);}\n'
        test_runtime_objects.RuntimeObjectsTests().run_native(body)


if __name__ == '__main__':
    unittest.main()
