#!/usr/bin/env python3
"""SPUPUT: where do guest accesses (SPU lanes, PPU) wait for the RSX owner? Reads an ANYPS3_FRAMETIME ring of a build with the
LeaseWait spans (kind 23) and the HostExec class bits (anyps3/frametime.py documents both).

usage: spuput_hostwait.py RING [--seconds 30] [--json OUT]

Per thread (main = PostFifo writer, rsx = FifoRun writer, others = SPU lanes / helper threads): LeaseWait count and ms/frame by
reason (1 owner RPC after a page-broker refusal, 2 owner RPC for a synchronously watched span, 3 broker wait_change), guest EA
classes (top byte), and the owner side: HostExec count, queue wait and execution time by class bit mask.
"""
import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'frametime'))
import ftring as ft  # noqa: E402

REASONS = {1: 'rpc_broker_refused', 2: 'rpc_watched', 3: 'broker_wait'}
CLASS_BITS = [(1, 'interested'), (2, 'raw_dirty'), (4, 'busy'), (8, 'label'), (16, 'protected'), (32, 'tex_inval'),
              (64, 'surface_flush'), (128, 'surface_dirty'), (256, 'guard_release')]


def class_name(mask):
    names = [n for bit, n in CLASS_BITS if mask & bit]
    return '+'.join(names) if names else 'none'


def analyze(events):
    kinds = collections.defaultdict(list)
    for e in events:
        kinds[e.kind].append(e)
    frames = max(1, len(kinds[ft.FLIP_REQ]))
    main = collections.Counter(e.tid for e in kinds[ft.POST_FIFO]).most_common(1)
    rsx = collections.Counter(e.tid for e in kinds[ft.FIFO_RUN]).most_common(1)
    roles = {}
    if main:
        roles[main[0][0]] = 'main'
    if rsx:
        roles[rsx[0][0]] = 'rsx'
    per = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
    ea = collections.defaultdict(lambda: [0, 0])
    sizes = collections.defaultdict(lambda: [0, 0])
    for e in kinds[ft.LEASE_WAIT]:
        reason = e.c & 0xff
        write = (e.c >> 8) & 1
        key = f'{REASONS.get(reason, reason)}{"/w" if write else "/r"}'
        per[e.tid][key][0] += 1
        per[e.tid][key][1] += e.b
        top = f'0x{(e.a >> 24) & 0xff:02x}xxxxxx {REASONS.get(reason, reason)}{"/w" if write else "/r"}'
        ea[top][0] += 1
        ea[top][1] += e.b
        bucket = 1 << max(0, (e.d - 1).bit_length()) if e.d else 0
        sizes[bucket][0] += 1
        sizes[bucket][1] += e.b
    execs = collections.defaultdict(lambda: [0, 0, 0])
    for e in kinds[ft.HOST_EXEC]:
        row = execs[class_name(e.d)]
        row[0] += 1
        row[1] += e.b
        row[2] += e.c
    reqs = collections.defaultdict(lambda: [0, 0])
    for e in kinds[ft.HOST_REQ]:
        reqs[e.tid][0] += 1
        reqs[e.tid][1] += e.b
    out = {'frames': frames, 'threads': {}, 'ea': {}, 'sizes': {}, 'host_exec': {}, 'host_req_ge20us': {}}
    for tid, rows in sorted(per.items(), key=lambda kv: -sum(v[1] for v in kv[1].values())):
        out['threads'][f'{tid} {roles.get(tid, "lane?")}'] = {k: {'count_per_frame': v[0] / frames, 'ms_per_frame': v[1] / 1e6 / frames,
                                                                'mean_us': v[1] / 1e3 / max(1, v[0])} for k, v in sorted(rows.items())}
    for k, v in sorted(ea.items(), key=lambda kv: -kv[1][1]):
        out['ea'][k] = {'count_per_frame': v[0] / frames, 'ms_per_frame': v[1] / 1e6 / frames}
    for k, v in sorted(sizes.items()):
        out['sizes'][str(k)] = {'count_per_frame': v[0] / frames, 'ms_per_frame': v[1] / 1e6 / frames}
    for k, v in sorted(execs.items(), key=lambda kv: -kv[1][1]):
        out['host_exec'][k] = {'count_per_frame': v[0] / frames, 'exec_ms_per_frame': v[1] / 1e6 / frames,
                               'mean_exec_us': v[1] / 1e3 / max(1, v[0]), 'mean_queue_us': v[2] / 1e3 / max(1, v[0])}
    for tid, v in sorted(reqs.items(), key=lambda kv: -kv[1][1]):
        out['host_req_ge20us'][f'{tid} {roles.get(tid, "lane?")}'] = {'count_per_frame': v[0] / frames, 'ms_per_frame': v[1] / 1e6 / frames}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ring')
    ap.add_argument('--seconds', type=float, default=30)
    ap.add_argument('--json')
    args = ap.parse_args()
    header, events = ft.read_ring(args.ring, seconds=args.seconds)
    out = analyze(events)
    out['dropped'] = header.get('dropped')
    out['missing'] = header.get('missing')
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))
    print(f"frames={out['frames']} dropped={out['dropped']} missing={out['missing']}")
    for section in ('threads', 'ea', 'sizes', 'host_exec', 'host_req_ge20us'):
        print(f'== {section}')
        for k, v in out[section].items():
            if isinstance(v, dict) and all(isinstance(x, dict) for x in v.values()):
                print(f'  {k}')
                for kk, vv in v.items():
                    print('    ' + kk + ' ' + ' '.join(f'{a}={b:.3f}' for a, b in vv.items()))
            else:
                print(f'  {k} ' + ' '.join(f'{a}={b:.3f}' for a, b in v.items()))


if __name__ == '__main__':
    main()
