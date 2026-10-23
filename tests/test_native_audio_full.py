"""Firmware audio PARA, LV2 notification and PCM regressions with fixed vectors."""
import ctypes.util
import unittest

from tests import test_native_audio
from anyps3.services import IMPORTS


class AudioFullTests(unittest.TestCase):
    run_native = test_native_audio.NativeAudioTests.run_native

    def test_output_device_configuration_and_be_abi(self):
        self.run_native(r'''
 auto out=[&](unsigned op,std::uint64_t x=0,std::uint64_t y=0,std::uint64_t z=0,std::uint64_t w=0){r[3]=x;r[4]=y;r[5]=z;r[6]=w;return audio_out_service(s,op);};
 assert(out(0)==1 && out(0,1)==0 && out(0,2)==0x8002b242);
 assert(out(1,0,0,0x10400)==0);
 assert(mem.load(0x10400,4)==0x00020100 && mem.load(0x10406,2)==6);
 const unsigned char stereo[]={0,2,4,0,0,0,0,1},surround[]={0,8,4,0,0x40,0,0,0};
 for(unsigned i=0;i<8;++i){assert(mem.load(0x10408+i,1)==stereo[i]);assert(mem.load(0x10410+i,1)==surround[i]);}
 assert(out(1,0,1,0x10400)==0x8002b243);
 assert(out(1,0,0,0)==0x8002b242);
 mem.store(0x10300,8,UINT64_C(0x0200000000000000));mem.store(0x10308,8,0);
 assert(out(3,0,0x10300,0,1)==0 && s.audio.output_channels==2 && s.audio.sink_ready);
 assert(out(4,0,0x10340)==0 && mem.load(0x10340,8)==UINT64_C(0x0200000000000000));
 assert(out(2,0,0,0x10380)==0 && mem.load(0x10380,4)==0 && mem.load(0x10388,4)==0);
 assert(mem.load(0x1038c,8)==UINT64_C(0x0002040000000001));
 assert(out(3,1,0x10300)==0x8002b245);
 mem.store(0x10300,1,6);assert(out(3,0,0x10300)==0x8002b246);
 mem.store(0x10300,1,8);mem.store(0x10301,1,1);assert(out(3,0,0x10300)==0x8002b246);
 mem.store(0x10301,1,0);mem.store(0x1030c,4,1);assert(out(3,0,0x10300)==0x8002b241);
 assert(s.audio.output_channels==2);mem.store(0x1030c,4,0);assert(out(3,0,0x10300)==0);
 assert(out(5,0,2)==0 && s.audio.copy_control==2);assert(out(5,0,3)==0x8002b242);
 assert(out(6,0,0,4,0)==8 && out(6,0,1,4,0)==0);
 r[3]=0;r[4]=0;r[5]=4;r[6]=2;r[7]=0;assert(audio_out_service(s,7)==2);
 r[6]=6;assert(audio_out_service(s,7)==0);
 // The actual firmware PARA uses the same config bytes as the public service.
 std::vector<std::uint8_t> p(28);p[4]=2;p[27]=1;
 assert(audio_out_parameter(s,0x4c09,p)==0 && s.audio.output_channels==2);
 p.assign(28,0);assert(audio_out_parameter(s,0x4c0b,p)==0 && p[4]==2);
 fault([&]{audio_out_parameter(s,0x4cff,p);});
''')

    def test_notify_fifo_period_decimation_overflow_and_lifecycle(self):
        self.run_native(r'''
 auto ext=[&](unsigned op,std::uint64_t x=0,std::uint64_t y=0,std::uint64_t z=0){r[3]=x;r[4]=y;r[5]=z;return audio_extended_service(s,op);};
 assert(ext(4,1)==0x80310703);
 assert(ext(3,0x10400,0x10408)==0);const auto id=mem.load(0x10400,4),key=mem.load(0x10408,8);
 assert(key==UINT64_C(0x80004d494f323221));auto& q=s.events.queues.front();assert(q.id==id && q.capacity==2 && q.type==1);
 assert(audio(0)==0 && ext(4,key)==0 && ext(4,key)==0x80310709 && ext(4,123)==0x80310709);
 const auto source=s.audio.notifications.front().source;assert(source>>32==1 && (source&0xffffffff)==0x0e000000);
 s.audio.next_tick=798000000;
 audio_advance(s,797999999);assert(q.data.empty());audio_advance(s,798000000);
 assert(q.data.size()==1);const auto event=q.data.front();assert(event[0]==source && event[1]==0 && event[2]==0 && event[3]==0);
 audio_advance(s,798000000+2*425600);assert(q.data.size()==2 && s.audio.notify_dropped==1);
 s.audio.next_tick=UINT64_MAX/2;q.data.clear();assert(ext(5,key)==0 && ext(5,key)==0x80310709);
 assert(ext(10,key,0x08000000)==0);s.audio.next_tick=UINT64_MAX/2;
 audio_advance(s,UINT64_MAX/2);assert(q.data.empty());audio_advance(s,UINT64_MAX/2+425600);assert(q.data.size()==1);
 assert(ext(11,key,0)==0x80310709);assert(ext(11,key,0x08000000)==0);
 assert(ext(10,key,1)==0x80310704);fault([&]{ext(10,key,0x40000000);});
 s.audio.next_tick=UINT64_MAX/2;assert(audio(1)==0 && s.audio.notifications.empty());
''')

    def test_notify_queue_destroy_recreate_and_spu_type(self):
        self.run_native(r'''
 auto ext=[&](unsigned op,std::uint64_t x=0,std::uint64_t y=0,std::uint64_t z=0){r[3]=x;r[4]=y;r[5]=z;return audio_extended_service(s,op);};
 assert(audio(0)==0 && ext(9,0x10400,0x10408,2)==0x80310704);
 assert(ext(9,0x10400,0x10408,1)==0);assert(s.events.queues.front().type==2);
 const auto key=mem.load(0x10408,8);assert(ext(4,key)==0);
 const auto old=s.audio.notifications.front().queue;s.events.queues.clear();assert(!old->exists);
 assert(ext(3,0x10400,0x10408)==0 && mem.load(0x10408,8)==key);
 assert(ext(4,key)==0 && s.audio.notifications.size()==1 && s.audio.notifications.front().queue!=old);
 s.audio.next_tick=UINT64_MAX/2;audio_advance(s,UINT64_MAX/2);assert(s.events.queues.front().data.size()==1);
 assert(old->values.empty());s.audio.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''')

    def test_tags_timestamp_window_be_and_wrap(self):
        self.run_native(r'''
 auto ext=[&](unsigned op,std::uint64_t x,std::uint64_t y){r[3]=0;r[4]=x;r[5]=y;return audio_extended_service(s,op);};
 assert(audio(0)==0);param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& a=s.audio;auto& p=a.ports[0];a.next_tick=UINT64_MAX/2;
 assert(ext(0,0,0x10400)==0x8031070f);
 a.next_tick=798000000;audio_advance(s,798000000);a.next_tick=UINT64_MAX/2;
 assert(ext(0,0,0x10400)==0 && mem.load(0x10400,8)==10000000);
 assert(ext(1,0,0x10400)==0 && mem.load(0x10400,8)==8); // block 0 wraps beyond current block 1
 assert(ext(1,1,0x10400)==0 && mem.load(0x10400,8)==1);
 assert(ext(0,1,0x10400)==0x8031070f);
 assert(ext(1,8,0x10400)==0x80310704);
 a.next_tick=798425600;audio_advance(s,798000000+15*425600);a.next_tick=UINT64_MAX/2;
 assert(p.position==0 && p.tag==16);
 assert(ext(0,0,0x10400)==0x8031070f && ext(0,16,0x10400)==0x8031070f);
 assert(ext(0,1,0x10400)==0 && mem.load(0x10400,8)==10005333);
 assert(ext(0,15,0x10400)==0 && mem.load(0x10400,8)==10080000);
 const unsigned char stamp[]={0,0,0,0,0,0x99,0xcf,0x00};
 for(unsigned i=0;i<8;++i)assert(mem.load(0x10400+i,1)==stamp[i]);
 assert(audio(1)==0);
''')

    def test_adddata_float_abi_aliasing_and_six_channel_order(self):
        self.run_native(r'''
 auto ext=[&](unsigned op,std::uint64_t src,unsigned count=256){r[3]=0;r[4]=src;r[5]=count;return audio_extended_service(s,op);};
 assert(audio(0)==0);param(8,8);assert(audio(2,0x10100,0x10180)==0);
 auto& p=s.audio.ports[0];s.fpr[1]=UINT64_C(0x3fe0000000000000); // double 0.5 in f1
 const unsigned src[]={0x3f800000,0x40000000,0x40800000,0x41000000,0x41800000,0x42000000};
 mem.map(0x20000,0x4000,6,nullptr,0);for(unsigned i=0;i<6;++i)mem.store(0x20000+i*4,4,src[i]);
 assert(ext(8,0x20000)==0);
 const unsigned expected[]={0x3f000000,0x40000000,0x3f800000,0x41800000,0x40800000,0x41000000,0,0};
 for(unsigned i=0;i<8;++i)assert(mem.load(p.address+i*4,4)==expected[i]);
 // Add2ch touches just FL/FR, source is snapshotted even when it aliases output.
 mem.store(0x20000,4,0x3f800000);mem.store(0x20004,4,0xbf800000);
 assert(ext(7,0x20000)==0 && mem.load(p.address,4)==0x3f800000 && mem.load(p.address+4,4)==0x3fc00000);
 assert(ext(6,p.address)==0 && mem.load(p.address,4)==0x3fc00000 && mem.load(p.address+4,4)==0x40100000);
 assert(ext(6,0,256)==0x80310704 && ext(6,0x20001)==0x80310704 && ext(6,0x20000,128)==0x80310704);
 assert(audio(1)==0);
''')

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_explicit_sdl_stereo_device_actual_queue_downmix_and_level_ramp(self):
        self.run_native(r'''
 setenv("ANYPS3_AUDIO_SINK","sdl",1);setenv("SDL_AUDIODRIVER","dummy",1);setenv("ANYPS3_AUDIO_HOST_CHANNELS","2",1);
 assert(audio(0)==0);auto& a=s.audio;assert(!a.null_sink && a.device && a.host_channels==2);a.pause(a.device,1);
 param(8,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);a.next_tick=UINT64_MAX/2;
 param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,1)==0);
 auto& p=a.ports[0];const unsigned samples[]={0x3f800000,0xbf800000,0x3f000000,0x43000000,0x3e800000,0xbe800000,0x3e800000,0xbe800000};
 for(unsigned i=0;i<8;++i)mem.store(p.address+i*4,4,samples[i]);
 mem.store(a.ports[1].address,4,0x3e800000);mem.store(a.ports[1].address+4,4,0xbe800000);
 static int (*real_queue)(std::uint32_t,const void*,std::uint32_t)=nullptr;real_queue=a.queue;
 static std::array<float,512> captured{};
 a.queue=+[](std::uint32_t d,const void* data,std::uint32_t size){assert(size==2048);std::memcpy(captured.data(),data,size);return real_queue(d,data,size);};
 audio_advance(s,UINT64_MAX/2);
 assert(std::fabs(captured[0]-1.457f)<0.00001f && std::fabs(captured[1]+0.957f)<0.00001f);
 assert(a.queued(a.device)==2048 && a.nonzero_frames==256);
 a.next_tick=UINT64_MAX/2;r[3]=0;s.fpr[1]=0;assert(audio_extended_service(s,2)==0);
 const auto start=a.next_tick;audio_advance(s,start+2*425600);assert(p.level==0 && p.level_remaining==0);
 a.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''', null=False)

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_stalled_service_catches_up_guest_periods_but_queues_only_current_host_audio(self):
        # FIX7: a 2.5 s stall of the audio service (no audio_run) used to queue all 469 caught-up periods at once and
        # stop with "SDL2 audio output queue exceeds two seconds". Independent expectation (period 256/48000 s, late
        # limit 50 ms): periods k=0..468 end at k*period <= 2.5 s; only k with 2.5 s - k*period <= 50 ms (k=460..468,
        # nine periods) are still current. Every period keeps its guest effects (block, index, tag, counter).
        self.run_native(r'''
 setenv("ANYPS3_AUDIO_SINK","sdl",1);setenv("SDL_AUDIODRIVER","dummy",1);setenv("ANYPS3_AUDIO_HOST_CHANNELS","2",1);
 assert(audio(0)==0);auto& a=s.audio;assert(!a.null_sink && a.device && a.host_channels==2);a.pause(a.device,1);
 param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& p=a.ports[0];for(unsigned i=0;i<8;++i)mem.store(p.address+i*2048,4,0x3e800000);
 const std::uint64_t start=UINT64_MAX/4;a.next_tick=start;
 audio_advance(s,start+199500000); // 2.5 s at 79.8 MHz
 assert(a.counter==469 && p.tag==469 && mem.load(p.index,8)==469%8);
 for(unsigned i=0;i<p.size;++i)assert(mem.load(p.address+i,1)==0);
 assert(a.late_dropped_frames==460*256 && a.submitted_frames==9*256 && a.queued(a.device)==9*2048);
 audio_advance(s,a.next_tick); // back on time: the next period is queued normally
 assert(a.counter==470 && a.late_dropped_frames==460*256 && a.submitted_frames==10*256 && a.queued(a.device)==10*2048);
 a.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''', null=False)

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_absent_host_device_is_zero_and_cannot_configure(self):
        stderr=self.run_native(r'''
 setenv("ANYPS3_AUDIO_SINK","sdl",1);setenv("SDL_AUDIODRIVER","dummy",1);
 s.audio.library=dlopen(nullptr,RTLD_NOW);
 r[3]=0;assert(audio_out_service(s,0)==0 && !s.audio.sink_ready);
 std::vector<std::uint8_t> p(28);p[4]=2;assert(audio_out_parameter(s,0x4c09,p)==0x8002b244);
 assert(!s.audio.maximum && !s.audio.device && !s.audio.indices);
''', null=False, prefix=test_native_audio.FAILING_SDL)
        self.assertIn('output unavailable',stderr)

    def test_original_nids_and_extended_signed_abi(self):
        expected={'0x4109d08c':'cellAudioGetPortTimestamp','0xe4046afe':'cellAudioGetPortBlockTag',
                  '0x56dfe179':'cellAudioSetPortLevel','0x04af134e':'cellAudioCreateNotifyEventQueue',
                  '0x377e0cd9':'cellAudioSetNotifyEventQueue','0xff3626fd':'cellAudioRemoveNotifyEventQueue',
                  '0xdab029aa':'cellAudioAddData','0x9e4b1db8':'cellAudioAdd2chData','0x832df17e':'cellAudioAdd6chData'}
        for nid,name in expected.items():
            entry=IMPORTS[('cellAudio',nid)];self.assertEqual(entry[0],name);self.assertIn('sign_word(',entry[1])

    def test_timestamp_history_survives_stop_restart_gap_and_port_reopen(self):
        self.run_native(r'''
 assert(audio(0)==0);param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& a=s.audio;auto& p=a.ports[0];a.next_tick=798000000;
 audio_advance(s,798000000);a.next_tick=UINT64_MAX/2;assert(audio(5,0)==0);
 assert(audio(4,0)==0);a.next_tick=1596000000;audio_advance(s,1596000000);a.next_tick=UINT64_MAX/2;
 r[3]=0;r[4]=0;r[5]=0x10400;assert(audio_extended_service(s,0)==0 && mem.load(0x10400,8)==10000000);
 r[4]=1;assert(audio_extended_service(s,0)==0 && mem.load(0x10400,8)==20000000);
 r[4]=2;assert(audio_extended_service(s,0)==0x8031070f);
 assert(audio(6,0)==0 && audio(2,0x10100,0x10180)==0);
 assert(p.history.empty() && p.tag==0);
 r[3]=0;r[4]=0;r[5]=0x10400;assert(audio_extended_service(s,0)==0x8031070f);
 assert(audio(1)==0);
''')

    def test_notify_event_is_read_through_lv2_receive_with_big_endian_fields(self):
        self.run_native(r'''
 assert(audio(0)==0);r[3]=0x10400;r[4]=0x10408;assert(audio_extended_service(s,3)==0);
 const auto id=mem.load(0x10400,4),key=mem.load(0x10408,8);
 r[3]=key;assert(audio_extended_service(s,4)==0);const auto source=s.audio.notifications.front().source;
 s.audio.next_tick=UINT64_MAX/2;audio_advance(s,UINT64_MAX/2);
 r[11]=131;r[3]=id;r[4]=0x10500;r[5]=1;r[6]=0x10600;assert(event_syscall(s)==0);
 assert(mem.load(0x10600,4)==1 && mem.load(0x10500,8)==source);
 assert(mem.load(0x10508,8)==0 && mem.load(0x10510,8)==0 && mem.load(0x10518,8)==0);
 assert(s.events.queues.front().data.empty());s.audio.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''')

    @unittest.skipUnless(ctypes.util.find_library('SDL2'), 'Requires SDL2')
    def test_wav_capture_spectrum_continuity_duration_and_safe_defaults(self):
        import json
        import math
        from pathlib import Path
        import struct
        import tempfile
        import wave
        for sink in ('null', 'sdl'):
            with self.subTest(sink=sink), tempfile.TemporaryDirectory() as directory:
                capture=Path(directory)/'signal.wav'
                body=r'''
 setenv("SDL_AUDIODRIVER","dummy",1);setenv("ANYPS3_AUDIO_HOST_CHANNELS","2",1);
 setenv("ANYPS3_AUDIO_SINK",@SINK@,1);setenv("ANYPS3_AUDIO_CAPTURE",@CAPTURE@,1);
 assert(audio(0)==0);param(2,8);assert(audio(2,0x10100,0x10180)==0 && audio(4,0)==0);
 auto& a=s.audio;auto& p=a.ports[0];a.next_tick=UINT64_MAX/2;
 for(unsigned block=0;block<48;++block){
   for(unsigned i=0;i<256;++i){
     const auto sample=0.5f*std::sin(double(block*256+i)*6.283185307179586/48.0);
     const auto ea=p.address+(p.position*256+i)*8;
     audio_store_float(s,ea,sample);audio_store_float(s,ea+4,-sample);
   }
   audio_advance(s,a.next_tick);
 }
 assert(a.submitted_frames==12288 && a.capture_bytes==49152 && a.nonzero_frames==12288);
 a.next_tick=UINT64_MAX/2;assert(audio(1)==0 && a.capture); // Capture persists across guest Quit/Init.
'''.replace('@CAPTURE@',json.dumps(str(capture))).replace('@SINK@',json.dumps(sink))
                self.run_native(body)
                with wave.open(str(capture),'rb') as wav:
                    self.assertEqual((wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes()),(2,2,48000,12288))
                    samples=struct.unpack('<24576h',wav.readframes(12288))
                left=samples[::2];right=samples[1::2]
                self.assertTrue(all(l==-r for l,r in zip(left,right)))
                rms=math.sqrt(sum(x*x for x in left)/len(left))/32767
                self.assertAlmostEqual(rms,0.35355339,places=4)
                def amplitude(freq):
                    real=sum(x*math.cos(2*math.pi*freq*i/48000) for i,x in enumerate(left))
                    imag=sum(x*math.sin(2*math.pi*freq*i/48000) for i,x in enumerate(left))
                    return math.hypot(real,imag)*2/len(left)/32767
                self.assertAlmostEqual(amplitude(1000),0.5,places=4)
                self.assertLess(amplitude(2000),0.0001)
                self.assertLess(max(abs(left[i]-left[i-1]) for i in range(1,len(left))),2200)
                self.assertEqual(capture.stat().st_size,49196)

    def test_wav_existing_file_is_never_overwritten(self):
        import json
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            capture=Path(directory)/'existing.wav';capture.write_bytes(b'preserve')
            self.run_native(r'''
 setenv("ANYPS3_AUDIO_CAPTURE",PATH,1);
 fault([&]{audio(0);});assert(!s.audio.maximum && !s.audio.sink_ready && !s.audio.device && !s.audio.capture);
'''.replace('PATH',json.dumps(str(capture))))
            self.assertEqual(capture.read_bytes(),b'preserve')

    def test_audio_clock_advances_without_frames_or_active_ports_when_notify_registered(self):
        self.run_native(r'''
 assert(audio(0)==0);r[3]=0x10400;r[4]=0x10408;assert(audio_extended_service(s,3)==0);
 r[3]=mem.load(0x10408,8);assert(audio_extended_service(s,4)==0);
 const auto start=s.audio.next_tick;std::this_thread::sleep_for(std::chrono::milliseconds(24));
 const auto before=timebase_ticks();audio_run(s);const auto after=timebase_ticks();
 const auto periods=(before-start)/425600+1;
 assert(s.audio.counter>=periods && s.audio.counter<=(after-start)/425600+1);
 assert(s.audio.submitted_frames==s.audio.counter*256 && s.audio.running==0);
 assert(s.events.queues.front().data.size()==2);
 s.audio.next_tick=UINT64_MAX/2;assert(audio(1)==0);
''')

    def test_audio_para_binary_wire_replies_and_malformed_requests_are_atomic(self):
        self.run_native(r'''
 (void)call;s.sysutil_connections.insert(0);auto& bytes=*s.mmapper.objects.front().bytes;
 auto put=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)bytes[a+j]=v>>(24-j*8);};
 auto word=[&](unsigned a){return(std::uint32_t(bytes[a])<<24)|(std::uint32_t(bytes[a+1])<<16)|(std::uint32_t(bytes[a+2])<<8)|bytes[a+3];};
 auto request=[&](unsigned id,unsigned length,bool set=false,unsigned output=0){
   std::fill(bytes.begin(),bytes.end(),0);put(0,0x400);put(4,0x7c00);
   const unsigned size=208+((length+15)&~15u);std::vector<std::uint8_t> packet(size,0);
   auto p=[&](unsigned a,std::uint32_t v){for(unsigned j=0;j<4;++j)packet[a+j]=v>>(24-j*8);};
   p(0,0x50415241);p(4,0x110);p(8,64);p(12,104);p(24,176);p(28,21);p(48,208);p(52,size-208);
   p(68,1);p(84,44);p(88,44);p(92,4);p(96,1);p(100,id);
   p(108,7);p(112,2);p(136,13);p(140,6);p(148,length);p(152,UINT32_MAX);
   const char names[]="get\0id\0param\0payload\0";std::copy_n(names,21,packet.begin()+176);
   if(set){std::copy_n("set",3,packet.begin()+176);}p(208,output);
   std::copy(packet.begin(),packet.end(),bytes.begin()+1024);
   put(8,size);put(12,0);put(16,size);put(20,2);put(24,1);
 };
 auto reply=[&](){assert(service_sysutil_connect(s,0x86000000));return 1024+word(12);};
 request(0x4c05,8);auto at=reply();assert(word(at+100)==0 && word(at+212)==1 && word(at+148)==8);
 request(0x4c05,8,false,1);at=reply();assert(word(at+212)==0);
 request(0x4c05,8,false,2);at=reply();assert(word(at+100)==0x8002b242);
 request(0x4c06,144);at=reply();assert(word(at+100)==0 && word(at+148)==144 && word(at+216)==0x00020100);
 request(0x4c09,28,true);put(1024+212,0x02000000);at=reply();assert(word(at+100)==0 && s.audio.output_channels==2);
 request(0x4c0b,28);at=reply();assert(word(at+212)==0x02000000 && word(at+224)==0);
 request(0x4c07,28);at=reply();assert(word(at+228)==0x00020400 && word(at+232)==1);
 request(0x4c0a,8,true);put(1024+212,2);at=reply();assert(word(at+100)==0 && s.audio.copy_control==2);
 request(0x4c09,28,true);put(1024+212,0x06000000);at=reply();assert(word(at+100)==0x8002b246 && s.audio.output_channels==2);
 request(0x4c05,12);auto before=bytes;fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
 request(0x4c09,28);before=bytes;fault([&]{service_sysutil_connect(s,0x86000000);});assert(bytes==before);
''')

    def test_standalone_transport_without_audio_rejects_audio_request_without_consuming_it(self):
        from tests.test_savedata_vsh import SaveDataVshTests
        SaveDataVshTests.run_native(self,r'''
 (void)send;s.sysutil_connections.insert(0);
 B packet(224,0);auto p=[&](unsigned a,unsigned v){for(unsigned j=0;j<4;++j)packet[a+j]=v>>(24-j*8);};
 p(0,0x50415241);p(4,0x110);p(8,64);p(12,104);p(24,176);p(28,21);p(48,208);p(52,16);
 p(68,1);p(84,44);p(88,44);p(92,4);p(96,1);p(100,0x4c05);
 p(108,7);p(112,2);p(136,13);p(140,6);p(148,8);p(152,UINT32_MAX);
 const char names[]="get\0id\0param\0payload\0";std::copy_n(names,21,packet.begin()+176);
 std::copy(packet.begin(),packet.end(),raw.begin()+1024);
 put(0,1024);put(4,0x7c00);put(8,224);put(12,0);put(16,224);put(20,2);put(24,1);
 const auto before=raw;bool stopped=false;
 try{service_sysutil_connect(s,0x86000000);}catch(const std::runtime_error& e){stopped=std::string(e.what()).find("requires a native audio endpoint")!=std::string::npos;}
 assert(stopped && raw==before);
''')
