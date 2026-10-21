# DualSense, SIXAXIS and rumble through SDL2

PADMOTION, 2026-10-21. Status: **verified by native fake-SDL and virtual-controller
regressions**, **derived from the references below**, **unverified on a physical
controller or in the original game**. This task builds no game and measures no FPS.

The runtime loads SDL2 dynamically. Basic DualSense input and motion require
SDL >= 2.0.14. Rumble capability detection requires SDL >= 2.0.18 and a controller
for which `SDL_GameControllerHasRumble` returns true. Missing optional symbols
leave basic input usable. The manager enables `SDL_JOYSTICK_HIDAPI_PS5=1` and
background controller events. HIDAPI may need read/write access to the relevant
`/dev/hidraw*` device through the distribution's controller udev rules. No udev
rules or host permissions were changed by this task.

## Guest behavior

- `cellPadGetInfo2`: standard conformity + sticks (`0x09`); sensor capability
  (`0x04`) only with both accelerometer and gyro and all sensor entry points;
  actuator (`0x10`) only with confirmed rumble support and rumble allowed.
- `cellPadSetActDirect`: small motor is binary (any nonzero byte), mapped to SDL
  high frequency `65535`; large motor `0..255` maps to SDL low frequency
  `value * 257`. The native worker renews a 1000 ms effect every 250 ms even
  without further guest pad calls. These durations are host policy, not PS3
  constants. Zero/zero stops immediately. Disconnect and `cellPadEnd` request a
  zero effect before closing the controller. End joins the worker; Init can
  subsequently start another worker. A failed active-motor stop makes End return
  `CELL_PAD_ERROR_FATAL` (`0x80121101`) after cleanup. A renewal failure is an
  explicit runtime STOP on the next pad poll.
- Existing actuator validation remains: uninitialized `0x80121104`, invalid
  port/null/reserved bytes `0x80121102`, absent device `0x80121107`, unsupported
  actuator or rejected SDL rumble `0x80121108`. Reserved-byte validation retains
  the signed SDK comparison above `0x1fffff`.
- `cellPadSetPortSetting` stores PRESS (`2`) and SENSOR (`4`) independently.
  As in the reference implementation, it accepts SENSOR_ON on a sensorless or
  disconnected port; ports 7..126 remain a successful no-op, 127 is invalid.
  On capable hardware it enables/disables both SDL sensors and rolls back the
  first change if the second fails. SDL enable failure returns unsupported;
  rollback failure stops explicitly. Stored settings are applied on reconnect.
- **Derived runtime error policy:** GetData with SENSOR_ON but no complete sensor
  backend returns `CELL_PAD_ERROR_UNSUPPORTED_GAMEPAD` (`0x80121108`), leaving
  the output untouched. A supported sensor read failure or nonfinite measurement
  returns `CELL_PAD_ERROR_DATA_READ_FAILED` (`0x80121106`). These choices honor
  the accepted setting without fabricating neutral motion; they are not claimed
  as measurements of sensorless PS3 hardware behavior.
- GetData sensor reports have length 24 and header `0x7c`; X/Y/Z/G are unsigned
  big-endian 16-bit words 20..23 with 10-bit values. Pressure words 8..19 are zero
  unless PRESS_ON. Without SENSOR_ON the previous 8/20-word reports, headers,
  44-byte output span and change detection remain intact. Unchanged sensor
  reports repeat after approximately 10 ms, following the reference behavior.
- SDL GUIDE (button 5) maps to the existing PS bit `0x0100` in DIGITAL1, word 2.
  No extra button word or substitute system menu is introduced. Whether the
  desktop/controller driver delivers GUIDE and whether the game uses it remain
  unverified.

## Motion scales and provenance

**Derived conversion:** `X/Y/Z = truncate(clamp(512 - a / 9.80665 * 113, 0, 1023))`.
SDL accelerometer values are in m/s² including gravity: +X points right, +Y up,
+Z toward the player. Each axis is inverted for the PS3 representation.
A controller resting with SDL Y = +1 g therefore yields PS3 Y = **399**;
negative 1 g yields **625**. Zero acceleration yields **512**.

`G = truncate(clamp(512 + gyro_y * 180/pi * 123/90, 0, 1023))`.
SDL gyro values are rad/s. Only Y yaw contributes; positive yaw stays positive.
+90°/s yields **635**, -90°/s **389**. All outputs saturate to **0..1023**.
The scale is a conversion to the DS3 interface, not an integration into angles.

Reference root on this PC: `/home/martink/AnyPS3/local/reference/`.
The Cell manuals present there concern CPU architecture and do not document this
controller ABI; no PDF table or firmware-specific sensor calibration was assumed.

| Versioned reference | Evidence |
| --- | --- |
| RPCS3 `ffba2bf09ef3980638f9327b25240d81414034ab`, `rpcs3/Emu/Io/pad_types.h:98-114,136-140,274-277,362` | setting flags, PS bit, capabilities, sensor word offsets, 113 counts/g |
| Same revision, `rpcs3/Emu/Cell/Modules/cellPad.h:7-18,28-32` | error codes and report lengths 8/20/24 |
| Same revision, `rpcs3/Emu/Cell/Modules/cellPad.cpp:539-594,867-908,1036-1066` | sensor cadence/layout/header, actuator validation/order, settings accepted without capability check |
| Same revision, `rpcs3/Input/sdl_pad_handler.cpp:657-688` | SDL orientation, inverted accelerometer axes, gyro Y, 512 center, 123/90 degrees/s |
| Same revision, `rpcs3/Emu/Io/PadHandler.cpp:174-177` | clamp then truncate to unsigned integer |
| Same revision, `rpcs3/Input/ds4_pad_handler.cpp:890-906`, `rpcs3/Input/dualsense_pad_handler.cpp:615-632` | independent handler cross-check of DS3 scales and yaw selection |
| PSL1GHT `f649a08fd536a9e27c08c7db2d93a2d7ee4c3bbe`, `ppu/include/io/pad.h:163-170,205-217,336-339` | 10-bit range, small/large actuator byte semantics, sensor offsets |
| Installed SDL2 headers `2.32.74`, `SDL_sensor.h:71-109,139`, `SDL_gamecontroller.h:759-766` | SI units, right/up/toward-player axes, gravity constant, GUIDE enum value |

SDL API contracts: [Rumble](https://wiki.libsdl.org/SDL2/SDL_GameControllerRumble),
[HasRumble](https://wiki.libsdl.org/SDL2/SDL_GameControllerHasRumble),
[GetSensorData](https://wiki.libsdl.org/SDL2/SDL_GameControllerGetSensorData).
**Refuted probe:** [SDL release-2.0.14, SDL_joystick.c:807-814](https://github.com/libsdl-org/SDL/blob/release-2.0.14/src/joystick/SDL_joystick.c#L807)
returns success for unchanged zero amplitudes without querying the driver.
Consequently neither runtime nor diagnostics use `Rumble(0,0)` as capability
proof. Older SDL remains usable with actuator support unadvertised.

## Manager and validation

`input.rumble` defaults to `true`; set it globally or per title in Settings, or
with `settings set input.rumble false`. It exports `ANYPS3_PAD_RUMBLE=0/1` only
when the selected build contains that setting. With it disabled, GetInfo2 omits
actuator support and guest actuator requests return unsupported. The explicit
manager Rumble test is a separate diagnostic. The controller screen shows both
Rumble and Motion (accelerometer + gyro) capabilities. Existing compiled game
artifacts do not gain runtime functionality until their owner rebuilds them.

Reproducible scoped commands, logs, test-source hashes and mutation results are
in [evidence/padmotion/README.md](evidence/padmotion/README.md). Tests use an
isolated fake SDL shared library or SDL virtual devices, never a physical rumble
probe. All processes force dummy/null audio; the Qt checks run offscreen.
Physical USB/Bluetooth behavior, calibration quality, actual game motion/rumble
usage, and menu navigation with a physical gamepad remain unverified.
