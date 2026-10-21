"""Reader for the FRAMETIME ring written by ANYPS3_FRAMETIME (anyps3/frametime.py documents the wire format)."""
import mmap
import struct
from collections import namedtuple

HEADER = struct.Struct('<8sQQQQ24x')   # magic, capacity, version, head, dropped (v1: 32 pad bytes after head), pad
EVENT = struct.Struct('<QIIQQQQ')       # ns, kind, tid, a, b, c, d: the 48-byte event of wire format 1
EVENT2 = struct.Struct('<QQIIQQQQ8x')   # seq, ns, kind, tid, a, b, c, d, pad: the 64-byte event of wire format 2
SEQ = struct.Struct('<Q')
assert HEADER.size == 64 and EVENT.size == 48 and EVENT2.size == 64

Event = namedtuple('Event', 'ns kind tid a b c d')

(POST_FIFO, FIFO_RUN, VBLANK, FLIP_REQ, FLIP_VSYNC, FLIP_ASYNC, GPU_SUBMIT, GPU_RETIRE, LABEL_PUB, HOST_REQ, HOST_EXEC,
 BLOCK, IDLE, SERVICE, FAULT, DISPLAY_REQ, DISPLAY_EXEC, BARRIER_REQ, SWITCH, EVENT_PUB, WORKER_WAIT, MARK, LEASE_WAIT) = range(1, 24)
NAMES = {POST_FIFO: 'PostFifo', FIFO_RUN: 'FifoRun', VBLANK: 'Vblank', FLIP_REQ: 'FlipReq', FLIP_VSYNC: 'FlipVsync',
         FLIP_ASYNC: 'FlipAsync', GPU_SUBMIT: 'GpuSubmit', GPU_RETIRE: 'GpuRetire', LABEL_PUB: 'LabelPub',
         HOST_REQ: 'HostReq', HOST_EXEC: 'HostExec', BLOCK: 'Block', IDLE: 'Idle', SERVICE: 'Service', FAULT: 'Fault',
         DISPLAY_REQ: 'DisplayReq', DISPLAY_EXEC: 'DisplayExec', BARRIER_REQ: 'BarrierReq', SWITCH: 'Switch',
         EVENT_PUB: 'EventPub', WORKER_WAIT: 'WorkerWait', MARK: 'Mark', LEASE_WAIT: 'LeaseWait'}


def unpack_text(value):
    """Packed 8-character ASCII (little endian) -> str."""
    return value.to_bytes(8, 'little').rstrip(b'\0').decode('ascii', 'replace')


def read_ring(path, seconds=None, skip_end_s=0.0, _after_copy=None):
    """Return (header dict, [Event]) ordered by time (spans by START time).

    Wire format 2: the newest min(head, capacity) reserved indices are the window; index j is returned only when its slot's
    commit word is exactly 2j+2 before and after the fields were copied (anyps3/frametime.py documents the protocol), so a
    half-written event (writer killed or still storing), an event of another lap (a delayed writer) and a dropped event are
    never returned (a slot a writer claims while it is copied is rejected by the second commit-word read; `_after_copy(index)` is a
    test hook run between the copy and that read); header['missing'] counts such window slots and header['dropped'] the writers' own drop counter.
    Wire format 1 (48-byte events, no commit word, older builds): slots with ns == 0 or kind == 0 are skipped, a torn slot can
    only be among the last few events. `seconds` keeps only the last N seconds before the newest event minus `skip_end_s`.
    """
    with open(path, 'rb') as f:
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, capacity, version, head, dropped = HEADER.unpack_from(data, 0)
        if magic != b'ANYPSFT1' or version not in (1, 2):
            raise ValueError('not a FRAMETIME ring')
        if version == 1:
            dropped = 0     # the v1 header has no drop counter (those bytes are padding)
        count = min(head, capacity)
        first = head - count
        events = []
        missing = 0
        mask = capacity - 1
        if version == 2:
            for index in range(first, head):
                offset = HEADER.size + (index & mask) * EVENT2.size
                seq, ns, kind, tid, a, b, c, d = EVENT2.unpack_from(data, offset)
                if _after_copy is not None:
                    _after_copy(index)
                if seq != 2 * index + 2 or SEQ.unpack_from(data, offset)[0] != seq:
                    missing += 1
                    continue
                events.append(Event(ns, kind, tid, a, b, c, d))
        else:
            for index in range(first, head):
                slot = index & mask
                ns, kind, tid, a, b, c, d = EVENT.unpack_from(data, HEADER.size + slot * EVENT.size)
                if ns == 0 or kind == 0:
                    continue
                events.append(Event(ns, kind, tid, a, b, c, d))
        data.close()
    events.sort(key=lambda e: e.ns)
    if seconds is not None and events:
        end = max(e.ns + (e.b if e.kind in SPAN_KINDS else 0) for e in events) - int(skip_end_s * 1e9)
        events = [e for e in events if end - int(seconds * 1e9) <= e.ns <= end]
    return dict(capacity=capacity, head=head, count=count, version=version, dropped=dropped, missing=missing), events


# Kinds emitted once at their end with ns = start and b = duration.
SPAN_KINDS = {FIFO_RUN, FLIP_VSYNC, HOST_REQ, HOST_EXEC, IDLE, SERVICE, FAULT, DISPLAY_REQ, DISPLAY_EXEC, BARRIER_REQ,
              WORKER_WAIT}


def write_ring(path, events, capacity=1 << 12, version=2):
    """Write a ring in the wire format (tests and synthetic inputs): every event committed, in index order."""
    with open(path, 'wb') as f:
        f.write(HEADER.pack(b'ANYPSFT1', capacity, version, len(events), 0))
        size = EVENT2.size if version == 2 else EVENT.size
        slots = bytearray(capacity * size)
        for index, e in enumerate(events):
            offset = (index & (capacity - 1)) * size
            if version == 2:
                EVENT2.pack_into(slots, offset, 2 * index + 2, e.ns, e.kind, e.tid, e.a, e.b, e.c, e.d)
            else:
                EVENT.pack_into(slots, offset, e.ns, e.kind, e.tid, e.a, e.b, e.c, e.d)
        f.write(slots)
