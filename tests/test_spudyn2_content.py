"""Generic SPU discovery/content cache: fixed bytes and native execution."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from anyps3.elf import ElfImage
from anyps3.errors import AnyPS3Error
from anyps3.spu_scan import discover_spu_images, cache_from_dynamic_log, cached_spu_image, augment_spu_program
from tests import test_spu_block, test_runtime_objects


def original_elf(code=bytes.fromhex('1c01c387000000014020000040200000')):
    payload=bytearray(0x100)+code
    payload[:16]=b'\x7fELF\x01\x02\x01'+bytes(9)
    struct.pack_into('>HHIIIIIHHHHHH',payload,16,2,23,1,0x100,52,0,0,52,32,1,0,0,0)
    struct.pack_into('>8I',payload,52,1,0x100,0x100,0x100,len(code),len(code),5,4)
    return bytes(payload)


def record(data,base=0x10680):
    return dict(schema_version=1,spu_id=0x200,ls_base=base,pc=base,size=len(data),
                word=int.from_bytes(data[:4],'big'),sha256=hashlib.sha256(data).hexdigest(),data_hex=data.hex(),last_dma=[])


class ContentTests(unittest.TestCase):
    def test_find_embedded_elf_from_declared_headers_only_and_deduplicate(self):
        raw=original_elf();images,evidence=discover_spu_images([('prx',b'prefix!'+raw+b'noise'),('other',raw)])
        self.assertEqual(len(images),1)
        self.assertEqual(evidence[0]['offset'],7)
        self.assertEqual(evidence[0]['source_sha256'],hashlib.sha256(b'prefix!'+raw+b'noise').hexdigest())
        self.assertEqual(images[0].entry,0x100)
        self.assertEqual(images[0].read_virtual(0x100,4,executable=True),bytes.fromhex('1c01c387'))
        self.assertEqual(evidence[0]['size'],0x110)
        for field,value in [(18,21),(24,0x101),(60,0x40000),(68,0x40004)]:
            bad=bytearray(raw)
            if field==18:struct.pack_into('>H',bad,field,value)
            else:struct.pack_into('>I',bad,field,value)
            self.assertEqual(discover_spu_images([('bad',bad)])[0],())
        self.assertEqual(discover_spu_images([('truncated',raw[:-1])])[0],())
        # Raw code without an ELF/job header is not silently identified.
        self.assertEqual(discover_spu_images([('job',raw[0x100:])])[0],())

    def test_discovery_accepts_unaligned_short_data_and_bss(self):
        raw=bytearray(original_elf())
        struct.pack_into('>H',raw,44,2)
        struct.pack_into('>8I',raw,84,1,0x110,0x8001,0x8001,3,5,6,1)
        raw.extend(bytes.fromhex('abcde0'))
        images,_=discover_spu_images([('original',raw)])
        self.assertEqual(len(images),1)
        self.assertEqual(images[0].read_virtual(0x8001,3),bytes.fromhex('abcde0'))
        self.assertEqual(images[0].segments[1].memory_size,5)

    @unittest.skipUnless(shutil.which('c++'),'Requires C++')
    def test_cache_only_translation_is_not_accepted_as_a_boot_image(self):
        data=bytes.fromhex('1c004183')*16
        cache=dict(schema_version=1,kind='spu_observed_cells',cells=[dict(sha256=hashlib.sha256(data).hexdigest(),data_hex=data.hex())])
        images,overlays=cached_spu_image(cache)
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);
 const std::uint8_t word[]={0x1c,0,0x41,0x83};
 for(unsigned n=0;n<16;++n)std::copy_n(word,4,storage.data()+4*n);
 fault([&]{spu_prepare(s,t,storage.data());});
 assert(t.image_index==UINT32_MAX && !t.steps);
 t.image_index=0;t.pc=0;spu_step(s,t,storage.data(),16);
 assert(t.steps==16 && t.pc==64 && spu_word(t.gpr[3],0)==16);
''',spu_images=images,spu_overlays=overlays)

    def test_cache_has_content_identity_across_lanes_and_addresses(self):
        data=bytes.fromhex('1c01c387000000014020000040200000')*4
        rows=[record(data),record(data,0x100),record(data,0x3ffc0)]
        rows[1]['spu_id']=0x3000200
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'log';p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
            cache=cache_from_dynamic_log(p)
            self.assertEqual(cache['cells'],[dict(sha256=hashlib.sha256(data).hexdigest(),data_hex=data.hex())])
            images,overlays=cached_spu_image(cache)
            self.assertEqual(len(images),1);self.assertEqual(images[0].type,0)
            self.assertEqual(overlays,(((0,data),),))
            for key,value in [('sha256','0'*64),('pc',3),('ls_base',0x40000),('word',0),('schema_version',2)]:
                bad=dict(rows[0]);bad[key]=value;p.write_text(json.dumps(bad)+'\n')
                with self.assertRaises(AnyPS3Error):cache_from_dynamic_log(p)
            p.write_text('{')
            with self.assertRaises(AnyPS3Error):cache_from_dynamic_log(p)
            cache['cells'][0]['data_hex']='00'*64
            with self.assertRaises(AnyPS3Error):cached_spu_image(cache)

    def test_build_adapter_preserves_profile_and_never_replaces_guest_bytes(self):
        first=ElfImage.parse(original_elf())
        images,overlays,evidence=augment_spu_program((first,),(((0x200,b'\0'*16),),),[('decoded',original_elf()+b'debug')])
        self.assertEqual(images,(first,));self.assertEqual(overlays,(((0x200,b'\0'*16),),))
        self.assertEqual(len(evidence),1)

    @unittest.skipUnless(shutil.which('c++'),'Requires C++')
    def test_native_cross_image_zero_and_nonzero_relocation_mutation_fallback(self):
        boot=test_spu_block.image({0x100:0x1c004183,0x104:1})
        other=ElfImage.parse(original_elf())
        test_runtime_objects.RuntimeObjectsTests().run_native(test_spu_block.setup(boot)+r'''
 const std::uint8_t code[]={0x1c,1,0xc3,0x87,0,0,0,1,0x40,0x20,0,0,0x40,0x20,0,0};
 for(unsigned base:{0x10680u,0x100u}){
  std::copy_n(code,16,ls+base);t.pc=base;t.steps=0;t.gpr={};t.reloc_delta=0;t.reloc_image_index=UINT32_MAX;
  assert(spu_run(s,10) && t.steps==2 && t.pc==base+8);
  // Content dispatch may serve this from image 1 without a relocation lookup.
  assert(t.image_index==0 && spu_word(t.gpr[7],0)==7);
  assert(t.interpreted_instructions.value.load()==0);
 }
 ls[0x101]=2;ls[0x102]=3; // AI r7,r7,8, not the translated +7.
 t.pc=0x100;assert(spu_run(s,1) && t.steps==3 && spu_word(t.gpr[7],0)==15);
 assert(t.interpreted_instructions.value.load()==1);
 ls[0x100]=2;ls[0x101]=ls[0x102]=ls[0x103]=0;t.pc=0x100;
 fault([&]{spu_run(s,10);});assert(t.steps==3 && t.pc==0x100);
''',spu_images=(boot,other))

    @unittest.skipUnless(shutil.which('c++'),'Requires C++')
    def test_native_observed_cache_result_at_two_bases_and_exact_hard_limit(self):
        boot=test_spu_block.image({0x100:1})
        data=bytes.fromhex('1c014387')*15+bytes.fromhex('00000001')  # AI r7,r7,5; STOP yield
        cache=dict(schema_version=1,kind='spu_observed_cells',cells=[dict(sha256=hashlib.sha256(data).hexdigest(),data_hex=data.hex())])
        images,overlays,_=augment_spu_program((boot,),(),cache=cache)
        body=test_spu_block.setup(boot)+'const std::uint8_t data[]={'+','.join(map(str,data))+'};'+r'''
 for(unsigned base:{0x8000u,0x10680u}){
  std::copy_n(data,64,ls+base);t.pc=base;t.steps=0;t.gpr={};t.reloc_delta=0;t.reloc_image_index=UINT32_MAX;
  spu_step(s,t,ls,100,9);assert(t.steps==9 && t.pc==base+36 && spu_word(t.gpr[7],0)==45);
  fault([&]{spu_step(s,t,ls,1,9);});assert(t.steps==9);
  assert(spu_run(s,7) && t.steps==16 && t.pc==base+64 && spu_word(t.gpr[7],0)==75);
  // The 15-word block crosses the exact hard limit 9: the interpreter retires the prefix.
  assert(t.image_index==0);
 }
'''
        test_runtime_objects.RuntimeObjectsTests().run_native(body,spu_images=images,spu_overlays=overlays)

    @unittest.skipUnless(shutil.which('c++'),'Requires C++')
    def test_interpreted_slice_interrupts_between_instructions_and_exact_stop(self):
        test_spu_block.SpuBlockTests().native({0x100:1},r'''
 auto put=[&](unsigned pc,std::uint32_t word){for(unsigned i=0;i<4;++i)ls[pc+i]=word>>(24-8*i);};
 // Handler IRET disables interrupts and retries the interrupted PC.
 put(0,0x35400000);
 put(0x20000,0x1c004183); // AI r3,r3,1
 put(0x20004,0x21a00084); // WRCH EventMask,r4
 put(0x20008,0x1c014183); // AI r3,r3,5
 put(0x2000c,1);
 t.pc=0x20000;t.event_status=0x400;t.event_ready=false;t.event_mask=0;t.interrupts_enabled=true;
 spu_word(t.gpr[4],0,0x400);
 assert(spu_run(s,3) && t.steps==3 && t.pc==0x20008);
 assert(spu_word(t.gpr[3],0)==1 && !t.interrupts_enabled && t.srr0==0x20008);
 assert(spu_run(s,2) && t.steps==5 && t.pc==0x20010 && spu_word(t.gpr[3],0)==6);
 assert(t.interpreted_instructions.value.load()==5);
''')


if __name__=='__main__':unittest.main()
