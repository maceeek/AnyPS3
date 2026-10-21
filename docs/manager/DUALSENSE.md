# DualSense, SIXAXIS and rumble through SDL2

PADMOTION, 2026-10-21. Status: **verified by native fake-SDL and virtual-controller
regressions**, **derived from the references below**, **unverified on a physical
controller or in the original game**. This task builds no game and measures no FPS.

## SDL requirements and input path

The runtime loads SDL2 dynamically. Basic DualSense input and motion require
SDL >= 2.0.14; SDL introduced DualSense HIDAPI support in that version
([release notes](https://raw.githubusercontent.com/libsdl-org/SDL/release-2.0.14/WhatsNew.txt)).
Confirmed rumble capability requires SDL >= 2.0.18 and a controller for which
`SDL_GameControllerHasRumble` returns true. Missing optional runtime symbols leave
basic input usable, with unsupported capabilities unadvertised.

The manager controller screen requires **SDL >= 2.0.18** because it resolves
`SDL_GameControllerHasRumble`. It reports a missing required symbol as
`incompatible-sdl`, naming the minimum version, before initializing any subsystem.
Mock-library tests cover this error and controller-only initialization without
loading SDL. The runtime's optional-symbol policy is separate from this manager
requirement.

[NativePad::load/poll/sample](../../anyps3/native_pad.py) enumerates recognized SDL
GameControllers and automatically assigns them to free ports. Manager
[Pads.__init__/devices](../../anyps3_manager/pads.py) enables
`SDL_JOYSTICK_HIDAPI_PS5=1` and background controller events, initializes only
`SDL_INIT_GAMECONTROLLER` (`0x2000`) and reads six axes and 21 buttons. Launch
settings enable the PS5 hint by default and accept SDL mapping entries through
`input.sdl_mappings`. Neither controller path initializes SDL audio.

## Basic input mapping

Combining the runtime mapping with SDL's public
[button enum](https://wiki.libsdl.org/SDL2/SDL_GameControllerButton) and
[axis enum/ranges](https://wiki.libsdl.org/SDL2/SDL_GameControllerAxis) gives this
**derived mapping** for a standard SDL DualSense mapping:

| DualSense input | SDL input | PS3 output |
|---|---|---|
| Cross / Circle / Square / Triangle | A/B/X/Y, buttons 0/1/2/3 | Cross / Circle / Square / Triangle |
| L1 / R1 | LEFTSHOULDER / RIGHTSHOULDER, 9/10 | L1 / R1 |
| L2 / R2 | TRIGGERLEFT / TRIGGERRIGHT, axes 4/5 | Digital L2/R2 when positive; analog pressure 0–255 |
| L3 / R3 | LEFTSTICK / RIGHTSTICK, 7/8 | L3 / R3 |
| Options / Create | START / BACK, 6/4 | Start / Select |
| D-pad up/right/down/left | Buttons 11/14/12/13 | Corresponding PS3 D-pad bits |
| Left stick X/Y | Axes 0/1 | PS3 left X/Y (words 6/7) |
| Right stick X/Y | Axes 2/3 | PS3 right X/Y (words 4/5) |
| PS | GUIDE, button 5 | PS bit `0x0100` in DIGITAL1, word 2 |

Stick values map −32768…32767 to 0…255 (center 128); no application dead zone is
applied. Trigger pressure rounds the positive 0…32767 range to 0…255 and is sent
only when the guest requests pressure mode. Other button pressures are binary
0/255. Touchpad, microphone, adaptive triggers and other DualSense-specific
controls have no guest mapping.

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
proof. Older SDL remains usable by the runtime with actuator support unadvertised;
manager diagnostics require SDL >= 2.0.18.

## Linux udev and hidraw access

**Verified in the source inspection; local permissions untested:** Linux
HIDAPI's hidraw backend
opens the device with `O_RDWR | O_CLOEXEC`
([SDL 2.32.10 vendored HIDAPI](https://github.com/libsdl-org/SDL/blob/release-2.32.10/src/hidapi/linux/hid.c#L687-L703)).
The logged-in user therefore needs read/write access to the corresponding
`/dev/hidraw*`, normally supplied by the distribution's controller udev rules and
active-session ACLs. The PS5 hint does not grant access. An evdev fallback also
needs access to `/dev/input/event*` and joystick classification; see
[SDL 2.32.10 Linux troubleshooting](https://github.com/libsdl-org/SDL/blob/release-2.32.10/docs/README-linux.md#joystick-does-not-work).
USB/Bluetooth connection, installed SDL driver configuration, udev rules and
device ACLs must be checked with the actual controller before claiming hardware
support. No host permissions or system rules were changed for this inspection.

## Manager and validation

`input.rumble` defaults to `true`; set it globally or per title in Settings, or
with `settings set input.rumble false`. It exports `ANYPS3_PAD_RUMBLE=0/1` only
when the selected build contains that setting. With it disabled, GetInfo2 omits
actuator support and guest actuator requests return unsupported. The explicit
manager Rumble test is a separate diagnostic. The
[controller screen](../../anyps3_gui/app.py) shows name/GUID/serial/battery,
Rumble and Motion (accelerometer + gyro) capabilities, and live raw button/axis
arrays (including PS at button index 5). It marks custom port assignment,
remapping and dead zones unavailable; default SDL mapping remains active.
Existing compiled game artifacts do not gain runtime functionality until their owner rebuilds them.

Reproducible scoped commands, logs, test-source hashes and mutation results are
in [evidence/padmotion/README.md](evidence/padmotion/README.md). Tests use an
isolated fake SDL shared library or SDL virtual devices, never a physical rumble
probe. All processes force dummy/null audio; the Qt checks run offscreen.
Physical USB/Bluetooth behavior, calibration quality, actual game motion/rumble
usage, and menu navigation with a physical gamepad remain unverified.
