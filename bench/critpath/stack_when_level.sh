#!/bin/bash
# Usage: stack_when_level.sh NAME DELAY SAMPLES INTERVAL : after "level detected" in measure-NAME.log wait DELAY s, then take SAMPLES eu-stack
# snapshots (all threads, host stacks) INTERVAL s apart into work/critpath/stack-NAME.jsonl. Diagnostic: each sample stops the game briefly.
N=$1; DELAY=${2:-45}; S=${3:-120}; I=${4:-0.25}
R=/home/martink/AnyPS3; L=$R/local/ratchet
until grep -q "level detected" $L/measure-$N.log 2>/dev/null; do sleep 1; done
sleep $DELAY
P=""; for p in $(cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/anyps3-run-$N.scope/cgroup.procs 2>/dev/null); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = program ] && P=$p; done
[ -n "$P" ] || { echo "no program pid"; exit 1; }
cd $R && python3 $L/tools/stack_sample.py sample $P $S $I $L/work/critpath/stack-$N.jsonl --log $L/native-virtual-$N/stderr.log
echo "$(date +%T) stack samples done"
