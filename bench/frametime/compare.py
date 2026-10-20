#!/usr/bin/env python3
"""One summary row per FRAMETIME ring: frame-period distribution, work span, pacing-label latency, lane host-request stalls.

usage: compare.py RING [RING ...] [--seconds 55] [--skip-end 3] [--label-address 0x60300410]
"""
import argparse
import bisect
import collections
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as an  # noqa: E402
import ftring as ft  # noqa: E402


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p * len(values)))] if values else float('nan')


def summarize(path, seconds, skip_end, label_address):
    _, events = ft.read_ring(path, seconds, skip_end)
    k = an.by_kind(events)
    frames = an.annotate(an.build_frames(k), k, None)
    rows = [f for f in frames[1:] if f.get('periods') is not None]
    periods = collections.Counter(min(f['periods'], 3) for f in rows)
    n = len(rows)
    span = sorted(f['work_span'] / 1e6 for f in rows)
    vs = [f['vs_end'] for f in frames]
    labels = [(e.ns - vs[i]) / 1e6 for e in k[ft.LABEL_PUB] if e.a == label_address
              for i in [bisect.bisect_right(vs, e.ns) - 1] if i >= 0]
    lanes = sum(e.b for e in k[ft.HOST_REQ]) / 1e6 / max(n, 1)
    fps = n / ((rows[-1]['vs_end'] - rows[0]['lo']) / 1e9) if n else 0
    return dict(name=Path(path).stem, frames=n, fps=fps, one=100 * periods[1] / max(n, 1), two=100 * periods[2] / max(n, 1),
                three=100 * periods[3] / max(n, 1), span50=pct(span, .5), span90=pct(span, .9), span99=pct(span, .99),
                label50=pct(labels, .5), label90=pct(labels, .9), lanes=lanes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('rings', nargs='+')
    ap.add_argument('--seconds', type=float, default=55.0)
    ap.add_argument('--skip-end', type=float, default=3.0)
    ap.add_argument('--label-address', type=lambda x: int(x, 0), default=0x60300410)
    args = ap.parse_args()
    print(f'{"ring":14s} {"frames":>6s} {"FPS":>6s} {"1vbl%":>6s} {"2vbl%":>6s} {"3+%":>5s} {"span50":>7s} {"span90":>7s} {"span99":>7s} '
          f'{"lbl50":>6s} {"lbl90":>6s} {"laneHostReq":>11s}')
    for path in args.rings:
        r = summarize(path, args.seconds, args.skip_end, args.label_address)
        print(f'{r["name"]:14s} {r["frames"]:6d} {r["fps"]:6.2f} {r["one"]:6.1f} {r["two"]:6.1f} {r["three"]:5.1f} {r["span50"]:7.2f} '
              f'{r["span90"]:7.2f} {r["span99"]:7.2f} {r["label50"]:6.2f} {r["label90"]:6.2f} {r["lanes"]:11.2f}')


if __name__ == '__main__':
    main()
