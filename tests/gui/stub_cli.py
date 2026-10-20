import json
import sys
import time

args = sys.argv[1:]

def emit(value):
    print(json.dumps({"schema": "anyps3.manager/1", "future_field": "ignored",
                      "uint64": 18446744073709551615, **value}), flush=True)

if args[0] == "jobs" and args[1] == "follow":
    for value in (dict(event="stage", stage="compile", index=2, count=3),
                  dict(event="progress", stage="compile", done=1, total=2, unit="objects"),
                  dict(event="result", build="fixture")):
        emit(value)
        time.sleep(.03)
elif args[0] == "error":
    emit(dict(event="error", code="guest-stop", message="STOP pc=0x10: fixture", detail={"run": "test"}))
    sys.exit(3)
elif args[0] == "malformed":
    print("not JSON")
elif args[0] == "titles":
    emit({"titles": []})
elif args[0] == "jobs":
    emit({"jobs": []})
elif args[0] == "doctor":
    emit({"checks": {"ram_bytes": 64*1024**3}})
elif args[0] == "settings":
    emit({"settings": {}, "overrides": {}})
else:
    emit({"ok": True})
