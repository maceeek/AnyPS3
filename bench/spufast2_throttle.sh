#!/bin/bash
# Causality experiment (diagnostic only, no guest-visible change): during the level measurement window, pin one SPU lane
# thread (identified through the sampler file index) to one logical CPU shared with a CPU hog, which roughly halves its
# throughput and adds scheduling latency. Compares the main thread's per-frame wait/busy numbers against an unthrottled run.
# POLICY=idle additionally sets SCHED_IDLE on the lane (it then starves against the hog = near freeze).
# Usage (project root, own run name must not exist): spufast2_throttle.sh SRC NAME LANE_INDEX[,LANE_INDEX..] [CPU=17]
#   LANE_INDEX = sampler file index (0 = PPU, 1 = first SPU lane = 0x100, 2 = 0x200, 3 = 0x1000200, 4 = 0x2000200, 5 = 0x3000200, 6 = 0x4000200)
set -u
S=$1; N=$2; LANES=$3; CPU=${4:-17}
R=/home/martink/AnyPS3; L=$R/local/ratchet; D=$L/native-virtual-$N; SP=$L/sample-profile-$N
cd $R || exit 1
( while ! grep -aq 'level detected' $L/measure-$N.log 2>/dev/null; do sleep 2; done
  sleep 45
  P=""; for p in $(cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/anyps3-run-$N.scope/cgroup.procs 2>/dev/null); do
    [ "$(cat /proc/$p/comm 2>/dev/null)" = program ] && P=$p; done
  [ -n "$P" ] || { echo "$(date +%T) no pid" >> $L/throttle-$N.log; exit 1; }
  taskset -c $CPU sh -c 'while :; do :; done' & HOG=$!
  for idx in ${LANES//,/ }; do
    f=$(ls $SP/thread-*-$idx.samples 2>/dev/null | head -1); t=$(basename $f | sed 's/thread-\([0-9]*\)-.*/\1/')
    echo "$(date +%T) pin lane index $idx tid $t file $f to cpu $CPU (hog $HOG)" >> $L/throttle-$N.log
    taskset -pc $CPU $t >> $L/throttle-$N.log 2>&1
    [ "${POLICY:-}" = idle ] && chrt -i -p 0 $t >> $L/throttle-$N.log 2>&1
  done
  sleep 66
  for idx in ${LANES//,/ }; do f=$(ls $SP/thread-*-$idx.samples | head -1); t=$(basename $f | sed 's/thread-\([0-9]*\)-.*/\1/'); [ "${POLICY:-}" = idle ] && chrt -o -p 0 $t >> $L/throttle-$N.log 2>&1; taskset -pc 0-5,12-17 $t >> $L/throttle-$N.log 2>&1; done
  [ "$(cat /proc/$HOG/comm 2>/dev/null)" = sh ] && kill $HOG
  echo "$(date +%T) released" >> $L/throttle-$N.log ) &
bash $L/tools/measure_level.sh $S $N ANYPS3_SAMPLE_PROFILE=$SP ANYPS3_SAMPLE_PROFILE_US=2000 ANYPS3_SAMPLE_PROFILE_ROLES=ppu,spu > $L/measure-$N.log 2>&1
wait
