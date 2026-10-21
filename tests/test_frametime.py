"""FRAMETIME ring (anyps3/frametime.py): off by default, SIGKILL-safe file, exact event fields, ring wrap, thread safety.

The C++ block is compiled alone (it only needs the standard library; `block()` is a template over the guest state), run with
ANYPS3_FRAMETIME pointing into a temp dir and killed with SIGKILL, so the Python reader proves the data are in the MAP_SHARED file
without any exit-time flush. Expectations are written independently of the implementation (explicit per-thread sequence numbers).
"""
from pathlib import Path
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import unittest

from anyps3.frametime import CPP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'bench' / 'frametime'))
import ftring  # noqa: E402

PROGRAM = r'''
#include <chrono>
#include <atomic>
#include <csignal>
#include <string>
#include <thread>
#include <vector>
''' + CPP + r'''
struct FakeMemory {std::uint64_t load(std::uint64_t address,unsigned size)const{(void)size;return address==0x1230 ? 0xCAFEF00Du : 0;}};
struct FakeState {std::uint64_t r[32]{};std::uint64_t lr=0,pc=0;unsigned thread_id=1;FakeMemory memory;};
int main(int argc,char** argv){
    const std::string mode=argc>1?argv[1]:"";
    if(mode=="off"){
        // Not enabled: nothing is recorded or created, the stamp calls are no-ops.
        anyps3_ft::mark(anyps3_ft::Mark,1,2,3,4);
        {anyps3_ft::Span span(anyps3_ft::Idle,1);}
        FakeState state;anyps3_ft::block(state,12,0);
        std::printf("on=%d\n",int(anyps3_ft::on));return 0;
    }
    if(mode=="threads"){
        // 4 threads x 2000 sequence-numbered marks; the ring is large enough to keep all of them.
        std::vector<std::thread> threads;
        for(std::uint64_t t=0;t<4;++t)threads.emplace_back([t]{for(std::uint64_t i=0;i<2000;++i)anyps3_ft::mark(anyps3_ft::Mark,t,i,t*100000+i);});
        for(auto& t:threads)t.join();
        std::raise(SIGKILL);
    }
    if(mode=="wrap"){
        for(std::uint64_t i=0;i<3000;++i)anyps3_ft::mark(anyps3_ft::Mark,i);
        std::raise(SIGKILL);
    }
    if(mode=="torn"){
        // A writer claims its slot, stores part of the fields and is killed before it commits (SIGKILL mid-write).
        anyps3_ft::mark(anyps3_ft::Mark,1);
        const auto index=anyps3_ft::reserve();
        anyps3_ft::Event* e=anyps3_ft::claim(index);
        e->ns=123456789;e->kind=anyps3_ft::Mark;e->tid=77;e->a=0xBAD;       // b, c, d never written, no commit
        anyps3_ft::mark(anyps3_ft::Mark,2);
        std::raise(SIGKILL);
    }
    if(mode=="stale" || mode=="stall"){
        // Writer A holds index 0 while 2500 events (ring capacity 1024: two and a half laps) are written by the main thread.
        // "stale": A only reserved the index and claims after the wraps; "stall": A claimed the slot and fills it after the wraps.
        std::atomic<int> phase{0};
        std::thread a([&]{
            const auto index=anyps3_ft::reserve();
            anyps3_ft::Event* e=mode=="stall" ? anyps3_ft::claim(index) : nullptr;
            phase=1;while(phase!=2)std::this_thread::yield();
            if(mode=="stale")e=anyps3_ft::claim(index);
            std::printf("a_claimed=%d\n",e?1:0);std::fflush(stdout);
            if(e){e->ns=77;e->kind=anyps3_ft::Mark;e->tid=1;e->a=0xBEEF;e->b=e->c=e->d=0xBEEF;anyps3_ft::commit(e,index);}
        });
        while(phase!=1)std::this_thread::yield();
        for(std::uint64_t n=0;n<2500;++n)anyps3_ft::mark(anyps3_ft::Mark,1000+n,0,1000+n);
        phase=2;a.join();
        std::printf("dropped=%llu\n",(unsigned long long)anyps3_ft::header->dropped.load());std::fflush(stdout);
        std::raise(SIGKILL);
    }
    if(mode=="stress"){
        // Contended wraps: 4 threads x 100000 events through 1024 slots; every event is self-consistent in a, b, c.
        std::vector<std::thread> threads;
        for(std::uint64_t t=0;t<4;++t)threads.emplace_back([t]{for(std::uint64_t i=0;i<100000;++i)anyps3_ft::mark(anyps3_ft::Mark,t,i,t*1000003+i);});
        for(auto& t:threads)t.join();
        std::printf("dropped=%llu\n",(unsigned long long)anyps3_ft::header->dropped.load());std::fflush(stdout);
        std::raise(SIGKILL);
    }
    if(mode=="fields"){
        anyps3_ft::mark(anyps3_ft::Vblank,11,22,33,44);
        {anyps3_ft::Span span(anyps3_ft::FifoRun,5,6,0,7);std::this_thread::sleep_for(std::chrono::milliseconds(20));span.a=55;span.c=66;}
        {anyps3_ft::Span span(anyps3_ft::Service,0,0,50000000);}       // shorter than min_ns: not recorded
        // wrap(): closure posted now, executed on another thread 30 ms later; records exec time and queue wait.
        auto closure=anyps3_ft::wrap(anyps3_ft::HostExec,77,[]{std::this_thread::sleep_for(std::chrono::milliseconds(10));});
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
        std::thread other([&]{closure();});other.join();
        FakeState state;state.lr=0x5c5734;state.pc=0x5c5730;state.thread_id=7;state.r[3]=50;state.r[26]=0x1230;
        anyps3_ft::block(state,12,999);     // usleep: b is the microsecond argument r3, not the id
        anyps3_ft::block(state,7,999);      // other kinds: b is the object id
        state.lr=0x1111;state.pc=0x5c5738;anyps3_ft::block(state,12,0);   // different pc: not peeked when ANYPS3_FRAMETIME_PEEK_PC is set
        state.pc=0x5c5734;state.lr=0x2222;anyps3_ft::block(state,12,0);   // pc+4 is peeked as well
        anyps3_ft::mark(anyps3_ft::GpuSubmit,1,2,3,anyps3_ft::pack("fifo_labels_long"));
        std::raise(SIGKILL);
    }
    return 3;
}
'''


@unittest.skipUnless(shutil.which('c++'), 'Requires a C++ compiler')
class FrameTimeRingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        root = Path(cls.dir.name)
        (root / 'ft.cpp').write_text(PROGRAM)
        build = subprocess.run(['c++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', str(root / 'ft.cpp'),
                                '-o', str(root / 'ft')], capture_output=True, text=True)
        assert build.returncode == 0, build.stderr
        cls.exe = str(root / 'ft')
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def run_mode(self, mode, name, with_path=True, **env):
        path = self.root / name
        e = {k: v for k, v in os.environ.items() if not k.startswith('ANYPS3_FRAMETIME')}
        e.update(env)
        if with_path:
            e['ANYPS3_FRAMETIME'] = str(path)
        return path, subprocess.run([self.exe, mode], capture_output=True, text=True, env=e, timeout=60)

    def test_off_by_default(self):
        path, result = self.run_mode('off', 'off.bin', with_path=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'on=0')
        self.assertFalse(path.exists())

    def test_threads_complete_and_sequenced_after_sigkill(self):
        path, result = self.run_mode('threads', 'threads.bin')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        header, events = ftring.read_ring(path)
        self.assertEqual(header['head'], 8000)
        self.assertEqual((header['version'], header['dropped'], header['missing']), (2, 0, 0))
        self.assertEqual(header['capacity'], 1 << 23)
        marks = [e for e in events if e.kind == ftring.MARK]
        self.assertEqual(len(marks), 8000)
        tids = {}
        for e in marks:
            tids.setdefault(e.a, set()).add(e.tid)
        # each logical thread has exactly one OS thread id, and all 4 are distinct
        self.assertTrue(all(len(v) == 1 for v in tids.values()))
        self.assertEqual(len({next(iter(v)) for v in tids.values()}), 4)
        for t in range(4):
            seq = [e.b for e in marks if e.a == t]          # events sorted by time: per-thread order is the emit order
            self.assertEqual(seq, list(range(2000)))
            self.assertTrue(all(e.c == t * 100000 + e.b for e in marks if e.a == t))

    def test_ring_wraps_and_keeps_newest(self):
        path, result = self.run_mode('wrap', 'wrap.bin', ANYPS3_FRAMETIME_EVENTS='1024')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        header, events = ftring.read_ring(path)
        self.assertEqual((header['head'], header['capacity'], header['count']), (3000, 1024, 1024))
        self.assertEqual([e.a for e in events], list(range(3000 - 1024, 3000)))

    def test_killed_mid_write_is_never_returned(self):
        path, result = self.run_mode('torn', 'torn.bin', ANYPS3_FRAMETIME_EVENTS='1024')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        header, events = ftring.read_ring(path)
        self.assertEqual((header['head'], header['dropped'], header['missing']), (3, 0, 1))
        self.assertEqual(sorted(e.a for e in events), [1, 2])       # the half-written third event (a == 0xBAD) is not accepted
        # raw bytes: the claimed slot carries the odd claim word 2*1+1, the committed ones 2*0+2 and 2*2+2
        raw = path.read_bytes()
        self.assertEqual([struct.unpack_from('<Q', raw, 64 + 64 * slot)[0] for slot in range(3)], [2, 3, 6])

    def test_delayed_writer_after_wraps_is_dropped(self):
        # writer A reserved index 0 and claims only after 2500 later events wrapped the 1024-slot ring twice
        path, result = self.run_mode('stale', 'stale.bin', ANYPS3_FRAMETIME_EVENTS='1024')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertEqual(result.stdout.split(), ['a_claimed=0', 'dropped=1'])
        header, events = ftring.read_ring(path)
        self.assertEqual((header['head'], header['dropped'], header['missing'], header['count']), (2501, 1, 0, 1024))
        # main's events have indices 1..2500 (value 1000 + index - 1); the window is indices 1477..2500, every one intact
        self.assertEqual(sorted(e.a for e in events), list(range(1000 + 1476, 1000 + 2500)))
        self.assertTrue(all(e.c == e.a for e in events))

    def test_writer_stalled_inside_a_slot_across_wraps_blocks_only_that_slot(self):
        # writer A owns slot 0 (odd claim word) while the ring wraps twice and only then fills and commits it
        path, result = self.run_mode('stall', 'stall.bin', ANYPS3_FRAMETIME_EVENTS='1024')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertEqual(result.stdout.split(), ['a_claimed=1', 'dropped=2'])     # indices 1024 and 2048 found the slot busy
        header, events = ftring.read_ring(path)
        self.assertEqual((header['head'], header['dropped'], header['missing'], header['count']), (2501, 2, 1, 1024))
        self.assertNotIn(0xBEEF, [e.a for e in events])             # A's stale event is not mixed into the newest laps
        expected = [1000 + index - 1 for index in range(1477, 2501) if index != 2048]
        self.assertEqual(sorted(e.a for e in events), expected)
        self.assertTrue(all(e.c == e.a and e.tid != 1 for e in events))

    def test_contended_wraps_never_mix_events(self):
        path, result = self.run_mode('stress', 'stress.bin', ANYPS3_FRAMETIME_EVENTS='1024')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        dropped = int(result.stdout.split('=')[1])
        header, events = ftring.read_ring(path)
        self.assertEqual((header['head'], header['count'], header['dropped']), (400000, 1024, dropped))
        self.assertEqual(len(events) + header['missing'], 1024)
        self.assertTrue(all(e.kind == ftring.MARK and e.c == e.a * 1000003 + e.b and e.a < 4 and e.b < 100000 for e in events))

    def test_reader_rejects_uncommitted_and_foreign_lap_slots_and_reads_version_1(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'hand.bin'
            capacity = 1024
            slots = bytearray(capacity * 64)

            def put(index, seq, a):
                struct.pack_into('<QQIIQQQQ', slots, (index & (capacity - 1)) * 64, seq, 1000 + index, ftring.MARK, 5, a, 0, 0, 0)

            head = 1030                                             # window = indices 6..1029
            for index in range(6, head):
                put(index, 2 * index + 2, index)
            put(10, 2 * 10 + 1, 10)                                 # odd: a writer is inside the slot
            put(20, 2 * (20 + capacity) + 2, 20)                    # committed by a LATER lap
            put(1025, 2 * (1025 - capacity) + 2, 1025)                    # committed by an EARLIER lap (the writer never arrived)
            put(40, 0, 40)                                          # never committed
            path.write_bytes(struct.pack('<8sQQQQ24x', b'ANYPSFT1', capacity, 2, head, 9) + bytes(slots))
            header, events = ftring.read_ring(path)
            self.assertEqual((header['version'], header['dropped'], header['missing']), (2, 9, 4))
            self.assertEqual(sorted(e.a for e in events), [i for i in range(6, head) if i not in (10, 20, 1025, 40)])
            v1 = Path(d) / 'v1.bin'                                 # a ring of an older build: 48-byte events, no commit word
            body = bytearray(8 * 48)
            for index in range(3):
                struct.pack_into('<QIIQQQQ', body, index * 48, 500 + index, ftring.MARK, 9, index, 0, 0, 0)
            v1.write_bytes(struct.pack('<8sQQQ32x', b'ANYPSFT1', 8, 1, 3) + bytes(body))
            header, events = ftring.read_ring(v1)
            self.assertEqual((header['version'], header['count'], header['missing']), (1, 3, 0))
            self.assertEqual([(e.ns, e.a) for e in events], [(500, 0), (501, 1), (502, 2)])

    def test_slot_claimed_while_the_reader_copies_it_is_rejected(self):
        # a live reader: a writer claims slot 5 (index 5 -> odd claim word of its next lap) between the field copy and the re-check
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'live.bin'
            ftring.write_ring(path, [ftring.Event(1000 + i, ftring.MARK, 3, i, 0, 0, 0) for i in range(10)], capacity=1024)

            def writer_claims(index):
                if index == 5:
                    with open(path, 'r+b') as f:
                        f.seek(64 + 5 * 64)
                        f.write(struct.pack('<Q', 2 * (5 + 1024) + 1))

            header, events = ftring.read_ring(path, _after_copy=writer_claims)
            self.assertEqual((header['missing'], sorted(e.a for e in events)), (1, [0, 1, 2, 3, 4, 6, 7, 8, 9]))

    def test_invalid_capacity_is_an_error(self):
        path, result = self.run_mode('wrap', 'bad.bin', ANYPS3_FRAMETIME_EVENTS='1000')
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid ANYPS3_FRAMETIME_EVENTS', result.stderr)

    def test_field_layout_spans_wrap_and_block(self):
        path, result = self.run_mode('fields', 'fields.bin', ANYPS3_FRAMETIME_PEEK_REG='26', ANYPS3_FRAMETIME_PEEK_PC='0x5c5730')
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        _, events = ftring.read_ring(path)
        by_kind = {}
        for e in events:
            by_kind.setdefault(e.kind, []).append(e)
        vb = by_kind[ftring.VBLANK][0]
        self.assertEqual((vb.a, vb.b, vb.c, vb.d), (11, 22, 33, 44))
        span = by_kind[ftring.FIFO_RUN][0]
        self.assertEqual((span.a, span.c, span.d), (55, 66, 7))     # a and c modified after construction
        self.assertGreaterEqual(span.b, 20_000_000)                 # slept 20 ms
        self.assertLess(span.b, 2_000_000_000)
        self.assertNotIn(ftring.SERVICE, by_kind)                   # below min_ns
        wrapped = by_kind[ftring.HOST_EXEC][0]
        self.assertEqual(wrapped.a, 77)
        self.assertGreaterEqual(wrapped.c, 30_000_000)              # queue wait: posted before the 30 ms sleep
        self.assertGreaterEqual(wrapped.b, 10_000_000)              # execution
        self.assertNotEqual(wrapped.tid, span.tid)                  # executed on another thread
        sleeps = [e for e in by_kind[ftring.BLOCK] if e.c >> 32 == 12 and e.a >> 32 == 0x5c5734]
        others = [e for e in by_kind[ftring.BLOCK] if e.c >> 32 == 7]
        unpeeked = [e for e in by_kind[ftring.BLOCK] if e.a >> 32 == 0x1111]
        self.assertEqual([e.d for e in unpeeked], [0])
        plus4 = [e for e in by_kind[ftring.BLOCK] if e.a >> 32 == 0x2222]
        self.assertEqual([(e.d >> 32, e.d & 0xffffffff) for e in plus4], [(0x1230, 0xCAFEF00D)])
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(len(others), 1)
        s = sleeps[0]
        self.assertEqual((s.a >> 32, s.a & 0xffffffff), (0x5c5734, 0x5c5730))
        self.assertEqual(s.b, 50)                                   # usleep argument
        self.assertEqual(s.c & 0xffffffff, 7)                       # guest thread id
        self.assertEqual((s.d >> 32, s.d & 0xffffffff), (0x1230, 0xCAFEF00D))   # peeked word behind r26
        self.assertEqual(others[0].b, 999)                          # object id for other wait kinds
        submit = by_kind[ftring.GPU_SUBMIT][0]
        self.assertEqual(ftring.unpack_text(submit.d), 'fifo_lab')  # first 8 characters
        # time order: the span is stamped with its START time, which precedes the vblank mark's end time
        self.assertLessEqual(vb.ns, span.ns)


if __name__ == '__main__':
    unittest.main()
