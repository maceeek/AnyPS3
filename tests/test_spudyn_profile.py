import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


def module():
    path = Path(__file__).resolve().parents[1]/'local/ratchet/tools/spu_dynamic_profile.py'
    spec = importlib.util.spec_from_file_location('spudyn_profile_test', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class DynamicProfileTests(unittest.TestCase):
    def test_exact_file_offsets_heap_ea_and_unknown_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            payload=bytes(range(64))
            row=dict(schema_version=1, image_index=0, spu_id=0x3000200, pc=0x5600,
                     word=0x00010203, ls_base=0x5600, size=64,
                     sha256=hashlib.sha256(payload).hexdigest(), data_hex=payload.hex(),
                     last_dma=[dict(ls_base=0x5500,size=0x200,ea=0x47193400)])
            log=root/'log';log.write_text(json.dumps(row)+'\n'+json.dumps(row)+'\n')
            game=root/'level';game.write_bytes(b'x'*19+payload+b'y'*7+payload)
            heap=root/'heap';heap.write_bytes(b'z'*0x100+payload)
            tool=module()
            result=tool.proposal(log,[game],[('0x47193400',heap)])
            self.assertEqual(len(result['overlays']),1)
            sources=result['overlays'][0]['source_candidates']
            self.assertEqual([s['file_offset'] for s in sources],[19,90,0x100])
            self.assertEqual(sources[-1]['ea'],0x47193500)
            self.assertEqual(sources[0]['file_sha256'],hashlib.sha256(game.read_bytes()).hexdigest())
            self.assertFalse(result['unresolved'])
            self.assertEqual(len(tool.proposal(log)['unresolved']),1)
            limited=tool.proposal(log,[game],max_matches=1)['overlays'][0]
            self.assertTrue(limited['matches_truncated'])
            self.assertEqual(len(limited['source_candidates']),1)
            row['sha256']='0'*64;log.write_text(json.dumps(row)+'\n')
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                tool.proposal(log,[game])


if __name__ == '__main__':
    unittest.main()
