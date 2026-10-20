#!/bin/bash
# Usage: usr1_when_level.sh NAME DELAY [COUNT] : after "level detected" appears in measure-NAME.log send SIGUSR1 (guest thread dump)
# to the verified game PID of run NAME, DELAY seconds later, COUNT times 3 s apart (diagnostic only).
N=$1; DELAY=${2:-60}; COUNT=${3:-1}
L=/home/martink/AnyPS3/local/ratchet
until grep -q "level detected" $L/measure-$N.log 2>/dev/null; do sleep 1; done
sleep $DELAY
P=""; for p in $(cat /sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/app.slice/anyps3-run-$N.scope/cgroup.procs 2>/dev/null); do
  [ "$(cat /proc/$p/comm 2>/dev/null)" = program ] && P=$p; done
[ -n "$P" ] || { echo "no program pid"; exit 1; }
for i in $(seq 1 $COUNT); do kill -USR1 $P; echo "$(date +%T) USR1 -> $P"; sleep 3; done
