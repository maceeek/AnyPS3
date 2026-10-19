"""Actual owner/lane launch paths with the separately delivered worker hooks.

Build 374 integrated worker-hooks.patch into par_worker.py, rsx_worker.py and
rsx_game_worker.py; the generated code must contain each hook exactly once.
This is a native synthetic fixture, not a game build or evidence of game
performance.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from anyps3.elf import ElfImage
from anyps3.program import compile_program
from tests.test_anyps3 import BLR, program
from tests.test_sample_profile import HARNESS, reporter


@unittest.skipUnless(shutil.which('g++'), 'requires native toolchain')
class SampleProfileIntegrationTests(unittest.TestCase):
    def test_real_spu_lane_and_rsx_queue_launch_hooks(self):
        code = compile_program(ElfImage.parse(program(BLR)), process_entry=True).cpp()
        # Header emitted by program.py contains the fragment and the integrated worker hooks.
        hooks = [
            '    std::function<std::shared_ptr<void>()> thread_scope;\n    std::function<void(unsigned)> run_fifo;',
            '            try{const auto scope=thread_scope?thread_scope():nullptr;for(;;){',
            '            try{\n                anyps3_sample::Thread sample_profile(anyps3_sample::Role::Spu,context?&context->pc:nullptr,context?&context->id:nullptr,context?&context->steps:nullptr);\n                while(!stop){',
        ]
        for hook in hooks:
            self.assertEqual(code.count(hook), 1)
        self.assertIn('q.launch();', code)
        self.assertIn('anyps3_sample::Thread sample_profile(anyps3_sample::Role::Ppu,&state.pc,&state.thread_id,&state.steps)', code)
        code = code.replace('int main(int argc, char** argv)', 'int original_main(int argc, char** argv)', 1)
        code += HARNESS[:HARNESS.index('int main(int argc,char** argv)')]
        code += r'''
#include <cassert>
int main(){
    std::uint32_t pc=0x30000,tid=1;std::uint64_t steps=11;
    anyps3_sample::Thread coordinator(anyps3_sample::Role::Ppu,&pc,&tid,&steps);
    NativeSpu::Thread guest;guest.pc=0x100;guest.id=0x40;guest.steps=2;
    NativeSpuWorker lane;lane.context=&guest;lane.event_driven=true;
    lane.step=[](unsigned){assert(sample_caller_known(600000000)==179999999700000001ULL);return false;};
    RsxOwnerQueue queue;
    queue.thread_scope=[]()->std::shared_ptr<void>{
        if(!anyps3_sample::config().selected(anyps3_sample::Role::Rsx))return {};
        return std::make_shared<anyps3_sample::Thread>(anyps3_sample::Role::Rsx);
    };
    queue.run_fifo=[](unsigned){assert(sample_caller_known(600000000)==179999999700000001ULL);};
    lane.launch();queue.launch();queue.await(queue.post(RsxOwnerQueue::Kind::RunFifo));
    assert(sample_caller_known(600000000)==179999999700000001ULL);
    lane.shutdown();lane.check();queue.shutdown();queue.check();
}
'''
        with tempfile.TemporaryDirectory(prefix='anyps3-prof1-hooks-') as d:
            root = Path(d); source = root / 'fixture.cpp'; exe = root / 'program'
            source.write_text(code)
            build = subprocess.run(['g++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                                    '-Wno-unused-function', '-pthread', str(source), '-o', str(exe)],
                                   capture_output=True, text=True, timeout=180)
            self.assertEqual(build.returncode, 0, build.stderr[-10000:])
            env = {k: v for k, v in os.environ.items() if not k.startswith('ANYPS3_SAMPLE_PROFILE')}
            env.update(SDL_AUDIODRIVER='dummy', ANYPS3_SAMPLE_PROFILE=str(root / 'capture'))
            env.pop('ANYPS3_AUDIO_AUDIBLE', None)
            run = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
            result = reporter.report(root / 'capture', exe)
            self.assertEqual(set(result['roles']), {'ppu', 'rsx', 'spu'})
            for role in result['roles'].values():
                self.assertGreater(role['samples'], 20)
                self.assertTrue(any('sample_hot_known' in row['name'] for row in role['flat']))


if __name__ == '__main__':
    unittest.main()
