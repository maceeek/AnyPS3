"""MFC DMA list stall-and-notify: SN event, MFC_RdListStallStat (25), MFC_WrListStallAck (26), tag completion.

Expectations follow the Cell BE Handbook v1.1 list element layout (PDF p.530: bit 0 of the first word = stall-and-notify)
and RPCS3 ffba2bf0 SPUThread.cpp do_list_transfer / MFC_RdListStallStat / MFC_WrListStallAck: the list stalls after the
flagged element only if elements remain, SN is raised when the stall status was empty, reading channel 25 returns and
clears the stalled tags, acknowledging resumes the list. ACIT run 350load stopped with "unimplemented SPU event mask 2".
"""
import shutil
import unittest

from tests import test_runtime_objects


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class SpuListStallTests(unittest.TestCase):
    def test_stall_notify_status_ack_resume_and_tag_completion(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;NativeSpu::Thread t;std::vector<std::uint8_t> storage(0x40000);auto* ls=storage.data();
 for(unsigned i=0;i<0x100;i+=4)mem.store(0x10100+i,4,0xa0000000u+i);
 auto element=[&](unsigned index,std::uint32_t lts,std::uint32_t ea){
   for(unsigned b=0;b<4;++b){ls[0x1000+index*8+b]=lts>>(24-8*b);ls[0x1000+index*8+4+b]=ea>>(24-8*b);}};
 auto word=[&](unsigned a){return spu_load_word(ls+a);};
 auto blocked=[&](unsigned channel){bool hit=false;try{spu_read_channel(s,t,channel);}catch(const SpuBlocked&){hit=true;}assert(hit);};
 auto list=[&](unsigned count,unsigned tag){spu_write_channel(s,t,ls,16,0x2000);spu_write_channel(s,t,ls,17,0);
   spu_write_channel(s,t,ls,18,0x1000);spu_write_channel(s,t,ls,19,count*8);spu_write_channel(s,t,ls,20,tag);spu_write_channel(s,t,ls,21,0x44);};
 spu_write_channel(s,t,ls,1,0x2);assert(t.event_mask==0x2); // SN is a valid event mask now
 // Three 16-byte elements, stall after the first: only element 0 lands before the acknowledgement.
 element(0,0x80000010u,0x10100);element(1,0x10,0x10110);element(2,0x10,0x10120);
 assert(spu_channel_count(s,t,25)==0);list(3,5);
 assert(word(0x2000)==0xa0000000u && word(0x2010)==0 && word(0x2020)==0);
 assert((t.event_status&0x2) && t.event_ready && spu_channel_count(s,t,25)==1);
 spu_write_channel(s,t,ls,22,1u<<5);spu_write_channel(s,t,ls,23,0);assert(spu_read_channel(s,t,24)==0); // immediate: incomplete
 spu_write_channel(s,t,ls,23,1);assert(spu_channel_count(s,t,24)==0);blocked(24);                        // ANY waits
 assert(spu_read_channel(s,t,25)==(1u<<5) && spu_channel_count(s,t,25)==0);blocked(25);
 spu_write_channel(s,t,ls,2,0x2);assert(!(t.event_status&0x2));
 spu_write_channel(s,t,ls,26,7);assert(word(0x2010)==0); // acknowledging a non-stalled tag does nothing
 spu_write_channel(s,t,ls,26,5);
 assert(word(0x2010)==0xa0000010u && word(0x2020)==0xa0000020u && !(t.event_status&0x2));
 assert(spu_channel_count(s,t,24)==1 && spu_read_channel(s,t,24)==(1u<<5));
 // Two stall points; SN only when the status was empty; the flag on the last element never stalls.
 for(unsigned i=0x2000;i<0x2040;++i)ls[i]=0;
 element(0,0x80000010u,0x10100);element(1,0x80000010u,0x10110);element(2,0x80000010u,0x10120);
 list(3,9);assert((t.event_status&0x2) && word(0x2010)==0);
 spu_write_channel(s,t,ls,2,0x2);spu_write_channel(s,t,ls,26,9);           // status not read: still non-empty
 assert(word(0x2010)==0xa0000010u && word(0x2020)==0 && !(t.event_status&0x2));
 assert(spu_read_channel(s,t,25)==(1u<<9));
 spu_write_channel(s,t,ls,26,9);assert(word(0x2020)==0xa0000020u && spu_channel_count(s,t,25)==0);
 spu_write_channel(s,t,ls,22,1u<<9);spu_write_channel(s,t,ls,23,2);assert(spu_read_channel(s,t,24)==(1u<<9)); // ALL done
 // A second list on a stalled tag would need an MFC queue model: explicit stop, not a silent reorder.
 element(0,0x80000010u,0x10100);element(1,0x10,0x10110);list(2,3);
 fault([&]{list(2,3);});
''')


if __name__ == '__main__':
    unittest.main()
