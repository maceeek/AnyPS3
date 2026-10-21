"""Run with python -m tests.padmotion_mutations under the memory-capped scope."""
from anyps3.native_pad import TYPES, CPP
from tests.test_native_pad_motion import PadMotionTests, source

# Each mutation must compile, then be rejected by an observable ABI assertion.
# Production files are never modified.
MUTATIONS = [
    ("legacy-support-query", "types", "has_rumble && has_rumble(p.device)", "(!has_rumble || has_rumble(p.device))", "legacy"),
    ("rumble-setting", "types", "rumble_allowed && rumble", "rumble", "muted"),
    ("end-error", "cpp", "p.shutdown()?0:0x80121101", "(p.shutdown(),0)", "shutdown_failure"),
    ("actuator-capability", "cpp", "(port.actuator?16:0)", "(port.actuator?0:0)", "capabilities"),
    ("sensor-capability", "cpp", "(port.sensors?4:0)", "(port.sensors?0:0)", "capabilities"),
    ("both-sensors-required", "types", "has_sensor(p.device,1) && has_sensor(p.device,2)", "has_sensor(p.device,1)", "capabilities"),
    ("small-motor-binary", "cpp", "m.load(r[4],1)?65535:0", "m.load(r[4],1)*257", "rumble"),
    ("large-motor-scale", "cpp", "m.load(r[4]+1,1)*257", "m.load(r[4]+1,1)*256", "rumble"),
    ("motor-order", "cpp", "p.rumble(port.device,low,high,", "p.rumble(port.device,high,low,", "rumble"),
    ("persistent-effect", "types", "rumble_refresh_ms=250", "rumble_refresh_ms=5000", "rumble"),
    ("zero-stop", "cpp", "(low || high)?NativePad::rumble_duration_ms:0", "NativePad::rumble_duration_ms", "rumble"),
    ("disconnect-stop", "types", "stop_effect(p);close(p.device);", "close(p.device);", "rumble"),
    ("end-stop", "types", "stopped=stop_effect(p) && stopped;", "stopped=true;", "rumble"),
    ("sensor-enable", "types", "const int on=enabled?1:0", "const int on=0", "motion"),
    ("sensor-read-error", "types", "return 0x80121106;", "return 0;", "sensor_errors"),
    ("accel-sign", "types", "512.0f-(accel[i]", "512.0f+(accel[i]", "motion"),
    ("accel-scale", "types", "*113.0f", "*112.0f", "motion"),
    ("gravity-units", "types", "accel[i]/9.80665f", "accel[i]/1.0f", "motion"),
    ("sensor-centre", "types", "512.0f-(accel[i]", "511.0f-(accel[i]", "motion"),
    ("sensor-offset", "types", "data[20+i]=ten_bit", "data[19+i]=ten_bit", "motion"),
    ("gyro-axis", "types", "gyro[1]*rad_to_degree", "gyro[0]*rad_to_degree", "motion"),
    ("gyro-sign", "types", "512.0f+gyro[1]", "512.0f-gyro[1]", "motion"),
    ("gyro-scale", "types", "123.0f/90.0f", "90.0f/90.0f", "motion"),
    ("ten-bit-range", "types", "std::clamp(v,0.0f,1023.0f)", "std::clamp(v,0.0f,1022.0f)", "motion"),
    ("sensor-length", "cpp", "(port.setting&4)?24:", "(port.setting&4)?20:", "motion"),
    ("sensor-off-layout", "cpp", "const unsigned words=(port.setting&4)?24:20", "const unsigned words=24", "motion"),
    ("ps-bit", "types", "data[2]|=0x100", "data[2]|=0x200", "motion"),
]


def main():
    PadMotionTests.setUpClass()
    try:
        for name, section, old, new, case in MUTATIONS:
            types, cpp = TYPES, CPP
            original = types if section == "types" else cpp
            if old not in original:
                raise AssertionError(f"Mutation target missing: {name}")
            changed = original.replace(old, new)
            if section == "types":
                types = changed
            else:
                cpp = changed
            binary = PadMotionTests.build_source(source(types, cpp), "mutant-" + name)
            result = PadMotionTests.run_case(case, binary=binary, library="legacy" if case == "legacy" else "full", muted=case == "muted")
            if result.returncode == 0:
                raise AssertionError(f"SURVIVED: {name}")
            if "Assertion" not in result.stderr and "assertion" not in result.stderr:
                raise AssertionError(f"No assertion evidence for {name}: {result.stderr}")
            print(f"KILLED {name}: {case}", flush=True)
        print(f"All {len(MUTATIONS)} compiled mutations rejected.", flush=True)
    finally:
        PadMotionTests.doClassCleanups()


if __name__ == "__main__":
    main()
