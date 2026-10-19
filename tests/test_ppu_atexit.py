"""sysPrxForUser sys_ppu_thread_exit runs registered atexit OPDs as guest calls."""
import shutil
import unittest

from anyps3.elf import ElfImage
from anyps3.program import ATEXIT_RETURN, compile_program
from tests import test_runtime_objects
from tests.test_anyps3 import BLR, program


@unittest.skipUnless(shutil.which('c++'), 'Requires C++')
class PpuAtexitTests(unittest.TestCase):
    def test_trampoline_page_is_emitted(self):
        source = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        self.assertIn(f'case 0x{ATEXIT_RETURN:x}: anyps3_op_{ATEXIT_RETURN:x}(state);', source)
        self.assertIn('ppu_atexit_continue(state);', source)

    def test_callbacks_run_in_slot_order_then_thread_exits_with_saved_value(self):
        test_runtime_objects.RuntimeObjectsTests().run_native(r'''
 (void)call;
 auto thread=[&](unsigned id){s.thread_id=id;mem.map(0x30000,0x1000,6,nullptr,0);mem.map(0x32000,0x1000,6,nullptr,0);
   s.stack_base=0x30000;s.tls_base=0x32000;s.returned=false;};
 // OPDs at 0x10100 {code 0x10400, toc 0x11111}, 0x10108 {code 0x10500, toc 0x22222}.
 mem.store(0x10100,4,0x10400);mem.store(0x10104,4,0x11111);mem.store(0x10108,4,0x10500);mem.store(0x1010c,4,0x22222);
 (*s.thread_atexit)={0,0x10100,0,0x10108,0,0,0,0};
 thread(2);s.ppu_threads.push_back(static_cast<PpuContext&>(s));
 r[3]=0x55;bool blocked=false;try{ppu_thread_exit_wrapper(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.pc==0x10400 && r[2]==0x11111 && s.lr==0xfffe0010 && s.atexit_running && !s.returned);
 r[3]=0x99;anyps3_op_fffe0010(s);                      // first callback returned (r3 clobbered)
 assert(s.pc==0x10500 && r[2]==0x22222 && s.lr==0xfffe0010 && !s.returned);
 blocked=false;try{anyps3_op_fffe0010(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.returned && s.exit_status==0x55 && !s.atexit_running);
 fault([&]{mem.load(0x30000,4);});fault([&]{mem.load(0x32000,4);});
 fault([&]{anyps3_op_fffe0010(s);});                   // stray return without a pending exit
 // Raw syscall 41 does not run the wrapper's callbacks.
 thread(3);r[3]=7;blocked=false;try{ppu_thread_exit(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.returned && s.exit_status==7);
 // Another thread owns the process atexit mutex: block before consuming any callbacks.
 thread(4);PpuContext other;other.thread_id=5;other.atexit_running=true;s.ppu_threads.push_back(other);
 r[3]=0x66;
 const auto before_pc=s.pc;const auto before_lr=s.lr,before_toc=r[2],before_stack=s.stack_base,before_tls=s.tls_base;
 const auto before_status=s.exit_status,before_value=s.atexit_value;const auto before_next=s.atexit_next;
 const auto callbacks=*s.thread_atexit;
 blocked=false;try{ppu_thread_exit_wrapper(s);}catch(const PpuBlocked&){blocked=true;}
 assert(blocked && s.wait_kind==13 && s.wait_id==5 && !s.returned && !s.atexit_running);
 assert(s.pc==before_pc && s.lr==before_lr && r[2]==before_toc && r[3]==0x66);
 assert(s.stack_base==before_stack && s.tls_base==before_tls && s.exit_status==before_status);
 assert(s.atexit_value==before_value && s.atexit_next==before_next && *s.thread_atexit==callbacks);
 assert(s.ppu_threads.back().thread_id==5 && s.ppu_threads.back().atexit_running);
 mem.access(before_stack,4,false);mem.access(before_tls,4,false);
''')


if __name__ == '__main__':
    unittest.main()
