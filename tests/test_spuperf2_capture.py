"""Capture/replay transport is verified with fixed GETL and PUTL bytes."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tests import test_spu_block

ROOT = Path(__file__).resolve().parents[1]
REPLAY_PATH = ROOT / 'local/ratchet/codex/spuperf2/replay.py'


def replay_module():
    spec = importlib.util.spec_from_file_location('spuperf2_replay_test', REPLAY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(shutil.which('c++'), 'Requires native C++ compiler')
class SpuPerf2CaptureTests(unittest.TestCase):
    def test_getl_putl_capture_replay_and_corrupt_output_detection(self):
        words = {**{0x100+i*4: 0x1c004183 for i in range(4)},
                 0x110: 0x21a00a85, 0x114: 0x21a00a86, 0x118: 0x01a00e87, 0x11c: 1}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            capture_path = root / 'capture.json'
            body = r'''
 const unsigned list=0x400;
 const std::uint8_t descriptor[]={0,0,0,16,0,1,1,0,0,0,0,16,0,1,1,16};
 std::copy_n(descriptor,16,ls+list);t.dma={0x1000,0,list,16,7};
 spu_word(t.gpr[5],0,0x44);spu_word(t.gpr[6],0,0x24);
 for(unsigned i=0;i<32;++i)mem.store(0x10100+i,1,0xa0+i);
 spu_reset_decrementer(t,timebase_ticks());t.decrementer=0x7ffffff;
 CAPTURE_ENV
 assert(spu_run(s,100) && t.steps==6 && t.pc==0x118 && spu_word(t.gpr[3],0)==4);
'''.replace('CAPTURE_ENV', f'setenv("ANYPS3_SPU_CAPTURE",{json.dumps(str(capture_path))},1);')
            test_spu_block.SpuBlockTests().native(words, body)
            capture = json.loads(capture_path.read_text())
            dma = [e for e in capture['events'] if e['kind'] == 'dma']
            self.assertEqual([(e['ea'], e['put'], e['data']) for e in dma],
                             [(0x10100, False, bytes(range(0xa0, 0xb0)).hex()),
                              (0x10110, False, bytes(range(0xb0, 0xc0)).hex()),
                              (0x10100, True, bytes(range(0xa0, 0xb0)).hex()),
                              (0x10110, True, bytes(range(0xb0, 0xc0)).hex())])
            self.assertEqual(len([e for e in capture['events'] if e['kind'] == 'fetch']), 7)
            self.assertEqual(capture['final']['scalar'][3], 6)  # RDCH has not retired.
            replay = replay_module()
            self.assertTrue(replay.replay(capture_path, root / 'after')['matched'])
            # Preserve transport requests but corrupt an expected PUT byte.
            dma[2]['data'] = 'ff' + dma[2]['data'][2:]
            bad_path = root / 'corrupt.json'
            bad_path.write_text(json.dumps(capture))
            with self.assertRaisesRegex(RuntimeError, 'DMA|differs'):
                replay.replay(bad_path, root / 'corrupt-output')

    def test_external_environment_is_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, 'External environment'):
            replay_module().validate_capture(dict(schema=1, budget=16,
                events=[dict(kind='unsupported', reason='reservation DMA')]))


if __name__ == '__main__':
    unittest.main()
