#!/usr/bin/env python3
"""Per-frame timeline analysis of an ANYPS3_FRAMETIME ring (see anyps3/frametime.py for the event kinds).

usage: analyze.py RING [--seconds 60] [--skip-end 0] [--worst 12] [--json OUT] [--top-lr 8]

Frames are the guest flips executed by the RSX worker: FlipReq (worker starts the flip), FlipVsync (worker waits for the emulated
vblank `V.c`), FlipAsync (present batch queued), LabelPub kind 1/2 (the guest-visible flip completion). A frame "takes k vblanks"
when the vblank number of its flip minus the previous flip's vblank number is k. The target of the task is >= 95% with k == 1.
"""
import argparse
import collections
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ftring as ft  # noqa: E402

MS = 1e6


def by_kind(events):
    result = collections.defaultdict(list)
    for e in events:
        result[e.kind].append(e)
    return result


def overlap(lo, hi, start, dur):
    """Length of [start, start+dur] inside [lo, hi]."""
    return max(0, min(hi, start + dur) - max(lo, start))


def first_after(events, ns, lo=0):
    """Index of the first event with e.ns >= ns (events sorted by ns), bisect by hand to avoid copying."""
    a, b = lo, len(events)
    while a < b:
        m = (a + b) // 2
        if events[m].ns < ns:
            a = m + 1
        else:
            b = m
    return a


def build_frames(k):
    flips = k[ft.FLIP_REQ]
    vsyncs = k[ft.FLIP_VSYNC]
    asyncs = k[ft.FLIP_ASYNC]
    pubs = [e for e in k[ft.LABEL_PUB] if e.c == 1]
    frames = []
    vi = ai = pi = 0
    for index, req in enumerate(flips):
        nxt = flips[index + 1].ns if index + 1 < len(flips) else None
        vi = first_after(vsyncs, req.ns, vi)
        vs = vsyncs[vi] if vi < len(vsyncs) and (nxt is None or vsyncs[vi].ns < nxt) else None
        ai = first_after(asyncs, req.ns, ai)
        asy = asyncs[ai] if ai < len(asyncs) and (nxt is None or asyncs[ai].ns < nxt + 5_000_000) else None
        end_vsync = vs.ns + vs.b if vs else req.ns
        pi = first_after(pubs, (asy.ns if asy else req.ns), pi)
        pub = pubs[pi] if pi < len(pubs) else None
        frames.append(dict(req=req.ns, id=req.a, vs_start=vs.ns if vs else None, vs_end=end_vsync, deadline=vs.a if vs else None,
                           tick=vs.c if vs else None, vs_wait=vs.b if vs else 0, asy=asy.ns if asy else None,
                           done=pub.ns if pub and pub.ns >= (asy.ns if asy else req.ns) else None))
    return frames


def annotate(frames, k, coordinator_tid):
    runs = k[ft.FIFO_RUN]
    submits = k[ft.GPU_SUBMIT]
    retires = k[ft.GPU_RETIRE]
    waits = [e for e in k[ft.WORKER_WAIT] if e.c == 1]
    flushes = [e for e in k[ft.WORKER_WAIT] if e.c in (2, 3)]
    idles = [e for e in k[ft.IDLE] if e.tid == coordinator_tid]
    hosts = k[ft.HOST_REQ]
    hexec = k[ft.HOST_EXEC] + k[ft.FAULT]
    ri = 0
    for i, f in enumerate(frames):
        if i == 0:
            continue
        lo = frames[i - 1]['vs_end']
        hi = f['req']
        f['lo'] = lo
        f['work_span'] = hi - lo
        ri = first_after(runs, lo - 50_000_000, ri)
        busy = 0
        n = 0
        draws = 0
        first = None
        j = ri
        while j < len(runs) and runs[j].ns < hi:
            r = runs[j]
            o = overlap(lo, hi, r.ns, r.b)
            if o:
                busy += o
                n += 1
                draws += r.a
                if first is None:
                    first = max(lo, r.ns)
            j += 1
        f['worker_busy'] = busy
        f['worker_runs'] = n
        f['worker_draws'] = draws
        f['worker_start_lag'] = (first - lo) if first is not None else None
        f['worker_gap'] = f['work_span'] - busy
        f['gpu_blocking'] = sum(overlap(lo, hi, e.ns, e.b) for e in waits)
        f['flush_wait'] = sum(overlap(lo, hi, e.ns, e.b) for e in flushes)
        f['host_wait'] = sum(overlap(lo, hi, e.ns, e.b) for e in hosts)
        f['host_exec'] = sum(overlap(lo, hi, e.ns, e.b) for e in hexec)
        f['main_idle'] = sum(overlap(lo, hi, e.ns, e.b) for e in idles)
        f['gpu_submits'] = sum(1 for e in submits if lo <= e.ns < hi)
        f['vblank_late_ms'] = None
        f['post_vsync_ms'] = ((f['done'] - f['vs_end']) / MS) if f['done'] else None
        f['periods'] = (f['tick'] - frames[i - 1]['tick']) if f['tick'] is not None and frames[i - 1]['tick'] is not None else None
    return frames


def wait_episodes(k, guest_thread=1):
    """Contiguous runs of Block events of one guest thread: (start, end, lr, count). End = the next Switch to that thread."""
    blocks = [e for e in k[ft.BLOCK] if (e.c & 0xffffffff) == guest_thread]
    switches = [e for e in k[ft.SWITCH] if e.b == guest_thread]
    episodes = []
    si = 0
    for e in blocks:
        si = first_after(switches, e.ns, si)
        wake = switches[si].ns if si < len(switches) else e.ns
        episodes.append((e.ns, wake, e.a >> 32, e.a & 0xffffffff, e.b, e.c >> 32, e.d))
    return episodes


def summarize(values):
    if not values:
        return 'n/a'
    values = sorted(values)
    q = lambda p: values[min(len(values) - 1, int(p * len(values)))]
    return f'mean {statistics.fmean(values):7.3f}  p50 {q(.5):7.3f}  p90 {q(.9):7.3f}  p99 {q(.99):7.3f}  max {values[-1]:7.3f}'



def trace_frame(events, k, frames, index, margin_ms=2.0):
    """Time-ordered compressed trace around frame `index` (previous vsync end - margin .. flip request + vsync + margin)."""
    f = frames[index]
    lo = frames[index - 1]['vs_end'] - int(margin_ms * MS)
    hi = f['vs_end'] + int(margin_ms * MS)
    lines = []
    ep = None
    for e in events:
        if e.ns < lo or e.ns > hi:
            continue
        t = (e.ns - frames[index - 1]['vs_end']) / MS
        name = ft.NAMES[e.kind]
        if e.kind == ft.BLOCK:
            key = (e.a >> 32, e.a & 0xffffffff, e.c & 0xffffffff, e.c >> 32)
            if ep and ep['key'] == key and e.ns - ep['last'] < 400_000:
                ep['last'] = e.ns
                ep['n'] += 1
                ep['peek'] = e.d
                continue
            if ep:
                lines.append((ep['first'], f"{(ep['first'] - frames[index - 1]['vs_end']) / MS:9.3f}  Block   guest{ep['key'][2]} kind{ep['key'][3]} lr={ep['key'][0]:08x} pc={ep['key'][1]:08x} x{ep['n']} for {(ep['last'] - ep['first']) / 1e3:.0f} us peek={ep['peek'] >> 32:08x}:{ep['peek'] & 0xffffffff:08x}"))
            ep = dict(key=key, first=e.ns, last=e.ns, n=1, peek=e.d)
            continue
        if e.kind == ft.SWITCH:
            continue
        if e.kind in ft.SPAN_KINDS:
            if e.b < 100_000 and e.kind in (ft.IDLE, ft.HOST_EXEC):
                continue
            lines.append((e.ns, f"{t:9.3f}  {name:8s} dur {e.b / MS:7.3f} ms a={e.a:x} c={e.c:x} d={e.d:x}"))
        elif e.kind == ft.LABEL_PUB and e.c != 1:
            continue
        else:
            extra = ''
            if e.kind == ft.GPU_SUBMIT:
                extra = f' reason={ft.unpack_text(e.d)}'
            lines.append((e.ns, f"{t:9.3f}  {name:8s} a={e.a:x} b={e.b:x} c={e.c:x} d={e.d:x}{extra}"))
    if ep:
        lines.append((ep['first'], f"{(ep['first'] - frames[index - 1]['vs_end']) / MS:9.3f}  Block   guest{ep['key'][2]} kind{ep['key'][3]} lr={ep['key'][0]:08x} pc={ep['key'][1]:08x} x{ep['n']} for {(ep['last'] - ep['first']) / 1e3:.0f} us peek={ep['peek'] >> 32:08x}:{ep['peek'] & 0xffffffff:08x}"))
    lines.sort(key=lambda x: x[0])
    return [l for _, l in lines]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('ring')
    ap.add_argument('--seconds', type=float, default=60.0)
    ap.add_argument('--skip-end', type=float, default=0.0)
    ap.add_argument('--worst', type=int, default=12)
    ap.add_argument('--top-lr', type=int, default=8)
    ap.add_argument('--json')
    ap.add_argument('--trace-frame', type=int, action='append', default=[])
    args = ap.parse_args()
    header, events = ft.read_ring(args.ring, args.seconds, args.skip_end)
    k = by_kind(events)
    out = []
    p = out.append
    span_s = (events[-1].ns - events[0].ns) / 1e9 if events else 0
    p(f'ring capacity {header["capacity"]} head {header["head"]}; window {span_s:.1f} s, {len(events)} events')
    p('kinds: ' + ', '.join(f'{ft.NAMES[kind]}={len(v)}' for kind, v in sorted(k.items())))
    coordinator_tid = collections.Counter(e.tid for e in k[ft.POST_FIFO]).most_common(1)
    coordinator_tid = coordinator_tid[0][0] if coordinator_tid else None
    frames = build_frames(k)
    frames = annotate(frames, k, coordinator_tid)
    rows = [f for f in frames[1:] if f.get('periods') is not None]
    if not rows:
        p('no complete frames')
        print('\n'.join(out))
        return 1
    periods = collections.Counter(min(f['periods'], 5) for f in rows)
    total = len(rows)
    p(f'\nFRAMES {total}: ' + ', '.join(f'{n} vblank{"s" if n > 1 else ""}{"+" if n == 5 else ""}: {periods[n]} ({100 * periods[n] / total:.1f}%)'
                                      for n in sorted(periods)))
    fps = total / ((rows[-1]['vs_end'] - rows[0]['lo']) / 1e9)
    p(f'mean frame rate over the window {fps:.2f} FPS')
    fast = [f for f in rows if f['periods'] == 1]
    slow = [f for f in rows if f['periods'] >= 2]
    comps = [('work_span (prev vsync end -> flip req) ms', lambda f: f['work_span'] / MS),
             ('worker busy in FifoRun ms', lambda f: f['worker_busy'] / MS),
             ('worker gap (not in FifoRun) ms', lambda f: f['worker_gap'] / MS),
             ('worker restart lag after vsync ms', lambda f: (f['worker_start_lag'] or 0) / MS),
             ('draws processed', lambda f: f['worker_draws']),
             ('gpu blocking waits ms', lambda f: f['gpu_blocking'] / MS),
             ('flush waits ms', lambda f: f['flush_wait'] / MS),
             ('host-access requester waits ms', lambda f: f['host_wait'] / MS),
             ('host-access/fault exec ms', lambda f: f['host_exec'] / MS),
             ('main coordinator idle ms', lambda f: f['main_idle'] / MS),
             ('gpu submits', lambda f: f['gpu_submits']),
             ('vsync wait (slack before the deadline) ms', lambda f: f['vs_wait'] / MS),
             ('flip completion after vsync end ms', lambda f: f['post_vsync_ms'] if f['post_vsync_ms'] is not None else 0)]
    for label, rows_ in (('1-vblank frames', fast), ('>=2-vblank frames', slow)):
        p(f'\n{label}: {len(rows_)}')
        for name, fn in comps:
            p(f'  {name:46s} {summarize([fn(f) for f in rows_])}')
    p(f'\nWORST {args.worst} frames (by work_span): start-of-work is the previous vsync end')
    p('  idx   period work_span busy gap lag draws gpuW flushW hostW idle  vs_wait post_vs')
    worst = sorted(rows, key=lambda f: -f['work_span'])[:args.worst]
    for f in worst:
        p(f'  {frames.index(f):5d} {f["periods"]:5d} {f["work_span"] / MS:9.2f} {f["worker_busy"] / MS:5.2f} {f["worker_gap"] / MS:4.2f} '
          f'{(f["worker_start_lag"] or 0) / MS:4.2f} {f["worker_draws"]:5d} {f["gpu_blocking"] / MS:4.2f} {f["flush_wait"] / MS:5.2f} '
          f'{f["host_wait"] / MS:5.2f} {f["main_idle"] / MS:5.2f} {f["vs_wait"] / MS:7.2f} {(f["post_vsync_ms"] or 0):6.2f}')
    # Vblank publication lateness (ms): the guest sees a vblank this much after its ideal time.
    late = [e.b / MS for e in k[ft.VBLANK]]
    p(f'\nVBLANK publication lateness ms ({len(late)} ticks): {summarize(late)}')
    # Guest waits of the main thread, by call site.
    episodes = wait_episodes(k)
    by_lr = collections.defaultdict(list)
    for start, wake, lr, pc, arg, wkind, peek in episodes:
        by_lr[(lr, pc, wkind)].append((wake - start) / 1e3)
    p(f'\nMAIN guest thread blocks (wake = next scheduler switch to the thread), {len(episodes)} blocks; top call sites by total time')
    p('  lr        pc        kind  count    total_ms  mean_us  p99_us')
    ranked = sorted(by_lr.items(), key=lambda kv: -sum(kv[1]))[:args.top_lr]
    for (lr, pc, wkind), v in ranked:
        v.sort()
        p(f'  {lr:08x}  {pc:08x}  {wkind:4d} {len(v):6d} {sum(v) / 1e3:10.1f} {statistics.fmean(v):8.1f} {v[int(.99 * (len(v) - 1))]:8.1f}')
    for index in args.trace_frame:
        p(f'\nTRACE frame {index} (t=0 at the previous flip vsync end, period {frames[index].get("periods")})')
        out.extend(trace_frame(events, k, frames, index))
    print('\n'.join(out))
    if args.json:
        Path(args.json).write_text(json.dumps(dict(periods=dict(periods), frames=rows), indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
