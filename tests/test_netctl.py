"""libnetctl getstate over the native sysutil ring, using the request captured in run 266."""
import shutil
import unittest

from tests import test_runtime_objects

# native-virtual-266 stderr "sysutil request channel=0 size=128" (libnetctl cellNetCtlGetState).
REQUEST = bytes.fromhex(
    '4e54434c00000110000000400000002c00000070000000000000007000000009'
    '0000008000000000000000800000000000000080000000000000000000000000'
    '0000000000000001ffffffffffffffffffffffffffffffffffffffffffffffff'
    '0000000000000000000000000000000067657473746174650000000000000000')


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class NetCtlTests(unittest.TestCase):
    def test_getstate_reply_tree_reports_disconnected_and_rejects_other_shapes(self):
        load = ''.join(f'bytes[0x400+{i}]={b};' for i, b in enumerate(REQUEST))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto request=[&]{put(12,0);put(16,128);put(20,2);put(24,1);''' + load + r'''};
 auto text=[&](unsigned reply,unsigned name){return std::string(reinterpret_cast<const char*>(bytes.data()+reply+word(reply+24)+name));};
 request();put(0x400+100,1);auto before=bytes;
 fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);   // unexpected leaf value
 request();put(0x400+68,2);before=bytes;fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
 request();assert(service_sysutil_connect(s,0x86000000));
 const unsigned reply=0x400+word(12);assert(word(12)==128 && word(24)==2 && word(20)==2);
 assert(word(reply)==0x4e54434c && word(reply+8)==64 && word(reply+12)==88);
 assert(text(reply,word(reply+64))=="getstate" && word(reply+68)==1 && word(reply+84)==44 && word(reply+88)==44);
 assert(text(reply,word(reply+92))=="result" && word(reply+96)==1 && word(reply+100)==0);
 const unsigned child=reply+64+44;
 assert(text(reply,word(child))=="param" && word(child+4)==1 && word(child+8)==0);
 assert(word(child+12)==UINT32_MAX && word(child+16)==UINT32_MAX && word(child+20)==UINT32_MAX);
 assert(text(reply,word(child+28))=="state" && word(child+32)==1 && word(child+36)==0);
''')


if __name__ == '__main__':
    unittest.main()
