"""Raw PROF1 sample records including the wall-clock stamp slot (addresses[7]) that sample_report.read_samples drops.

Wall-mode samples (ANYPS3_SAMPLE_PROFILE_WALL, or ANYPS3_SAMPLE_PROFILE_STAMP=1) carry the CLOCK_MONOTONIC time of the sample in
nanoseconds in addresses[7] (depth is capped at 7 then), so records of different threads share one time axis even when timer
notifications were coalesced (overrun > 0). Wire layout: sample_report.HEADER / CHUNK / RECORD.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'local/ratchet/tools'))
import sample_report as sr


def read(path):
    """-> (header dict, [record dict]) from a thread-*.samples file; torn trailing chunks are ignored like sample_report does."""
    data = Path(path).read_bytes()
    magic, version, size, period_ns, tid, role, depth, pid, _ = sr.HEADER.unpack_from(data, 0)
    if magic != b'APS3SP01':
        raise ValueError(f'{path}: not a sample file')
    offset, records = sr.HEADER.size, []
    while offset + sr.CHUNK.size <= len(data):
        cmagic, length, count, *_ = sr.CHUNK.unpack_from(data, offset)
        end = offset + sr.CHUNK.size + length + 16
        if cmagic != b'APCHUNK1' or end > len(data):
            break
        payload = data[offset + sr.CHUNK.size:offset + sr.CHUNK.size + length]
        for sequence, overrun, steps, pc, guest_tid, n, flags, *a in sr.RECORD.iter_unpack(payload):
            records.append(dict(sequence=sequence, overrun=overrun, guest_steps=steps, pc=pc, guest_tid=guest_tid, depth=n,
                                flags=flags, rip=a[0], chain=a[1:n], stamp=a[7]))
        offset = end
    return dict(role=sr.ROLES[role], tid=tid, period_ns=period_ns), records
