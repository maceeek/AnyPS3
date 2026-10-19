"""Independent byte/pixel oracles for RSX transfer classes (no game assets)."""
import shutil
import unittest
from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class RsxSweepBlitTests(unittest.TestCase):
    def test_copy_blit_and_fifo(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& g=s.rsx;auto& R=g.registers;
 g.dma=0x10000;g.fifo_enabled=true;
 mem.map(0x70000000,0x300000,6,nullptr,0);g.io[0]=0x70000000;g.io[1]=0x70100000;g.io[2]=0x70200000;
 mem.map(0xc0000000,0x200000,6,nullptr,0);g.local_size=0x200000;
 R[0x2184]=R[0x2188]=0xfeed0001;R[0x230c]=0x1000;R[0x2310]=0x1002;
 R[0x2314]=R[0x2318]=8;R[0x231c]=4;R[0x2320]=2;R[0x2324]=0x101;
 mem.store(0x70001000,8,UINT64_C(0x01020304aabbccdd));mem.store(0x70001008,8,UINT64_C(0x05060708eeff9988));
 rsxblit::copy(s,0); // in-place two-row copy preserves pitch padding
 assert(mem.load(0x70001000,8)==UINT64_C(0x010201020304ccdd));
 assert(mem.load(0x70001008,8)==UINT64_C(0x0506050607089988));
 R[0x230c]=0x100a;R[0x2310]=0x2000;R[0x2314]=0xfffffff8;R[0x2318]=8;R[0x2324]=0x402;R[0x231c]=2;
 mem.store(0x70002000,8,UINT64_C(0xeeeeeeeeeeeeeeee));mem.store(0x70002008,8,UINT64_C(0xeeeeeeeeeeeeeeee));
 rsxblit::copy(s,0); // signed negative input pitch, gather every second byte, scatter every fourth
 assert(mem.load(0x70002000,8)==UINT64_C(0x05eeeeee07eeeeee));
 assert(mem.load(0x70002008,8)==UINT64_C(0x01eeeeee03eeeeee));
 // Real FIFO launch of local-to-main copy; data words and pixels are BE.
 R[0x2184]=0xfeed0000;R[0x230c]=0x100;R[0x2310]=0x2100;R[0x2314]=R[0x2318]=4;
 R[0x2324]=0x101;R[0x231c]=4;R[0x2320]=1;mem.store(0xc0000100,4,0x89abcdef);
 mem.store(0x70000000,4,0x42328);mem.store(0x70000004,4,0);mem.store(g.dma+0x40,4,8);
 assert(rsx_run(s,1) && mem.load(0x70002100,4)==0x89abcdef && mem.load(g.dma+0x44,4)==8);
 // A bad later row must leave the first row and reservation version untouched.
 R[0x2310]=0x2ffffc;R[0x2320]=2;const auto token=mem.reservation_token(0x702fff80);
 fault([&]{rsxblit::copy(s,0);});assert(mem.load(0x702ffffc,4)==0 && mem.reservation_token(0x702fff80)==token);
 fault([&]{rsxblit::copy(s,1);});R[0x2320]=1;R[0x2324]=0x105;fault([&]{rsxblit::copy(s,0);});
 // A mapped first and last page cannot hide a missing middle page.
 g.io[1]=UINT32_MAX;fault([&]{rsxblit::address(g,0xfeed0001,0,0x200004);});g.io[1]=0x70100000;
 // Scaled 2x2 ARGB8 image -> 4x4, nearest samples [A A B B; A A B B; C C D D; C C D D].
 R[0xc184]=0xfeed0001;R[0xc198]=0x313371c3;R[0xc2fc]=1;R[0xc300]=3;R[0xc304]=3;
 R[0xc308]=0;R[0xc30c]=R[0xc314]=0x00040004;R[0xc310]=0;R[0xc318]=R[0xc31c]=0x80000;
 R[0xc400]=0x00020002;R[0xc404]=0x00020008;R[0xc408]=0x3000;
 R[0x6188]=0xfeed0000;R[0x6300]=10;R[0x6304]=20u<<16;R[0x630c]=0x1000;
 mem.store(0x70003000,8,UINT64_C(0xff000000ff400000));mem.store(0x70003008,8,UINT64_C(0xff004000ff404000));
 rsxblit::scaled(s,0);
 const unsigned nearest[16]={0xff000000,0xff000000,0xff400000,0xff400000,
  0xff000000,0xff000000,0xff400000,0xff400000,0xff004000,0xff004000,0xff404000,0xff404000,
  0xff004000,0xff004000,0xff404000,0xff404000};
 for(unsigned y=0;y<4;++y){for(unsigned x=0;x<4;++x)assert(mem.load(0xc0001000+y*20+x*4,4)==nearest[y*4+x]);assert(mem.load(0xc0001010+y*20,4)==0);}
 // FOH midpoint is the average of four corner pixels: R=G=32, B=0.
 R[0xc404]=0x01020008;rsxblit::scaled(s,0);assert(mem.load(0xc0001018,4)==0xff202000);
 // Clip to absolute destination [1,1 .. 3,3), leaving surrounding sentinel.
 for(unsigned y=0;y<4;++y)for(unsigned x=0;x<4;++x)mem.store(0xc0001000+y*20+x*4,4,0x12345678);
 R[0xc308]=0x10001;R[0xc30c]=0x20002;rsxblit::scaled(s,0);
 assert(mem.load(0xc0001018,4)==0xff202000 && mem.load(0xc0001000,4)==0x12345678 && mem.load(0xc0001048,4)==0x12345678);
 // Rectangular Morton swizzle 4x2: A B E F C D G H in physical memory.
 R[0xc198]=0x31337a73;R[0x8184]=0xfeed0000;R[0x8300]=0x0102000a;R[0x8304]=0x2000;
 R[0xc308]=0;R[0xc30c]=R[0xc314]=0x20004;R[0xc400]=0x20004;R[0xc404]=0x20010;R[0xc318]=R[0xc31c]=0x100000;
 for(unsigned i=0;i<8;++i)mem.store(0x70003000+i*4,4,0xff000001+i);
 rsxblit::scaled(s,0);const unsigned morton[]={1,2,5,6,3,4,7,8};
 for(unsigned i=0;i<8;++i)assert(mem.load(0xc0002000+i*4,4)==0xff000000+morton[i]);
 // RGB565 conversion: primary red and green map exactly, alpha is opaque on read.
 R[0xc198]=0x313371c3;R[0x6300]=4;R[0x6304]=4u<<16;R[0x630c]=0x4000;
 R[0xc30c]=R[0xc314]=R[0xc400]=0x10002;R[0xc404]=0x20008;
 mem.store(0x70003000,8,UINT64_C(0xffff0000ff00ff00));rsxblit::scaled(s,0);assert(mem.load(0xc0004000,4)==0xf80007e0);
 R[0xc300]=7;R[0xc408]=0x4000;R[0xc184]=0xfeed0000;R[0xc404]=0x20004;
 R[0x6300]=10;R[0x6304]=8u<<16;R[0x630c]=0x5000;rsxblit::scaled(s,0);
 assert(mem.load(0xc0005000,8)==UINT64_C(0xffff0000ff00ff00));
 // Alias-safe in-place horizontal reverse using signed 12:20 step.
 R[0xc300]=3;R[0xc408]=0x5000;R[0xc404]=0x20008;R[0xc318]=0xfff00000;
 rsxblit::scaled(s,16);assert(mem.load(0xc0005000,8)==UINT64_C(0xff00ff00ffff0000));
 // CPU image registers are indexed pixels; non-incrementing writes replace the same pixel.
 R[0xa19c]=0x313371c3;R[0xa2f8]=1;R[0xa2fc]=3;R[0xa300]=4;R[0xa304]=0;R[0xa308]=R[0xa30c]=0x10002;
 R[0x630c]=0x6000;rsxblit::cpu(s,0xa400,0x11223344);rsxblit::cpu(s,0xa404,0x55667788);
 assert(mem.load(0xc0006000,8)==UINT64_C(0x1122334455667788));
 mem.store(0x70000008,4,0x40000000|(2u<<18)|0xa400);mem.store(0x7000000c,4,0xabcdef01);mem.store(0x70000010,4,0xdeadbeef);
 mem.store(g.dma+0x40,4,20);assert(rsx_run(s,1) && mem.load(0xc0006000,8)==UINT64_C(0xdeadbeef55667788));
 // Unsupported context/operation/format does not silently become SRCCOPY.
 R[0xc304]=1;fault([&]{rsxblit::scaled(s,0);});R[0xc304]=3;
 R[0xc300]=5;fault([&]{rsxblit::scaled(s,0);});R[0xc300]=3;
 R[0xa300]=99;fault([&]{rsxblit::cpu(s,0xa400,0);});

 // Read-only destinations reject without publishing pixels or a reservation version.
 R[0x2184]=R[0x2188]=0xfeed0000;R[0x230c]=0x100;R[0x2310]=0x6000;
 R[0x231c]=4;R[0x2320]=1;R[0x2324]=0x101;const auto old_pixel=mem.load(0xc0006000,4);
 for(auto& region:mem.regions)if(region.base==0xc0000000)region.flags=4;
 fault([&]{rsxblit::copy(s,0);});assert(mem.load(0xc0006000,4)==old_pixel);
 for(auto& region:mem.regions)if(region.base==0xc0000000)region.flags=6;
 // Different DMA locations can alias the same host storage through an IO map.
 g.io[1]=0xc0000000;R[0x2184]=0xfeed0001;R[0x230c]=0x105000;R[0x2310]=0x5002;
 mem.store(0xc0005000,8,UINT64_C(0x0102030405060708));rsxblit::copy(s,0);
 assert(mem.load(0xc0005000,8)==UINT64_C(0x0102010203040708));g.io[1]=0x70100000;
 std::puts("PASS M2MF signed pitches/strides/overlap/BE/FIFO, scaled nearest/FOH/clip/swizzle/RGB565/alias, CPU blit and faults");
''')
