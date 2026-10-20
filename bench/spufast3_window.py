#!/usr/bin/env python3
"""Print 'LO HI' = ppu_steps of PERF interval lines N-29 and N of a run (the 60 s measurement window of measure_level.sh).
Usage: spufast3_window.py RUN_NAME_OR_DIR"""
import re, sys, os
r = sys.argv[1]
d = r if os.path.isdir(r) else f'/home/martink/AnyPS3/local/ratchet/native-virtual-{r}'
v = [int(m.group(1)) for l in open(d + '/stderr.log', errors='replace') for m in [re.search(r'PERF interval_s=.* ppu_steps=(\d+)', l)] if m]
print(v[-30], v[-1])
