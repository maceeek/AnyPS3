"""Observable native GPU memory and FIFO behavior; no game assets needed."""
import shutil
import unittest
from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'),'Requires C++')
class RsxTests(unittest.TestCase):
    def test_failed_scanout_does_not_complete_flip_or_release_waiter(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.driver=0x10000;gpu.reports=0x10000;
 gpu.display_buffers[1]={640,360,2560,0};mem.store(gpu.driver+0x1100,4,0x40000002);
 mem.store(gpu.driver+0x110c,4,1);mem.store(gpu.reports+0x10,8,UINT64_MAX);
 fault([&]{rsx_flip_display(s,1,0x8000010f);}); // Runtime compiled without Vulkan.
 assert(gpu.captured_frames==0 && mem.load(gpu.reports+0x10,8)==UINT64_MAX);
 assert(mem.load(gpu.driver+0x1100,4)==0x40000002 && mem.load(gpu.driver+0x1108,4)==0);
 fault([&]{rsx_flip_display(s,1,0x80000102);});
 fault([&]{rsx_flip_display(s,1,0x80000201);});
 fault([&]{rsx_flip_display(s,1,0x1000);});
''')

    def test_idle_clock_coalescing_does_not_delay_active_fifo_vblank(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.context=1;gpu.driver=0x10000;gpu.fifo_enabled=true;
 const auto now=std::uint64_t(std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count());
 gpu.vblank_epoch_ns=now-1001000000;gpu.vblank_poll_ticks=1;
 assert(!rsx_run_impl(s,0,true) && gpu.vblank_ticks==0);
 // An active FIFO checks wall time even between coalesced idle poll slots.
 assert(rsx_run_impl(s,0,false) && gpu.vblank_ticks>=60);
 assert(mem.load(gpu.driver+0x1128,8)==gpu.vblank_ticks);
''')

    def test_vblank_follows_advertised_clock_and_coalesces_pending_interrupts(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.context=1;gpu.driver=0x10000;gpu.fifo_enabled=true;
 gpu.event_port=123;gpu.event_queue=456;s.events.ports.push_back({123,1,456,789});
 s.events.queues.push_back({456,1,0,{}});mem.store(gpu.driver+0x12c0,4,0x802);
 assert(!rsx_tick_vblank(s,1000000000));assert(!rsx_tick_vblank(s,1016683333));
 assert(rsx_tick_vblank(s,1016683334) && gpu.vblank_ticks==1 && s.reschedule);
 assert(mem.load(gpu.driver+0x1128,8)==1 && mem.load(gpu.driver+0x1118,8)==1016683);
 assert(s.events.queues.back().data[0][2]==0x802);
 assert(rsx_tick_vblank(s,2001000000) && gpu.vblank_ticks==60 && s.events.queues.back().data.size()==1);
 assert(mem.load(gpu.driver+0x1128,8)==60);assert(!rsx_tick_vblank(s,2001000000));
 s.events.queues.back().data.clear();mem.store(gpu.driver+0x12c0,4,0);
 assert(rsx_tick_vblank(s,3002000000) && s.events.queues.back().data.empty());
 fault([&]{rsx_tick_vblank(s,1);});
''')

    def test_display_queue_notifies_firmware_without_completing_flip(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.driver=0x10000;gpu.event_port=123;gpu.event_queue=456;
 gpu.display_buffers[1]={640,360,2560,0};s.events.ports.push_back({123,1,456,789});
 s.events.queues.push_back({456,1,0,{}});mem.store(gpu.driver+0x1100,4,0x80000000);
 mem.store(gpu.driver+0x12c0,4,0x40);
 rsx_queue_display(s,1,1);const auto& data=s.events.queues.back().data;
 assert(data.size()==1 && data[0][0]==789 && data[0][1]==0 && data[0][2]==0x40 && data[0][3]==0);
 assert(mem.load(gpu.driver+0x1100,4)==0xc0000002 && mem.load(gpu.driver+0x110c,4)==1);
 assert(mem.load(gpu.driver+0x10f8,8)==0 && mem.load(gpu.driver+0x1108,4)==0);
 fault([&]{rsx_queue_display(s,1,1);});assert(data.size()==1);
 fault([&]{rsx_queue_display(s,2,1);});fault([&]{rsx_queue_display(s,1,0);});
 mem.store(gpu.driver+0x12c0,4,0);rsx_queue_display(s,1,1);assert(data.size()==1);
 gpu.device=0x11000;rsx_release_semaphore(s,gpu.device+0x30,0);
 assert(mem.load(gpu.device+0x30,4)==1 && gpu.display_queue_acknowledged==2);
 rsx_release_semaphore(s,gpu.device+0x30,0);assert(mem.load(gpu.device+0x30,4)==0);
 // No queue command means no acknowledgment; no flip completion is posted.
 assert(mem.load(gpu.driver+0x10f8,8)==0 && mem.load(gpu.driver+0x1108,4)==0);
''')

    def test_reports_publish_ordered_values_and_reject_invalid_targets(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;gpu.reports=0x80000000;
 mem.map(gpu.reports,0x100000,6,nullptr,0);mem.map(0x70000000,0x100000,6,nullptr,0);
 gpu.io[0]=0x70000000;gpu.io[0xe0]=0x70000000;gpu.fifo_enabled=true;
 gpu.registers[0x1a8]=0x66626660;gpu.report_values={17,31,47,61,79};
 for(unsigned type=1;type<=5;++type){
  const auto previous=gpu.report_timestamp;rsx_get_report(s,(type<<24)|0x7ff0);
  assert(mem.load(gpu.reports+0x93f0,8)>previous);
  // Without stats enable and a bound zeta Z-cull region, statistics are zero.
  assert(mem.load(gpu.reports+0x93f8,4)==(type==1?17u:0u));
  assert(mem.load(gpu.reports+0x93fc,4)==0);
 }
 // Z24S8 zeta at local 0x40000, center AA, bound region with zFormat 2/aa 0.
 gpu.registers[0x198]=0xfeed0000;gpu.registers[0x214]=0x40000;gpu.registers[0x208]=0x2u<<5;
 gpu.zculls[1].bound=true;gpu.zculls[1].offset=0x40000;gpu.zculls[1].region_word=(UINT64_C(0x21)<<32);
 auto stat=[&](unsigned type){rsx_get_report(s,(type<<24)|0x10);return mem.load(gpu.reports+0x1418,4);};
 for(unsigned type=2;type<=5;++type)assert(stat(type)==0);   // stats disabled
 gpu.registers[0x1804]=1;
 assert(stat(2)==0xffffffffu && stat(3)==0xffffffffu && stat(4)==0xffffffffu);
 assert(stat(5)==0);                                          // pixels passed
 gpu.report_values[0]=0;assert(stat(5)==0xffffu);
 gpu.registers[0x214]=0x50000;assert(stat(2)==0 && stat(5)==0);  // other zeta
 gpu.registers[0x214]=0x40000;gpu.registers[0x208]=0x1u<<5;assert(stat(2)==0);  // Z16 mismatch
 gpu.registers[0x208]=0x2u<<5;gpu.registers[0x198]=0xfeed0001;assert(stat(2)==0);
 gpu.registers.erase(0x1804);gpu.registers.erase(0x198);gpu.registers.erase(0x214);gpu.registers.erase(0x208);
 gpu.zculls[1]={};gpu.report_values={17,31,47,61,79};
 fault([&]{rsx_get_report(s,0x01008000);});fault([&]{rsx_get_report(s,0x01000001);});
 fault([&]{rsx_get_report(s,0x00000000);});fault([&]{rsx_get_report(s,0x06000000);});
 gpu.registers[0x1a8]=0xbad68000;rsx_get_report(s,0x02020a00);
 assert(mem.load(0x70020a08,4)==0 && mem.load(0x70020a00,8)==gpu.report_timestamp);
 mem.store(0x70000000,4,0x41800);mem.store(0x70000004,4,0x02100000);
 mem.store(gpu.dma+0x40,4,8);const auto previous=gpu.report_timestamp;
 fault([&]{rsx_run(s,1);});assert(mem.load(gpu.dma+0x44,4)==0 && previous==gpu.report_timestamp);
 gpu.registers[0x1a8]=0xfeed0001;rsx_get_report(s,0x01020a00);assert(mem.load(0x70020a08,4)==17);
 gpu.registers[0x1a8]=0xfeed0000;gpu.local_size=0x100000;mem.map(0xc0000000,0x100000,6,nullptr,0);
 rsx_get_report(s,0x010ffff0);assert(mem.load(0xc00ffff8,4)==17);
 fault([&]{rsx_get_report(s,0x04100000);});
 gpu.registers[0x1a8]=0;fault([&]{rsx_get_report(s,0x01000000);});
''')

    def test_conditional_render_enable_uses_report_value(self):
        # NV4097_SET_RENDER_ENABLE: mode 1 renders, mode 2 renders only while CellGcmReportData.value
        # (+8) of the referenced report is nonzero; other modes and misaligned offsets stop explicitly.
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.reports=0x80000000;mem.map(gpu.reports,0x100000,6,nullptr,0);
 gpu.registers[0x1a8]=0x66626660;
 mem.store(gpu.reports+0x1420,8,123);mem.store(gpu.reports+0x1428,4,0);mem.store(gpu.reports+0x142c,4,7);
 rsx_set_render_enable(s,0x02000020);assert(gpu.render_disabled);   // value 0 (timestamp/padding ignored)
 gpu.draw.primitive=5;gpu.draw.ranges={{0,3}};rsx_execute_draw(s);
 assert(gpu.draw.ranges.empty() && gpu.draw.draws==0);                // skipped before any state access
 rsx_set_render_enable(s,0x01000000);assert(!gpu.render_disabled);
 mem.store(gpu.reports+0x1428,4,1);rsx_set_render_enable(s,0x02000020);assert(!gpu.render_disabled);
 mem.store(gpu.reports+0x1428,4,0);rsx_set_render_enable(s,0x02000020);assert(gpu.render_disabled);
 rsx_set_render_enable(s,0x01000000);
 fault([&]{rsx_set_render_enable(s,0x00000000);});fault([&]{rsx_set_render_enable(s,0x03000020);});
 fault([&]{rsx_set_render_enable(s,0x02000024);});fault([&]{rsx_set_render_enable(s,0x02008000);});
 assert(!gpu.render_disabled);
''')

    def test_tiled_address_golden_offsets_and_unique_samples(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;
 NativeRsx::Tile tile;tile.offset=0x880000;tile.size=0x580000;tile.pitch=8192;tile.bank=1;
 // Golden offsets independently checked against the pinned reference's
 // tile-to-linear round trip, including row/bank and tile-grid boundaries.
 const std::pair<unsigned,unsigned> golden[]={{0,0x884000},{4,0x884004},{32,0x884100},{64,0x884280},
  {256,0x88c200},{8192,0x884020},{32768,0x884080},{65536,0x884c00},{131072,0x885200},
  {524288,0x908000},{1048576,0x98c000}};
 for(auto [linear,physical]:golden)assert(rsx_tiled_offset(tile,linear)==physical);
 for(unsigned pitch:{256,512,1280,5120,8192})for(unsigned bank=0;bank<4;++bank){
  tile.pitch=pitch;tile.bank=bank;std::set<unsigned> samples;
  for(unsigned y=0;y<128;++y)for(unsigned x=0;x<pitch/4;++x)
   assert(samples.insert(rsx_tiled_offset(tile,y*pitch+x*4)).second);
 }
 tile.pitch=8192;fault([&]{rsx_tiled_offset(tile,tile.size);});
 tile.pitch=7680;fault([&]{rsx_tiled_offset(tile,0);});
''')

    def test_transform_constants_use_load_index_and_preserve_raw_bits(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;
 mem.map(0x70000000,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;gpu.fifo_enabled=true;
 auto word=[&](unsigned offset,unsigned value){mem.store(0x70000000+offset,4,value);};
 word(0,(5u<<18)|0x1efc);word(4,7);word(8,0x3f800000);word(12,0x80000000);word(16,0x7fc12345);word(20,0xbf800000);
 mem.store(gpu.dma+0x40,4,24);assert(rsx_run(s,10));
 assert(gpu.transform_constants[28]==0x3f800000 && gpu.transform_constants[29]==0x80000000);
 assert(gpu.transform_constants[30]==0x7fc12345 && gpu.transform_constants[31]==0xbf800000);
 assert(!gpu.transform_constants_written[27] && gpu.transform_constants_written[28] && !gpu.transform_constants_written[32]);
 assert(gpu.registers.at(0x1efc)==7); // Constant load does not auto-increment.
 word(24,0x40000000|(2u<<18)|0x1f00);word(28,17);word(32,99);mem.store(gpu.dma+0x40,4,36);
 assert(rsx_run(s,10) && gpu.transform_constants[28]==99 && gpu.transform_constants[29]==0x80000000);
 gpu.registers[0x1efc]=512;word(36,0x41f00);word(40,0);mem.store(gpu.dma+0x40,4,44);
 fault([&]{rsx_run(s,10);});assert(mem.load(gpu.dma+0x44,4)==36 && gpu.transform_constants[28]==99);
''')

    def test_transform_program_upload_follows_and_advances_load_position(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;
 mem.map(0x70000000,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;gpu.fifo_enabled=true;
 auto word=[&](unsigned offset,unsigned value){mem.store(0x70000000+offset,4,value);};
 word(0,(1u<<18)|0x1e9c);word(4,3);
 word(8,(6u<<18)|0xb80);for(unsigned i=0;i<6;++i)word(12+4*i,0x100+i); // 1.5 instructions.
 mem.store(gpu.dma+0x40,4,36);assert(rsx_run(s,10));
 for(unsigned i=0;i<6;++i)assert(gpu.transform_program[12+i]==0x100+i && gpu.transform_program_written[12+i]);
 assert(!gpu.transform_program_written[11] && !gpu.transform_program_written[18]);
 assert(gpu.registers.at(0x1e9c)==4); // (6+0)/4 instructions completed.
 word(36,(2u<<18)|0xb88);word(40,0x200);word(44,0x201);mem.store(gpu.dma+0x40,4,48); // index 2: words 2,3.
 assert(rsx_run(s,10) && gpu.transform_program[18]==0x200 && gpu.transform_program[19]==0x201);
 assert(gpu.registers.at(0x1e9c)==5);
 // 544 instruction slots (RPCS3 program_util.h:10): the last slot 543 accepts a full instruction.
 gpu.registers[0x1e9c]=543;word(48,(4u<<18)|0xb80);for(unsigned i=0;i<4;++i)word(52+4*i,0x300+i);mem.store(gpu.dma+0x40,4,68);
 assert(rsx_run(s,10));for(unsigned i=0;i<4;++i)assert(gpu.transform_program[543*4+i]==0x300+i && gpu.transform_program_written[543*4+i]);
 assert(gpu.registers.at(0x1e9c)==544);
 word(68,(1u<<18)|0xb80);word(72,1);mem.store(gpu.dma+0x40,4,76);
 fault([&]{rsx_run(s,10);});assert(mem.load(gpu.dma+0x44,4)==68);
''')

    def test_nv4097_back_end_and_texture_read_semaphore_release(self):
        # 0x1d70 stores (v&0xff00ff00)|((v&0xff)<<16)|((v>>16)&0xff); 0x1d74 stores v (RPCS3 nv4097.cpp:787-842).
        test_runtime_objects.RuntimeObjectsTests().run_native(r"""
 auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;gpu.device=0x80000000;gpu.reports=0x80001000;
 mem.map(0x70000000,0x100000,6,nullptr,0);mem.map(gpu.device,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;
 auto word=[&](unsigned offset,unsigned value){mem.store(0x70000000+offset,4,value);};
 auto fifo=[&](unsigned get,unsigned put){assert(call(674,123,1,get,put)==0);};
 word(0,0x401a4);word(4,0x66606660);word(8,0x41d6c);word(12,0x40);word(16,0x41d70);word(20,0x11223344);fifo(0,24);
 assert(rsx_run(s,10) && mem.load(gpu.reports+0x40,4)==0x11443322);
 word(24,0x41d74);word(28,0x55667788);mem.store(gpu.dma+0x40,4,32);
 assert(rsx_run(s,10) && mem.load(gpu.reports+0x40,4)==0x55667788);
 word(32,0x41d6c);word(36,0x44);word(40,0x41d70);word(44,1);mem.store(gpu.dma+0x40,4,48);
 fault([&]{rsx_run(s,10);});assert(mem.load(gpu.reports+0x44,4)==0);       // unaligned offset
 word(32,0x401a4);word(36,0x12345678);mem.store(gpu.dma+0x44,4,32);gpu.registers[0x1d6c]=0x40;
 fault([&]{rsx_run(s,10);});                                                 // unknown DMA context
""")

    def test_diagnostic_skip_mode_logs_and_continues_unsupported_method(self):
        # ANYPS3_RSX_DIAG_SKIP=1 is diagnostic only: an unimplemented method is counted and stored raw.
        import os
        previous = os.environ.get('ANYPS3_RSX_DIAG_SKIP')
        os.environ['ANYPS3_RSX_DIAG_SKIP'] = '1'
        try:
            test_runtime_objects.RuntimeObjectsTests().run_native(r"""
 auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;gpu.device=0x80000000;gpu.reports=0x80001000;
 mem.map(0x70000000,0x100000,6,nullptr,0);mem.map(gpu.device,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;
 mem.store(0x70000000,4,0x40004);mem.store(0x70000004,4,0x5a5a5a5a);   // method 0x0004 is not implemented
 assert(call(674,123,1,0,8)==0);assert(rsx_run(s,10));
 assert(gpu.diag_skipped==1 && gpu.registers.at(0x4)==0x5a5a5a5a && mem.load(gpu.dma+0x44,4)==8);
""")
        finally:
            if previous is None:
                os.environ.pop('ANYPS3_RSX_DIAG_SKIP', None)
            else:
                os.environ['ANYPS3_RSX_DIAG_SKIP'] = previous

    def test_unwritten_nv4097_semaphore_offset_uses_reset_value_zero(self):
        # SET_SEMAPHORE_OFFSET (0x1d6c) never written: reset value 0 (RPCS3 rsx_methods.cpp:137).
        test_runtime_objects.RuntimeObjectsTests().run_native(r"""
 auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;gpu.device=0x80000000;gpu.reports=0x80001000;
 mem.map(0x70000000,0x100000,6,nullptr,0);mem.map(gpu.device,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;
 auto word=[&](unsigned offset,unsigned value){mem.store(0x70000000+offset,4,value);};
 mem.store(gpu.reports,4,0xdeadbeef);mem.store(gpu.reports+0x40,4,0xdeadbeef);
 word(0,0x401a4);word(4,0x66606660);word(8,0x41d74);word(12,0xa1b2c3d4);
 assert(call(674,123,1,0,16)==0);assert(rsx_run(s,10));
 assert(!gpu.registers.count(0x1d6c));
 assert(mem.load(gpu.reports,4)==0xa1b2c3d4 && mem.load(gpu.reports+0x40,4)==0xdeadbeef);
""")

    def test_fifo_order_partial_packets_semaphores_and_control_flow(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;gpu.device=0x80000000;gpu.reports=0x80001000;
 mem.map(0x70000000,0x100000,6,nullptr,0);mem.map(gpu.device,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;
 auto word=[&](unsigned offset,unsigned value){mem.store(0x70000000+offset,4,value);};
 auto fifo=[&](unsigned get,unsigned put){assert(call(674,123,1,get,put)==0);};
 word(0,0x40050);word(4,0x12345678);fifo(0,4);
 assert(!rsx_run(s,10) && mem.load(gpu.dma+0x44,4)==0 && mem.load(gpu.dma+0x48,4)==0);
 mem.store(gpu.dma+0x40,4,8);assert(rsx_run(s,10));
 assert(mem.load(gpu.dma+0x48,4)==0x12345678 && mem.load(gpu.dma+0x44,4)==8 && !rsx_run(s,10));
 // Validate the whole packet before publishing a reference in its first word.
 word(0,0x80050);word(4,0xdeadbeef);word(8,0);fifo(0,12);
 fault([&]{rsx_run(s,10);});assert(mem.load(gpu.dma+0x44,4)==0 && mem.load(gpu.dma+0x48,4)==0x12345678);
 // A real shared-memory write wakes acquire; blocked packets retain their cursor.
 word(0,0xc0060);word(4,0x66606660);word(8,0x20);word(12,0xabcdef01);fifo(0,16);
 assert(!rsx_run(s,10) && gpu.semaphore_wait && gpu.packet_index==2);
 assert(mem.load(gpu.dma+0x44,4)==0 && mem.load(gpu.reports+0x20,4)==0);
 mem.store(gpu.reports+0x20,4,0xabcdef01);assert(rsx_run(s,10) && !gpu.semaphore_wait && gpu.packet_index==0);
 word(16,0x4006c);word(20,0x98765432);mem.store(gpu.dma+0x40,4,24);
 assert(rsx_run(s,10) && mem.load(gpu.reports+0x20,4)==0x98765432);
 gpu.registers[0x64]=0x1000;fault([&]{rsx_semaphore_address(gpu,gpu.registers.at(0x60),gpu.registers.at(0x64));});
 gpu.registers[0x60]=0x56616661;gpu.registers[0x64]=0x30;
 assert(rsx_semaphore_address(gpu,gpu.registers.at(0x60),gpu.registers.at(0x64))==gpu.device+0x30);
 // Call -> reference -> return -> jump, then an idle self-jump yields.
 word(0,0x22);word(4,0x41);word(32,0x40050);word(36,77);word(40,0x20000);word(64,0x41);fifo(0,68);
 assert(rsx_run(s,10) && mem.load(gpu.dma+0x44,4)==64 && mem.load(gpu.dma+0x48,4)==77);
 assert(gpu.return_offset==UINT32_MAX && !rsx_run(s,10));
 word(64,0x20000048);word(72,0);mem.store(gpu.dma+0x40,4,76);
 assert(rsx_run(s,10) && mem.load(gpu.dma+0x44,4)==76);
 // Surface state is retained, but a clear action cannot silently succeed.
 word(0,0x41d90);word(4,0x12345678);word(8,0x41d94);word(12,0xf0);fifo(0,16);
 assert(rsx_run(s,1) && gpu.registers.at(0x1d90)==0x12345678);
 fault([&]{rsx_run(s,1);});assert(mem.load(gpu.dma+0x44,4)==8);
 word(12,0);assert(rsx_run(s,1)); // A zero clear mask changes no component.
 gpu.registers[0xc30c]=0;gpu.registers[0xc314]=0x10001;
 word(0,0x4c40c);word(4,0);fifo(0,8);assert(rsx_run(s,1));
 gpu.registers[0xc30c]=0x10001;fifo(0,8);fault([&]{rsx_run(s,1);});
 assert(mem.load(gpu.dma+0x44,4)==0); // Nonempty transfers remain explicit failures.
 gpu.report_values.fill(123);word(0,0x417c8);word(4,1);fifo(0,8);assert(rsx_run(s,1));
 assert(gpu.report_values[0]==0 && gpu.report_values[1]==123);
 word(4,2);fifo(0,8);assert(rsx_run(s,1));for(auto value:gpu.report_values)assert(value==0);
 word(0,0x41438);word(4,3);fifo(0,8);assert(rsx_run(s,1) && gpu.zcull_generation==1);
 word(0,0xc1710);word(4,0);word(8,0);word(12,0);fifo(0,16);
 assert(rsx_run(s,1) && gpu.vertex_cache_generation==2);
''')

    def test_device_memory_context_and_io_mapping(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 const auto main_used=mem.allocated;
 assert(call(675,0x10000,0,8)==0 && mem.load(0x10000,8)==0x60000000);
 assert(mem.allocated==main_used && mem.device_allocated==0x100000);
 assert(call(675,0x10008,0,8)==0 && mem.load(0x10008,8)==0x60000000 && mem.device_allocated==0x100000);
 r[7]=0x300000;r[8]=15;r[9]=8;
 assert(call(668,0x10010,0x10018,0x100001,0x80000)==0xffffffef);
 assert(call(668,0x10010,0x10018,0x100000,0x80000)==0);
 const auto handle=mem.load(0x10010,4);assert(handle==s.rsx.memory_handle && mem.load(0x10018,8)==0xc0000000);
 assert(mem.allocated==main_used && mem.device_allocated==0x200000);
 mem.store(0xc0000000,4,0xaabbccdd);assert(mem.load(0xc0000000,4)==0xaabbccdd);
 r[7]=handle;r[8]=0x20;assert(call(670,0x10020,0x10028,0x10030,0x10038)==0);
 const auto ctx=mem.load(0x10020,4),dma=mem.load(0x10028,8),driver=mem.load(0x10030,8),reports=mem.load(0x10038,8);
 assert(ctx==s.rsx.context && driver==dma+0x100000 && reports==driver+0x100000);
 assert(mem.load(s.rsx.device+0x30,4)==1); // Initially no queued display operation.
 assert(mem.load(driver+8,4)==0x100000 && mem.load(driver+0x50,4)==0x20);
 assert(mem.load(reports+0x1000,8)==UINT64_MAX && mem.load(reports+0x1408,4)==0);
 assert(mem.load(driver+0x12d0,4)==s.rsx.event_queue && s.events.queues.back().data.empty());
 assert(s.events.ports.back().queue==s.rsx.event_queue && mem.allocated==main_used);
 r[7]=0x80000000;assert(call(674,ctx,0x10a,1,0)==0);
 assert(mem.load(driver+0x1100,4)==0x80000000 && mem.load(driver+0x10c0,4)==0);
 r[7]=0x123;assert(call(674,ctx,0x10a,1,0x80000000)==0 && mem.load(driver+0x1100,4)==0x80000123);
 assert(call(674,ctx,0x10a,8,0)==0xffffffef);
 r[7]=handle;
 assert(call(670,0x10020,0x10028,0x10030,0x10038)==MM_EBUSY);
 mem.map(0x70000000,0x100000,6,nullptr,0);
 r[7]=UINT64_C(0xe000000000000800);
 assert(call(672,ctx,0,0x70000000,0x100000)==0 && s.rsx.io[0]==0x70000000);
 assert(s.rsx.io_flags[0]==UINT64_C(0xe000000000000800));
 assert(call(672,ctx,1,0x70000000,0x100000)==MM_EINVAL && s.rsx.io[0]==0x70000000);
 fault([&]{call(672,ctx,0x100000,0x70100000,0x100000);});assert(s.rsx.io[1]==UINT32_MAX);
 r[3]=1;rsx_set_flip_mode(s,1);assert(!s.rsx.vsync && s.rsx.flip_modes[1]==1);
 r[3]=2;rsx_set_flip_mode(s,1);assert(s.rsx.vsync);
 r[3]=3;rsx_set_flip_mode(s,0);assert(s.rsx.vsync && s.rsx.flip_modes[0]==3);
 r[3]=9;fault([&]{rsx_set_flip_mode(s,1);});assert(s.rsx.flip_modes[1]==2);
 r[7]=(UINT64_C(2560)<<32);r[8]=0;
 assert(call(674,ctx,0x104,3,(UINT64_C(640)<<32)|360)==0);
 assert(s.rsx.display_buffers[3].width==640 && s.rsx.display_buffers[3].height==360 && s.rsx.display_buffers[3].pitch==2560);
 assert(call(674,ctx,0x104,8,0)==0xffffffef);
 r[7]=(UINT64_C(256)<<32)|0x40000000;
 assert(call(674,ctx,0x300,1,(UINT64_C(0x21)<<32)|0xf0000)==0);
 const auto& tile=s.rsx.tiles[1];assert(tile.bound && tile.bank==2 && tile.size==0x100000 && tile.pitch==256 && tile.location==0);
 assert(call(674,ctx,0x300,1,(UINT64_C(0x21)<<32)|0x100000)==MM_EINVAL && tile.size==0x100000);
 assert(call(674,ctx,0x300,1,(UINT64_C(2)<<32)|0xf0000)==0 && tile.location==1);
 assert(call(674,ctx,0x300,1,(UINT64_C(2)<<32)|0x100000)==MM_EINVAL && tile.size==0x100000);
 assert(call(674,ctx,0x300,1,0)==0 && !tile.bound);
 r[7]=0;r[8]=UINT64_C(0xff00100400202000);
 assert(call(674,ctx,0x301,0,(UINT64_C(0x321)<<32)|0x400040)==0);
 const auto& z=s.rsx.zculls[0];assert(z.bound && z.width==64 && z.height==64 && z.start==0 && z.offset==0);
 r[7]=UINT64_C(0x300000)<<32;
 assert(call(674,ctx,0x301,0,(UINT64_C(0x321)<<32)|0x400040)==MM_EINVAL && z.start==0);
 assert(call(674,ctx,0x301,8,0)==0xffffffef);
''')


GAME_VP = [0x401f9c6c,0x0040010d,0x8106c083,0x6041ff9c, 0x401f9c6c,0x0040020d,0x8106c083,0x6041ffa0,
           0x401f9c6c,0x0040030d,0x8106c083,0x6041ffa4, 0x401f9c6c,0x0040040d,0x8106c083,0x6041ffa8,
           0x401f9c6c,0x0040050d,0x8106c083,0x6041ffac, 0x401f9c6c,0x0040060d,0x8106c083,0x6041ffb0,
           0x401f9c6c,0x0040070d,0x8106c083,0x6041ffb4, 0x401f9c6c,0x0040080d,0x8106c083,0x6041ffb8,
           0x401f9c6c,0x0040090d,0x8106c083,0x6041ff84, 0x00001c6c,0x00c06080,0x8186c080,0x20407ffc,
           0x401f9c6c,0x10406a0d,0x8106c0bf,0xe069e008, 0x401f9c6c,0x10406055,0x8106c095,0x40706000,
           0x401f9c6c,0x0080002a,0x80bfc043,0x60409f80, 0x401f9c6c,0x00800055,0x00800043,0x60411f81]


class RsxDrawTests(unittest.TestCase):
    def test_constant_fragment_draw_keeps_scissor_depth_blend_and_masks(self):
        import struct as st
        swap = lambda w: ((w << 16) | (w >> 16)) & 0xffffffff
        bits = lambda x: st.unpack('>I', st.pack('>f', x))[0]
        fp = [0x1e810140, 0xc8021c9d, 0xc8000001, 0xc8000001] + [swap(bits(x)) for x in (1.0, 0.5, 0.25, 0.5)]
        vp = ''.join(f'gpu.transform_program[{i}]={w}u;gpu.transform_program_written[{i}]=true;' for i, w in enumerate(GAME_VP))
        fpw = ''.join(f'mem.store(0xc0001000+{4*i},4,{w}u);' for i, w in enumerate(fp))
        verts = [(2, 3), (10, 3), (10, 7), (2, 7)]
        vw = ''.join(f'mem.store(0x70002000+{16*i}+{4*j},4,{bits(c)}u);' for i, v in enumerate(verts) for j, c in enumerate((*v, 0.0, 1.0)))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;
 mem.map(0x70000000,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;
 mem.map(0xc0000000,0x100000,6,nullptr,0);gpu.local_size=0x100000;
 ''' + vp + fpw + vw + r'''
 for(unsigned i=0;i<4;++i){gpu.transform_constants[24+i]=0x41000000;gpu.transform_constants_written[24+i]=true;}
 auto& R=gpu.registers;
 R[0x1ff0]=1;R[0x1740]=0x1042;R[0x1680]=0x80002000;R[0x1ea0]=0;
 R[0x208]=0x148;R[0x200]=16u<<16;R[0x204]=16u<<16;R[0x220]=1;
 R[0x194]=0xfeed0000;R[0x210]=0;R[0x20c]=64;
 R[0x198]=0xfeed0000;R[0x214]=0x4000;R[0x22c]=64;
 R[0x1d60]=0x02008400;
 R[0xa20]=0x41000000;R[0xa24]=0x41000000;R[0xa28]=0;
 R[0xa30]=0x41000000;R[0xa34]=0xc1000000;R[0xa38]=0x3f800000;
 R[0x8c0]=(4u<<16)|4;R[0x8c4]=(2u<<16)|4; // Exactly eight covered pixels.
 R[0xa74]=1;R[0xa70]=1;R[0xa6c]=0x201; // LESS; write depth, preserve stencil.
 R[0x304]=1;R[0x308]=0x204;R[0x30c]=64; // Alpha GREATER 64/255.
 R[0x310]=1;R[0x314]=(1u<<16)|0x302;R[0x318]=0x303;
 R[0x320]=0x80068006;R[0x324]=0x01010100; // RGB source-alpha blend; blue masked.
 // The identical MOV with a leading NOP takes the general quad interpreter.
 // Both executions must satisfy independently computed pixel expectations.
 mem.store(0xc0001100,4,0);mem.store(0xc0001104,4,0);mem.store(0xc0001108,4,0);mem.store(0xc000110c,4,0);
 for(unsigned i=0;i<32;++i)mem.store(0xc0001110+i,1,mem.load(0xc0001000+i,1));
 for(unsigned variant=0;variant<2;++variant){
   R[0x8e4]=variant?0x1101:0x1001;
   for(unsigned i=0;i<256;++i){mem.store(0xc0000000+i*4,4,0x11111111);mem.store(0xc0004000+i*4,4,0xffffff5a);}
   gpu.draw.primitive=8;gpu.draw.ranges={{0,4}};
   const auto before=gpu.draw.pixels;rsx_execute_draw(s);
   assert(gpu.draw.pixels-before==8);
   for(unsigned y=0;y<16;++y)for(unsigned x=0;x<16;++x){
     const bool inside=x>=4 && x<8 && y>=4 && y<6;
     assert(mem.load(0xc0000000+y*64+x*4,4)==(inside?0x80884811u:0x11111111u));
     assert(mem.load(0xc0004000+y*64+x*4,4)==(inside?0x0000005au:0xffffff5au));
   }
   gpu.draw.ranges={{0,4}};rsx_execute_draw(s); // Equal depth fails LESS.
   assert(gpu.draw.pixels-before==8);
   R[0xa74]=0;R[0x30c]=128; // 0.5 is below 128/255: alpha fails.
   gpu.draw.ranges={{0,4}};rsx_execute_draw(s);assert(gpu.draw.pixels-before==8);
   R[0xa74]=1;R[0x30c]=64;
 }
''')

    def test_quad_runs_original_programs_and_fills_with_top_left_rule(self):
        import struct as st
        swap = lambda w: ((w << 16) | (w >> 16)) & 0xffffffff
        f = lambda x: st.unpack('>I', st.pack('>f', x))[0]
        fp = [0x1e810140, 0xc8021c9d, 0xc8000001, 0xc8000001] + [swap(f(x)) for x in (1.0, 0.5, 0.25, 1.0)]
        vp = ''.join(f'gpu.transform_program[{i}]={w}u;gpu.transform_program_written[{i}]=true;' for i, w in enumerate(GAME_VP))
        fpw = ''.join(f'mem.store(0xc0001000+{4*i},4,{w}u);' for i, w in enumerate(fp))
        verts = [(2, 3), (10, 3), (10, 7), (2, 7)]
        vw = ''.join(f'mem.store(0x70002000+{16*i}+{4*j},4,{f(c)}u);' for i, v in enumerate(verts) for j, c in enumerate((*v, 0.0, 1.0)))
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;gpu.context=123;gpu.dma=0x10000;
 mem.map(0x70000000,0x100000,6,nullptr,0);gpu.io[0]=0x70000000;gpu.fifo_enabled=true;
 mem.map(0xc0000000,0x100000,6,nullptr,0);gpu.local_size=0x100000;
 for(unsigned i=0;i<16*64;++i)mem.store(0xc0000000+i,1,0x11);
 ''' + vp + fpw + vw + r'''
 for(unsigned i=0;i<4;++i){gpu.transform_constants[24+i]=0x41000000;gpu.transform_constants_written[24+i]=true;}
 auto& R=gpu.registers;
 R[0x1ff0]=1;R[0x1740]=0x1042;R[0x1680]=0x80002000;R[0x1ea0]=0;
 R[0x208]=0x148;R[0x200]=16u<<16;R[0x204]=16u<<16;R[0x220]=1;R[0x194]=0xfeed0000;R[0x210]=0;R[0x20c]=64;
 R[0x1d60]=0x02008400;R[0x8e4]=0x1001;
 R[0xa20]=0x41000000;R[0xa24]=0x41000000;R[0xa28]=0;R[0xa30]=0x41000000;R[0xa34]=0xc1000000;R[0xa38]=0x3f800000;
 auto word=[&](unsigned offset,unsigned value){mem.store(0x70000000+offset,4,value);};
 word(0,(1u<<18)|0x1808);word(4,8);word(8,(1u<<18)|0x1814);word(12,3u<<24);word(16,(1u<<18)|0x1808);word(20,0);
 mem.store(gpu.dma+0x40,4,24);assert(rsx_run(s,10));
 unsigned written=0;
 for(unsigned y=0;y<16;++y)for(unsigned x=0;x<16;++x){
   const auto p=mem.load(0xc0000000+y*64+x*4,4);
   const bool inside=x>=2 && x<10 && y>=3 && y<7;
   if(inside){assert(p==0xffff8040u);++written;}else assert(p==0x11111111u);
 }
 assert(written==32 && gpu.draw.draws==1 && gpu.draw.pixels==32 && gpu.draw.triangles==2);
 word(24,(1u<<18)|0x1814);word(28,0);mem.store(gpu.dma+0x40,4,32);
 fault([&]{rsx_run(s,10);}); // Draw command outside begin/end.
''')

    def test_register_vertex_attribute_values(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;auto& gpu=s.rsx;
 assert(rsx_vertex_data_register(gpu,0x1948,0xe5f1efc3u));
 const auto& ub=gpu.draw.immediate[2];assert(ub.count==4 && ub.type==1 && ub.words[0]==0xe5f1efc3u);
 assert(rsx_vertex_data_register(gpu,0x1888+4,0x3f800000u) && gpu.draw.immediate[1].count==2 && gpu.draw.immediate[1].words[1]==0x3f800000u);
 assert(rsx_vertex_data_register(gpu,0x1c00+16*3+12,0x40000000u) && gpu.draw.immediate[3].words[3]==0x40000000u);
 assert(!rsx_vertex_data_register(gpu,0x1740,0));
 gpu.registers[0x1740+4*2]=0x2; // array disabled: register value is used
 auto v=rsxdraw::fetch_attribute(s,rsxdraw::Reg{gpu},2,7);
 assert(v[0]==229.f/255.f && v[1]==241.f/255.f && v[2]==239.f/255.f && v[3]==195.f/255.f);
 gpu.draw.primitive=8; // Immediate mode: ATTR0 completion snapshots every register value.
 assert(rsx_vertex_data_register(gpu,0x1c00,1) && gpu.draw.immediate_vertices.empty());
 rsx_vertex_data_register(gpu,0x1c04,2);rsx_vertex_data_register(gpu,0x1c08,3);
 assert(rsx_vertex_data_register(gpu,0x1c0c,4) && gpu.draw.immediate_vertices.size()==1);
 assert(gpu.draw.immediate_vertices[0][2].words[0]==0xe5f1efc3u && gpu.draw.immediate_vertices[0][0].words[3]==4);
 rsx_vertex_data_register(gpu,0x1948,0x01020304u);rsx_vertex_data_register(gpu,0x1880,5);rsx_vertex_data_register(gpu,0x1884,6);
 assert(gpu.draw.immediate_vertices.size()==2 && gpu.draw.immediate_vertices[1][2].words[0]==0x01020304u);
 assert(gpu.draw.immediate_vertices[1][0].count==2 && gpu.draw.immediate_vertices[1][0].words[1]==6);
''')

    def test_fragment_condition_codes_masked_writes_and_kill(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;(void)s;using namespace rsxdraw;
 // OPDEST: end bit0, dest bits1-6, setcc bit8, mask bits9-12, opcode bits24-29. SRC0: type bits0-1 (2=constant),
 // swizzle xyzw=0xe4<<9, exec lt/eq/gt bits18-20, cond swizzle bits21-28, cc test bit31, cc update select bit30.
 const std::uint32_t identity=(0u<<9)|(1u<<11)|(2u<<13)|(3u<<15),cond_identity=(0u<<21)|(1u<<23)|(2u<<25)|(3u<<27);
 FpInst set{(1u<<24)|(15u<<9)|(1u<<8)|(1u<<1),2u|identity|(7u<<18)|cond_identity,0,0,{-1,0,2,3},true};
 FpInst gt{(1u<<24)|(15u<<9)|1u,2u|identity|(4u<<18)|cond_identity,0,0,{5,5,5,5},true};
 FpContext ctx;run_fragment_program({set,gt},ctx);
 auto r0=ctx.read_r(0),r1=ctx.read_r(1);
 assert(r1[0]==-1 && r1[1]==0 && r1[2]==2 && r1[3]==3 && ctx.cc[0][0]==-1 && ctx.cc[0][2]==2);
 assert(r0[0]==0 && r0[1]==0 && r0[2]==5 && r0[3]==5);
 FpInst skip{(1u<<24)|(15u<<9)|1u,2u|identity|cond_identity,0,0,{7,7,7,7},true};
 FpContext none;none.cc=ctx.cc;run_fragment_program({skip},none);assert(none.read_r(0)[0]==0);
 FpInst kill_lt{(18u<<24)|1u,(1u<<18)|cond_identity,0,0,{},false};
 FpContext k;k.cc=ctx.cc;run_fragment_program({kill_lt},k);assert(k.killed);
 FpInst kill_eq_on_cc1{(18u<<24)|1u,(2u<<18)|cond_identity|(1u<<31),0,0,{},false};
 FpContext k2;k2.cc[1]={1,1,1,1};run_fragment_program({kill_eq_on_cc1},k2);assert(!k2.killed);
''')

    def test_texture_sampling_formats_filters_wrap_and_remap(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;using namespace rsxdraw;auto& gpu=s.rsx;
 mem.map(0xc0000000,0x100000,6,nullptr,0);gpu.local_size=0x100000;
 auto& R=gpu.registers;
 // 2x2 A8R8G8B8 at 0x1000: texels (A,R,G,B) bytes.
 const std::uint32_t texels[4]={0xff000000u,0xffff0000u,0xff00ff00u,0x800000ffu};
 for(unsigned i=0;i<4;++i)mem.store(0xc0001000+i*4,4,texels[i]);
 auto setup=[&](unsigned format_byte,unsigned w,unsigned h,unsigned pitch,unsigned filter,unsigned wrap,unsigned remap){
   R[0x1a00]=0x1000;R[0x1a04]=0x00010029|(format_byte<<8);R[0x1a08]=wrap|(wrap<<8);R[0x1a0c]=0x80000000;
   R[0x1a10]=remap;R[0x1a14]=(filter<<16)|(filter<<24);R[0x1a18]=(w<<16)|h;R[0x1840]=pitch;
   return decode_texture(s,Reg{gpu},0);
 };
 auto t=setup(0xe5,2,2,8,1,3,0xaae4); // LN|UN|A8R8G8B8, nearest, clamp-to-edge
 auto c=sample_texture(t,0.5f,0.5f);assert(c[0]==0 && c[1]==0 && c[2]==0 && c[3]==1);
 c=sample_texture(t,1.5f,0.5f);assert(c[0]==1 && c[1]==0 && c[3]==1);
 c=sample_texture(t,1.5f,1.5f);assert(c[2]==1 && std::fabs(c[3]-128/255.f)<1e-6f);
 c=sample_texture(t,-3.f,9.f);assert(c[1]==1); // clamped to (0,1)
 t=setup(0xe5,2,2,8,2,3,0xaae4);c=sample_texture(t,1.f,1.f); // bilinear centre: average of four texels
 assert(std::fabs(c[0]-0.25f)<1e-6f && std::fabs(c[1]-0.25f)<1e-6f && std::fabs(c[2]-0.25f)<1e-6f && std::fabs(c[3]-(3+128/255.f)/4)<1e-6f);
 // CONVOLUTION_MIN=7 / CONVOLUTION_MAG=4 sample as linear without mipmaps (RPCS3 VKFormats.cpp:44,56).
 R[0x1a14]=(7u<<16)|(4u<<24);t=decode_texture(s,Reg{gpu},0);c=sample_texture(t,1.f,1.f);
 assert(std::fabs(c[0]-0.25f)<1e-6f && std::fabs(c[2]-0.25f)<1e-6f && std::fabs(c[3]-(3+128/255.f)/4)<1e-6f);
 R[0x1a14]=(0u<<16)|(2u<<24);fault([&]{decode_texture(s,Reg{gpu},0);});   // min filter 0 is undefined
 R[0x1a14]=(2u<<16)|(3u<<24);fault([&]{decode_texture(s,Reg{gpu},0);});   // mag filter 3 is undefined
 t=setup(0xa5,2,2,8,1,1,0xaae4);c=sample_texture(t,1.25f,0.25f); // normalized, wrap: u=2.5 -> x=0
 assert(c[0]==0 && c[1]==0);
 t=setup(0xa5,2,2,8,1,2,0xaae4);c=sample_texture(t,1.25f,0.25f); // mirror: x=2 -> 1
 assert(c[0]==1);
 t=setup(0xe5,2,2,8,1,3,0xa6e4);c=sample_texture(t,0.5f,0.5f); // R channel forced to one (control 1)
 assert(c[0]==1 && c[1]==0);
 // DXT1 4x4 block: c0=red565, c1=blue565, row0 indices 0,1,2,3.
 const std::uint8_t dxt1[8]={0x00,0xf8,0x1f,0x00,0xe4,0,0,0};
 for(unsigned i=0;i<8;++i)mem.store(0xc0001000+i,1,dxt1[i]);
 t=setup(0x86,4,4,0,1,3,0xaae4);
 c=sample_texture(t,0.125f,0.125f);assert(c[0]==1 && c[2]==0 && c[3]==1);
 c=sample_texture(t,0.375f,0.125f);assert(c[0]==0 && c[2]==1);
 c=sample_texture(t,0.625f,0.125f);assert(std::fabs(c[0]-2/3.f)<1e-6f && std::fabs(c[2]-1/3.f)<1e-6f);
 // DXT5: alpha0=255 alpha1=0, first texel index 1 -> alpha 0, second index 2 -> 6/7.
 const std::uint8_t dxt5[16]={255,0,0x11,0,0,0,0,0, 0x00,0xf8,0x1f,0x00,0,0,0,0};
 for(unsigned i=0;i<16;++i)mem.store(0xc0001000+i,1,dxt5[i]);
 t=setup(0x88,4,4,0,1,3,0xaae4);
 c=sample_texture(t,0.125f,0.125f);assert(c[3]==0);
 c=sample_texture(t,0.375f,0.125f);assert(std::fabs(c[3]-6/7.f)<1e-6f);
 // Border texels (bit3 clear): 2x2 LN texture stored inside a 4x4 block with a one-texel frame.
 for(unsigned i=0;i<16;++i)mem.store(0xc0002000+i*4,4,0xff111111u);
 mem.store(0xc0002000+16+4,4,0xffff0000u);
 R[0x1a00]=0x2000;R[0x1a04]=0x00010021|(0xe5<<8);R[0x1a14]=(1u<<16)|(1u<<24);R[0x1a18]=(2u<<16)|2;R[0x1840]=16;
 t=decode_texture(s,Reg{gpu},0);c=sample_texture(t,0.5f,0.5f);assert(c[0]==1 && c[1]==0);
 c=sample_texture(t,1.5f,1.5f);assert(std::fabs(c[0]-0x11/255.f)<1e-6f);
 R[0x1a08]=0x0303|(1u<<20);t=decode_texture(s,Reg{gpu},0);c=sample_texture(t,1.5f,1.5f); // gamma on R only
 assert(std::fabs(c[0]-std::pow((0x11/255.f+0.055f)/1.055f,2.4f))<1e-6f && std::fabs(c[1]-0x11/255.f)<1e-6f);
 R[0x1a08]=0x0303;
 R[0x1a04]=0x00010021|(0x86<<8);t=decode_texture(s,Reg{gpu},0);assert(t.format==0x86 && t.levels[0].border==0); // DXTC ignores border storage: RPCS3 TextureUtils.cpp:814-817
 R[0x1a0c]=0;fault([&]{decode_texture(s,Reg{gpu},0);}); // disabled unit
 R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a04]=0x00020029|(0x85<<8);R[0x1a14]=0x01030000;
 for(unsigned i=0;i<4;++i){mem.store(0xc0002000+4*i,4,0xffff0000);}
 mem.store(0xc0002010,4,0xff00ff00);   // 2x2 ARGB8 red base, 1x1 green mip at +16 bytes
 t=decode_texture(s,Reg{gpu},0);assert(t.levels.size()==2 && t.levels[1].data==t.storage+16);
 c=sample_texture(t,.5f,.5f,1);assert((c==Vec4{0,1,0,1})); // swizzled mipmaps are supported
''')

    def test_mipmap_selection_and_quad_derivatives(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;using namespace rsxdraw;auto& gpu=s.rsx;
 mem.map(0xc0000000,0x100000,6,nullptr,0);gpu.local_size=0x100000;
 auto& R=gpu.registers;
 // LN 4x4 A8R8G8B8, pitch 16, three levels: red, green, blue (levels share the row pitch).
 const std::uint32_t colours[3]={0xffff0000u,0xff00ff00u,0xff0000ffu};const unsigned heights[3]={4,2,1};
 unsigned offset=0x1000;
 for(unsigned l=0;l<3;++l){for(unsigned i=0;i<heights[l]*4;++i)mem.store(0xc0000000+offset+i*4,4,colours[l]);offset+=16*heights[l];}
 R[0x1a00]=0x1000;R[0x1a04]=0x00030029|(0xa5<<8);R[0x1a08]=0x0303;R[0x1a0c]=0x80000000|(0xfffu<<7);
 R[0x1a10]=0xaae4;R[0x1a18]=(4u<<16)|4;R[0x1840]=16;
 R[0x1a14]=(3u<<16)|(1u<<24); // NEAREST_MIPMAP_NEAREST
 auto t=decode_texture(s,Reg{gpu},0);
 assert(t.levels.size()==3 && t.levels[1].width==2 && t.levels[2].height==1);
 auto c=sample_texture(t,0.5f,0.5f,-1.f);assert(c[0]==1); // magnification
 c=sample_texture(t,0.5f,0.5f,1.f);assert(c[1]==1 && c[0]==0);
 c=sample_texture(t,0.5f,0.5f,7.f);assert(c[2]==1); // clamped to last level
 R[0x1a14]=(5u<<16)|(1u<<24);t=decode_texture(s,Reg{gpu},0); // NEAREST_MIPMAP_LINEAR
 c=sample_texture(t,0.5f,0.5f,1.5f);assert(std::fabs(c[1]-0.5f)<1e-6f && std::fabs(c[2]-0.5f)<1e-6f);
 R[0x1a0c]=0x80000000|(0x100u<<7);t=decode_texture(s,Reg{gpu},0); // max LOD 1.0
 c=sample_texture(t,0.5f,0.5f,5.f);assert(c[1]==1);
 // Quad TEX: TEX0 input u steps by 0.5 (2 texels) per pixel in x and y -> lambda 1 -> green.
 R[0x1a0c]=0x80000000|(0xfffu<<7);R[0x1a14]=(3u<<16)|(1u<<24);
 std::array<Texture,16> textures{};textures[0]=decode_texture(s,Reg{gpu},0);
 const std::uint32_t identity=(0u<<9)|(1u<<11)|(2u<<13)|(3u<<15);
 FpInst tex{(23u<<24)|(15u<<9)|(4u<<13)|1u,1u|identity|(7u<<18),0,0,{},false};
 std::array<FpContext,4> quad;
 for(unsigned l=0;l<4;++l){quad[l].textures=&textures;quad[l].inputs[4]={0.25f+0.5f*(l&1),0.25f+0.5f*(l>>1),0,1};}
 run_fragment_quad({tex},quad);
 for(unsigned l=0;l<4;++l){auto r=quad[l].read_r(0);assert(r[1]==1 && r[0]==0);}
 FpInst ddx{(21u<<24)|(15u<<9)|(4u<<13)|1u,1u|identity|(7u<<18),0,0,{},false};
 run_fragment_quad({ddx},quad);assert(quad[3].read_r(0)[0]==0.5f && quad[3].read_r(0)[1]==0);
''')
