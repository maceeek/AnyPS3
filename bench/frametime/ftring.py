"""Reader for the FRAMETIME ring written by ANYPS3_FRAMETIME (anyps3/frametime.py documents the wire format)."""
import mmap
import struct
from collections import namedtuple

HEADER = struct.Struct('<8sQQQ32x')   # magic, capacity, version, head, pad
EVENT = struct.Struct('<QIIQQQQ')       # ns, kind, tid, a, b, c, d
assert HEADER.size == 64 and EVENT.size == 48

Event = namedtuple('Event', 'ns kind tid a b c d')

(POST_FIFO, FIFO_RUN, VBLANK, FLIP_REQ, FLIP_VSYNC, FLIP_ASYNC, GPU_SUBMIT, GPU_RETIRE, LABEL_PUB, HOST_REQ, HOST_EXEC,
 BLOCK, IDLE, SERVICE, FAULT, DISPLAY_REQ, DISPLAY_EXEC, BARRIER_REQ, SWITCH, EVENT_PUB, WORKER_WAIT, MARK) = range(1, 23)
NAMES = {POST_FIFO: 'PostFifo', FIFO_RUN: 'FifoRun', VBLANK: 'Vblank', FLIP_REQ: 'FlipReq', FLIP_VSYNC: 'FlipVsync',
         FLIP_ASYNC: 'FlipAsync', GPU_SUBMIT: 'GpuSubmit', GPU_RETIRE: 'GpuRetire', LABEL_PUB: 'LabelPub',
         HOST_REQ: 'HostReq', HOST_EXEC: 'HostExec', BLOCK: 'Block', IDLE: 'Idle', SERVICE: 'Service', FAULT: 'Fault',
         DISPLAY_REQ: 'DisplayReq', DISPLAY_EXEC: 'DisplayExec', BARRIER_REQ: 'BarrierReq', SWITCH: 'Switch',
         EVENT_PUB: 'EventPub', WORKER_WAIT: 'WorkerWait', MARK: 'Mark'}


def unpack_text(value):
    """Packed 8-character ASCII (little endian) -> str."""
    return value.to_bytes(8, 'little').rstrip(b'\0').decode('ascii', 'replace')


def read_ring(path, seconds=None, skip_end_s=0.0):
    """Return (header dict, [Event]) ordered by time (spans by START time).

    The newest min(head, capacity) events are valid. `seconds` keeps only the last N seconds before the newest event
    minus `skip_end_s`. A torn slot (SIGKILL in the middle of a store) can only be among the last few events.
    """
    with open(path, 'rb') as f:
        data = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        magic, capacity, version, head = HEADER.unpack_from(data, 0)
        if magic != b'ANYPSFT1' or version != 1:
            raise ValueError('not a FRAMETIME ring')
        count = min(head, capacity)
        first = head - count
        events = []
        for index in range(first, head):
            slot = index & (capacity - 1)
            ns, kind, tid, a, b, c, d = EVENT.unpack_from(data, HEADER.size + slot * EVENT.size)
            if ns == 0 or kind == 0:
                continue
            events.append(Event(ns, kind, tid, a, b, c, d))
        data.close()
    events.sort(key=lambda e: e.ns)
    if seconds is not None and events:
        end = max(e.ns + (e.b if e.kind in SPAN_KINDS else 0) for e in events) - int(skip_end_s * 1e9)
        events = [e for e in events if end - int(seconds * 1e9) <= e.ns <= end]
    return dict(capacity=capacity, head=head, count=count), events


# Kinds emitted once at their end with ns = start and b = duration.
SPAN_KINDS = {FIFO_RUN, FLIP_VSYNC, HOST_REQ, HOST_EXEC, IDLE, SERVICE, FAULT, DISPLAY_REQ, DISPLAY_EXEC, BARRIER_REQ,
              WORKER_WAIT}


def write_ring(path, events, capacity=1 << 12):
    """Write a ring in the wire format (tests and synthetic inputs)."""
    with open(path, 'wb') as f:
        f.write(HEADER.pack(b'ANYPSFT1', capacity, 1, len(events)))
        slots = bytearray(capacity * EVENT.size)
        for index, e in enumerate(events):
            EVENT.pack_into(slots, (index & (capacity - 1)) * EVENT.size, e.ns, e.kind, e.tid, e.a, e.b, e.c, e.d)
        f.write(slots)
