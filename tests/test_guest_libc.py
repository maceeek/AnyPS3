import shutil
import unittest

from anyps3.services import IMPORTS
from tests import test_runtime_objects


def put(address, data):
    return ''.join(f'mem.store(0x{address + i:x},1,0x{b:02x});' for i, b in enumerate(data))


def check(address, data):
    return ''.join(f'assert(mem.load(0x{address + i:x},1)==0x{b:02x});' for i, b in enumerate(data))


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class GuestLibcTests(unittest.TestCase):
    def test_registered_nids(self):
        names = {key[1]: value[0] for key, value in IMPORTS.items() if key[0] == 'sysPrxForUser'}
        for nid, name in (('0xfb5db080', '_sys_memcmp'), ('0x27427742', '_sys_memmove'), ('0x7498887b', '_sys_strchr'),
                          ('0x191f0c4a', '_sys_strrchr'), ('0xd3039d4d', '_sys_strncpy'), ('0x1ca525a2', '_sys_strncasecmp')):
            self.assertEqual(names[nid], name)

    def test_string_and_memory_primitives(self):
        body = '(void)call;\n'
        # memcmp: unsigned bytes, sign only, size 0 equal, high r3/r4 bits ignored.
        body += put(0x10100, b'ab\x80d') + put(0x10200, b'ab\x01d')
        body += 'assert(guest_memcmp(s,0x10100,0x10200,4)==1 && guest_memcmp(s,0x10200,0x10100,4)==-1);'
        body += 'assert(guest_memcmp(s,0x10100,0x10200,2)==0 && guest_memcmp(s,0x10100,0x10200,0)==0);'
        body += 'assert(guest_memcmp(s,UINT64_C(0xffffffff00010100),0x10100,4)==0);'
        # memmove: overlapping forward and backward, returns dst.
        body += put(0x10300, b'0123456789')
        body += 'r[3]=0x10302;r[4]=0x10300;r[5]=6;guest_memmove(s);assert(r[3]==0x10302);' + check(0x10300, b'0101234589')
        body += 'r[3]=0x10300;r[4]=0x10302;r[5]=6;guest_memmove(s);' + check(0x10300, b'0123454589')
        # strchr/strrchr: first/last, NUL finds terminator, absent -> 0, byte >= 0x80.
        body += put(0x10400, b'a/b/\xc3c\x00')
        body += 'assert(guest_strchr(s,0x10400,0x2f,false)==0x10401 && guest_strchr(s,0x10400,0x2f,true)==0x10403);'
        body += 'assert(guest_strchr(s,0x10400,0,false)==0x10406 && guest_strchr(s,0x10400,0,true)==0x10406);'
        body += 'assert(guest_strchr(s,0x10400,0x7a,false)==0 && guest_strchr(s,0x10400,0xc3,true)==0x10404);'
        # strncpy: pads with zeros after NUL, no terminator when truncated, len<=0 writes nothing.
        body += put(0x10500, b'xyz\x00') + put(0x10600, b'\xee' * 8)
        body += 'r[3]=0x10600;r[4]=0x10500;r[5]=6;guest_strncpy(s);assert(r[3]==0x10600);' + check(0x10600, b'xyz\x00\x00\x00\xee\xee')
        body += put(0x10700, b'\xee' * 4) + 'r[3]=0x10700;r[4]=0x10500;r[5]=2;guest_strncpy(s);' + check(0x10700, b'xy\xee\xee')
        body += 'r[3]=0x10700;r[4]=0x10500;r[5]=UINT64_C(0xffffffff);guest_strncpy(s);assert(r[3]==0x10700);' + check(0x10700, b'xy\xee\xee')
        body += 'r[3]=0;r[4]=0x10500;r[5]=3;guest_strncpy(s);assert(r[3]==0);'
        # strncasecmp: ASCII folding only, stops at NUL and n.
        body += put(0x10800, b'Front_End\x00') + put(0x10900, b'fRONT_eNd\x00') + put(0x10a00, b'front_x\x00') + put(0x10b00, b'\xc3\x00')
        body += 'assert(guest_strncasecmp(s,0x10800,0x10900,64)==0 && guest_strncasecmp(s,0x10800,0x10a00,6)==0);'
        body += 'assert(guest_strncasecmp(s,0x10800,0x10a00,7)==-1 && guest_strncasecmp(s,0x10a00,0x10800,7)==1);'
        body += 'assert(guest_strncasecmp(s,0x10800,0x10b00,0)==0);fault([&]{guest_strncasecmp(s,0x10800,0x10b00,1);});'
        body += 'fault([&]{guest_memcmp(s,0x10100,0x90000000,1);});'  # unmapped range traps
        test_runtime_objects.RuntimeObjectsTests().run_native(body)


if __name__ == '__main__':
    unittest.main()
