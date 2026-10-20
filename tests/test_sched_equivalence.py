"""ppu_schedule / sync_ext_poll: the optimized scheduler makes the same decisions as the previous implementation.

REFERENCE_* below is the scheduler exactly as it was in b4547ab (anyps3/threads.py), renamed *_reference. A generated program
contains both the shipped functions and the reference; random thread/kernel-object populations are driven through identical
random mutations (semaphore posts, event sends, mutex release, expired deadlines, flag/rwlock state, thread exits, priority
ties) and after every scheduling call both states must agree on the running thread, every thread's wait state and every
sync_ext wait/flag/rwlock. Also pins the cache-line locality of the scan fields and the event-queue event counter.
"""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

from anyps3.program import compile_program
from anyps3.prx import make_image

REFERENCE_POLL = r'''
static void sync_ext_poll_reference(PpuState& s) {
    auto& x=s.sync_ext;const auto now=sync_ext_now();
    for(auto& pair:x.waits){
        auto& w=pair.second;
        for(const auto& t:s.ppu_threads)if(t.thread_id==pair.first)w.priority=t.priority;
        if(!w.done && w.deadline && now>=w.deadline){
            if(w.kind==9)for(const auto& f:x.flags)if(f.id==w.id)w.pattern=f.pattern;
            sync_ext_finish(s,pair.first,w,w.kind==12?0:0x8001000b);
        }
    }
    auto before=[](const auto& a,const auto& b,unsigned protocol){
        return protocol==2 && a.priority!=b.priority?a.priority<b.priority:a.sequence<b.sequence;
    };
    for(auto& f:x.flags){
        // Consume patterns in queue order at wake time; runnable threads cannot
        // steal a bit from a selected waiter while it reacquires the CPU.
        std::vector<std::uint32_t> queue;
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==9 && pair.second.id==f.id)queue.push_back(pair.first);
        std::sort(queue.begin(),queue.end(),[&](auto a,auto b){return before(x.waits.at(a),x.waits.at(b),f.protocol);});
        for(auto tid:queue){auto& w=x.waits.at(tid);const auto pattern=f.pattern;
            if(sync_ext_match(f.pattern,w.bits,w.mode)){w.pattern=pattern;sync_ext_finish(s,tid,w,0);}
        }
    }
    for(auto& l:x.rwlocks){
        if(l.writer)continue;
        std::uint32_t writer=0;
        for(const auto& pair:x.waits)if(!pair.second.done && pair.second.kind==11 && pair.second.id==l.id)
            if(!writer || before(pair.second,x.waits.at(writer),l.protocol))writer=pair.first;
        if(writer){
            if(!l.readers){l.writer=writer;sync_ext_finish(s,writer,x.waits.at(writer),0);}
            continue;
        }
        for(auto& pair:x.waits)if(!pair.second.done && pair.second.kind==10 && pair.second.id==l.id){
            ++l.readers;sync_ext_finish(s,pair.first,pair.second,0);
        }
    }
}

'''

REFERENCE_SCHEDULE = r'''
[[maybe_unused]] static void ppu_schedule_reference(PpuState& s,bool mandatory=false) {
    // Scheduler predicates use validated scalar memory only. Each nested
    // RSX/audio engine takes its own compatibility scope; do not exclude SPU
    // DMA for the whole scheduling loop or while choosing a ready context.
    SpuServiceScope owner(s,true);
    ExecutionScope scheduling(s.execution,ExecutionPhase::Coordinator);
    if(s.module_loading)throw std::runtime_error("PPU scheduling during module load is unsupported");
    if(mandatory){s.process.spu_worker.refresh_pending=true;spu_run(s,1024);rsx_run(s,1024);}
    auto active=std::find_if(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& t){return t.thread_id==s.thread_id;});
    if(active==s.ppu_threads.end()){s.ppu_threads.push_back(static_cast<PpuContext&>(s));active=std::prev(s.ppu_threads.end());}
    else *active=static_cast<PpuContext&>(s);
    for(;;){
    audio_run(s);
    sync_ext_poll_reference(s);
    for(auto& thread:s.ppu_threads){
        bool ready=false;
        if(thread.wait_kind==1)for(const auto& q:s.events.queues)if(q.id==thread.wait_id && !q.data.empty())ready=true;
        if(thread.wait_kind==2)for(const auto& sem:s.kernel.semaphores)if(sem.id==thread.wait_id && sem.value)ready=true;
        if(thread.wait_kind==3)for(const auto& mutex:s.kernel.mutexes)if(mutex.id==thread.wait_id && !mutex.owner)ready=true;
        // An expired timed mutex wait resumes; the retried lock reports ETIMEDOUT
        // unless the mutex became free (RPCS3 sys_mutex.cpp:247-290).
        if(!ready && (thread.wait_kind==1 || thread.wait_kind==3 || thread.wait_kind==7 || thread.wait_kind==8) &&
           thread.wait_deadline && sync_ext_now()>=thread.wait_deadline){thread.wait_timed_out=true;ready=true;}
        if(thread.wait_kind==5)for(const auto& group:s.spu.groups)if(group.id==thread.wait_id && group.join_cause)ready=true;
        if(thread.wait_kind==6)for(const auto& target:s.ppu_threads)if(target.thread_id==thread.wait_id && target.returned)ready=true;
        if(thread.wait_kind==7 && s.mutexes.count(thread.wait_id) && s.memory.load(thread.wait_id,4)==UINT32_MAX)ready=true;
        if(thread.wait_kind==13 && std::none_of(s.ppu_threads.begin(),s.ppu_threads.end(),[&](const auto& other){
            return other.thread_id!=thread.thread_id && other.atexit_running;}))ready=true;
        if(ready)thread.wait_kind=0;
    }
    PpuContext* next=nullptr;
    // Rotate equal priorities; a smaller number is a higher LV2 priority.
    const auto current=static_cast<std::size_t>(active-s.ppu_threads.begin());
    for(unsigned offset=1;offset<=s.ppu_threads.size();++offset){
        auto& candidate=s.ppu_threads[(current+offset)%s.ppu_threads.size()];
        if(candidate.wait_kind || candidate.returned)continue;
        if(!next || candidate.priority<next->priority)next=&candidate;
    }
    // The Cell PPU has two hardware threads: LV2 runs the two highest-priority
    // ready threads at once, so a busy higher-priority thread cannot starve the
    // next one. On a preemption point (not a block) alternate between the best
    // two ready contexts, which serially approximates both hardware threads.
    if(!mandatory && next && !active->wait_kind && !active->returned){
        PpuContext* second=nullptr;
        for(unsigned offset=1;offset<=s.ppu_threads.size();++offset){
            auto& candidate=s.ppu_threads[(current+offset)%s.ppu_threads.size()];
            if(&candidate==next || candidate.wait_kind || candidate.returned)continue;
            if(!second || candidate.priority<second->priority)second=&candidate;
        }
        if(second && &*active==next)next=second;
        else if(second && &*active==second)next=next;
    }
    if(!next){
        if(mandatory){
            // A timed PPU sleep can leave no instruction ticks for milliseconds.
            // Preserve the SPU scheduler deadline while the coordinator waits.
            s.process.spu_worker.refresh_pending=true;
            const bool spu_progress=spu_run(s,1024),rsx_progress=rsx_run(s,1024);
            if(spu_progress || rsx_progress)continue;
            // Idle until the earliest timed wake-up (at most 1 ms, so vblank and
            // other host-clock events are still polled). Active audio retains
            // its 100 us service bound, but cannot hide an earlier PPU deadline.
            std::uint64_t earliest=UINT64_MAX;
            for(const auto& pair:s.sync_ext.waits)if(!pair.second.done && pair.second.deadline)earliest=std::min(earliest,pair.second.deadline);
            for(const auto& thread:s.ppu_threads)if(thread.wait_kind && thread.wait_deadline)earliest=std::min(earliest,thread.wait_deadline);
            const bool audio_pending=s.audio.maximum && (s.audio.running || !s.audio.notifications.empty());
            if(earliest!=UINT64_MAX || audio_pending){const auto now=sync_ext_now();
                const std::uint64_t ceiling=audio_pending?100:1000;
                const auto delay=earliest==UINT64_MAX?ceiling:earliest>now?std::min(earliest-now,ceiling):0;
                if(delay){ExecutionScope idle(s.execution,ExecutionPhase::Idle);GuestMemory::RawYield yield(s.memory);std::this_thread::sleep_for(std::chrono::microseconds(delay));}
                continue;}
            if(s.process.spu_worker.enabled && std::any_of(s.spu.groups.begin(),s.spu.groups.end(),[](const auto& g){return g.state==6 || g.state==3;})){
                ExecutionScope idle(s.execution,ExecutionPhase::Idle);{GuestMemory::RawYield yield(s.memory);s.process.spu_worker.service();s.process.spu_worker.idle_wait();}continue;
            }
            if(s.process.rsx_worker.enabled){
                ExecutionScope idle(s.execution,ExecutionPhase::RsxWait);GuestMemory::RawYield yield(s.memory);
                rsx_worker_pump(s);std::this_thread::sleep_for(std::chrono::microseconds(100));continue;
            }
            throw std::runtime_error("native scheduler has no runnable PPU/SPU context or progressing RSX command");
        }
        return;
    }
    // Per-switch trace only on request: ~2000 switches/s in a level made it a stderr hot path.
    static const bool trace_switch=[]{const char* v=std::getenv("ANYPS3_TRACE_PPU_SWITCH");return v && std::string(v)=="1";}();
    if(trace_switch && next->thread_id!=s.thread_id)std::fprintf(stderr,"PPU switch %u -> %u pc=%08x\n",s.thread_id,next->thread_id,next->pc);
    static_cast<PpuContext&>(s)=*next;s.reschedule=false;
    return;
    }
}

'''

HARNESS = r'''
#include <cassert>
#include <sstream>
struct Rng {
    std::uint64_t x;
    std::uint32_t next(){x=x*6364136223846793005ULL+1442695040888963407ULL;return std::uint32_t(x>>33);}
    std::uint32_t below(std::uint32_t n){return next()%n;}
};
static void build(PpuState& s,std::uint64_t seed,std::uint64_t now){
    Rng r{seed};
    s.memory.map(0x10000,0x1000,6,nullptr,0);
    for(unsigned i=0;i<4;++i)s.kernel.semaphores.push_back({0x96000000u+i*0x100,r.below(2),4});
    for(unsigned i=0;i<3;++i)s.events.queues.push_back({0x8d000000u+i*0x100,4,0x1000+i});
    // kernel.mutexes already holds three system mutexes (ids 0x85000000, 0x85000100, 0x85000200)
    for(auto& m:s.kernel.mutexes)m.owner=r.below(2)?0:7;
    for(unsigned i=0;i<2;++i)s.sync_ext.flags.push_back({0x98000000u+i*0x100,1+r.below(2),0,1,0,r.next()&0xf});
    for(unsigned i=0;i<2;++i)s.sync_ext.rwlocks.push_back({0x88000000u+i*0x100,1+r.below(2),1,0,r.below(2),0});
    for(unsigned i=0;i<2;++i){const auto a=0x10000+i*16;s.mutexes.insert(a);s.memory.store(a,4,r.below(2)?UINT32_MAX:5);}
    const unsigned count=2+r.below(8);
    const unsigned priorities[]={0,1,1,2,5};
    for(unsigned t=1;t<=count;++t){
        PpuContext c;c.thread_id=t;c.priority=priorities[r.below(5)];c.pc=0x10000+t*4;
        const unsigned roll=t==1?0:r.below(16);
        switch(roll){
        case 1:case 2:c.wait_kind=1;c.wait_id=0x8d000000u+r.below(3)*0x100;break;
        case 3:case 4:c.wait_kind=2;c.wait_id=0x96000000u+r.below(4)*0x100;break;
        case 5:c.wait_kind=3;c.wait_id=0x85000000u+r.below(3)*0x100;break;
        case 6:c.wait_kind=6;c.wait_id=1+r.below(count);break;
        case 7:c.wait_kind=7;c.wait_id=0x10000+r.below(2)*16;break;
        case 8:c.wait_kind=8;c.wait_id=0x4000+r.below(3);break;
        case 9:c.wait_kind=13;break;
        case 10:c.wait_kind=4;c.wait_id=0x86000b00;break;
        case 11:case 12:case 13:{ // sync_ext waiter: flag (9), rwlock reader/writer (10/11, favoured), sleep (12)
            const unsigned kind=r.below(2)?11:9+r.below(4);c.wait_kind=kind;
            const std::uint32_t id=kind==9?0x98000000u+r.below(2)*0x100:kind==12?0:0x88000000u+r.below(2)*0x100;
            const std::uint64_t deadline=r.below(3)==0?now-5:r.below(3)==0?now+3600ull*1000000:0;
            s.sync_ext.waits.emplace(t,Lv2SyncExt::Wait{kind,id,c.priority,++s.sync_ext.sequence,deadline,1u<<r.below(4),r.below(48),0});
            break;}
        default:break;
        }
        if(c.wait_kind==1 || c.wait_kind==3 || c.wait_kind==7 || c.wait_kind==8)
            c.wait_deadline=r.below(3)==0?now-5:r.below(3)==0?now+3600ull*1000000:0;
        c.atexit_running=r.below(8)==0;
        c.returned=t!=1 && r.below(10)==0 && !c.wait_kind;
        s.ppu_threads.push_back(c);
    }
    // Priorities change after the waits were armed (setschedparam): queue order must follow the current priority.
    for(auto& t:s.ppu_threads)if(r.below(3)==0)t.priority=priorities[r.below(5)];
    s.thread_id=1;static_cast<PpuContext&>(s)=s.ppu_threads[0];
}
static void mutate(PpuState& s,Rng& r){
    switch(r.below(14)){
    case 0:s.kernel.semaphores[r.below(4)].value=r.below(2);break;
    case 1:{auto& q=s.events.queues[r.below(3)];if(r.below(2))q.data.send({1,2,3,4});else q.data.clear();break;}
    case 2:s.kernel.mutexes[r.below(3)].owner=r.below(2)?0:9;break;
    case 3:for(auto& t:s.ppu_threads)if(t.wait_kind && r.below(3)==0)t.wait_deadline=1;break;
    case 4:s.sync_ext.flags[r.below(2)].pattern=r.next()&0xf;break;
    case 5:{auto& l=s.sync_ext.rwlocks[r.below(2)];l.writer=r.below(2)?0:3;l.readers=r.below(2);break;}
    case 6:{auto& t=s.ppu_threads[1+r.below(s.ppu_threads.size()-1)];if(!t.wait_kind)t.returned=r.below(2);break;}
    case 7:s.memory.store(0x10000+r.below(2)*16,4,r.below(2)?UINT32_MAX:5);break;
    case 8:{auto& t=s.ppu_threads[r.below(s.ppu_threads.size())];t.atexit_running=r.below(2);break;}
    case 9:s.reschedule=true;break;
    case 10:for(auto& pair:s.sync_ext.waits)if(r.below(3)==0)pair.second.deadline=1;break;
    case 11:{ // priority change (setschedparam): the queued waits keep their arm-time priority until the next poll refreshes it
        const unsigned priorities[]={0,1,1,2,5};auto& t=s.ppu_threads[r.below(s.ppu_threads.size())];t.priority=priorities[r.below(5)];
        if(t.thread_id==s.thread_id){s.priority=t.priority;}
        break;}
    default:break;
    }
}
static std::string dump(PpuState& s){
    std::ostringstream o;
    o<<"cur="<<s.thread_id<<" pc="<<s.pc<<" wk="<<s.wait_kind<<" resched="<<s.reschedule<<"\n";
    for(const auto& t:s.ppu_threads)o<<t.thread_id<<":"<<t.wait_kind<<","<<t.wait_id<<","<<t.wait_timed_out<<","<<t.returned<<","<<t.priority<<","<<t.wait_deadline<<","<<t.atexit_running<<" ";
    o<<"\n";
    for(const auto& pair:s.sync_ext.waits)o<<"w"<<pair.first<<":"<<pair.second.done<<","<<pair.second.status<<","<<pair.second.pattern<<" ";
    for(const auto& f:s.sync_ext.flags)o<<"f"<<f.pattern<<" ";
    for(const auto& l:s.sync_ext.rwlocks)o<<"l"<<l.writer<<","<<l.readers<<" ";
    return o.str();
}
int main(){
    {   // The lock-free event counter follows every mutation of the queue.
        Events::Queue q(0x8d000000u,4,0x99);
        assert(!q.data.has_events() && q.data.empty());
        assert(q.data.send({1,2,3,4})==0 && q.data.has_events() && q.data.size()==1);
        q.data.clear();assert(!q.data.has_events() && q.data.empty());
        assert(q.data.send({1,2,3,4})==0 && q.data.send({5,6,7,8})==0 && q.data.size()==2);
        q.data.erase(q.data.begin());assert(q.data.has_events() && q.data.size()==1 && q.data.front()[0]==5);
        q.data.erase(q.data.begin());assert(!q.data.has_events() && q.data.empty());
        Events::Queue full(0x8d000100u,1,0x9a,{{9,9,9,9}});assert(full.data.has_events());
        assert(full.data.send({1,1,1,1})==0x8001000a && full.data.size()==1);
    }
    {   // Directed: queue order follows the CURRENT thread priority, not the one stored when the wait was armed.
        for(int variant=0;variant<2;++variant){
            PpuState s;
            for(unsigned t=1;t<=3;++t){PpuContext c;c.thread_id=t;c.priority=t==2?0:t==3?5:1;c.pc=0x10000;s.ppu_threads.push_back(c);}
            s.thread_id=1;static_cast<PpuContext&>(s)=s.ppu_threads[0];
            // Armed with the opposite priorities: thread 2 stored 5, thread 3 stored 0.
            const unsigned kind=variant==0?11:9;
            if(variant==0)s.sync_ext.rwlocks.push_back({0x88000000u,2,1,0,0,0});
            else s.sync_ext.flags.push_back({0x98000000u,2,0,1,0,1});
            const std::uint32_t id=variant==0?0x88000000u:0x98000000u;
            s.sync_ext.waits.emplace(2,Lv2SyncExt::Wait{kind,id,5,1,0,1,16,0});
            s.sync_ext.waits.emplace(3,Lv2SyncExt::Wait{kind,id,0,2,0,1,16,0});
            s.ppu_threads[1].wait_kind=kind;s.ppu_threads[2].wait_kind=kind;
            ppu_schedule(s);
            if(variant==0)assert(s.sync_ext.rwlocks[0].writer==2);              // priority 0 beats 5 although it armed later
            else assert(s.sync_ext.waits.at(2).done && !s.sync_ext.waits.at(3).done); // the single flag bit goes to priority 0
        }
    }
    unsigned switches=0,wakes=0;
    for(std::uint64_t seed=1;seed<=400;++seed){
        PpuState a,b;const auto now=sync_ext_now();build(a,seed*7919,now);build(b,seed*7919,now);
        Rng ra{seed*104729},rb{seed*104729};
        for(unsigned step=0;step<60;++step){
            mutate(a,ra);mutate(b,rb);
            const bool active_blocked=ra.below(10)==0;rb.below(10);
            if(active_blocked){a.wait_kind=2;a.wait_id=0x96000000u;b.wait_kind=2;b.wait_id=0x96000000u;}
            // A blocked active context needs another runnable one for the (non-sleeping) mandatory pass; otherwise skip it.
            bool mandatory=false;
            if(active_blocked){bool other=false;for(const auto& t:a.ppu_threads)other|=t.thread_id!=a.thread_id && !t.wait_kind && !t.returned;mandatory=other;
                if(!other){a.wait_kind=0;b.wait_kind=0;}}
            const auto before=dump(a);
            ppu_schedule(a,mandatory);ppu_schedule_reference(b,mandatory);
            const auto da=dump(a),db=dump(b);
            if(da!=db){std::fprintf(stderr,"MISMATCH seed=%llu step=%u mandatory=%d\nbefore: %s\nnew:    %s\nref:    %s\n",
                (unsigned long long)seed,step,int(mandatory),before.c_str(),da.c_str(),db.c_str());return 1;}
            switches+=a.thread_id!=1;wakes+=before!=da;
        }
    }
    std::printf("ok switches=%u changes=%u\n",switches,wakes);
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SchedulerEquivalence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        image = make_image([(0x10000, 5, struct.pack('>I', 0x4e800020)), (0x40000, 6, struct.pack('>II', 0x10000, 0))], entry=0x40000)
        source = compile_program(image, process_entry=True).cpp().replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)')
        marker = '[[maybe_unused]] static void ppu_schedule(PpuState& s,bool mandatory=false) {'
        position = source.index(marker)
        # The reference goes right before the shipped scheduler: everything it calls is already defined there.
        source = source[:position] + REFERENCE_POLL + REFERENCE_SCHEDULE + source[position:] + HARNESS
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        (root / 'test.cpp').write_text(source)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-function', '-pthread',
                                str(root / 'test.cpp'), '-o', str(root / 'test')], capture_output=True, text=True, timeout=900)
        assert build.returncode == 0, build.stderr[-6000:]
        cls.binary = str(root / 'test')

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_decisions_match_the_previous_scheduler(self):
        run = subprocess.run([self.binary], capture_output=True, text=True, timeout=300,
                             env={**os.environ, 'ANYPS3_PERF_REPORT': '0'})
        self.assertEqual(run.returncode, 0, run.stderr[-4000:])
        self.assertIn('ok', run.stdout)
        # The random drive must really exercise switches and state changes, or the comparison would be vacuous.
        fields = dict(item.split('=') for item in run.stdout.split()[1:])
        self.assertGreater(int(fields['switches']), 500)
        self.assertGreater(int(fields['changes']), 500)


if __name__ == '__main__':
    unittest.main()
