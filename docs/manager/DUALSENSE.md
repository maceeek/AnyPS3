# DualSense compatibility inspection — 2026-10-21

**Derived assessment:** a PS5 DualSense can supply the runtime's basic SDL2
GameController input on Linux when SDL recognizes the device and the user has
device access. This is a source inspection, not verified USB/Bluetooth operation
or original-game navigation. No controller, rumble, game or audio hardware test
was performed. Application source baseline: `73c93e3`; input semantics unchanged
by GUIAUDIO.

**Verified from upstream:** SDL added DualSense HIDAPI support in **2.0.14**
([release notes](https://raw.githubusercontent.com/libsdl-org/SDL/release-2.0.14/WhatsNew.txt),
General, first item). The manager controller screen requires **2.0.18+** because
it resolves [SDL_GameControllerHasRumble](https://wiki.libsdl.org/SDL2/SDL_GameControllerHasRumble),
introduced in that version. Its serial/sensor APIs are present in 2.0.14. This
change reports a missing required symbol as `incompatible-sdl`, with the minimum
version, before initializing any subsystem. Mock-library tests cover this error
and controller-only initialization; they do not load SDL.

**Verified from local code:** [native_pad.py](../../anyps3/native_pad.py),
`NativePad::load/poll/sample` (lines 27–79 at baseline), uses SDL2 GameController
enumeration and auto-assigns recognized devices to free ports. Manager
[pads.py](../../anyps3_manager/pads.py), `Pads.__init__/devices`, enables
`SDL_JOYSTICK_HIDAPI_PS5=1`, initializes only `SDL_INIT_GAMECONTROLLER` (`0x2000`)
and reads six axes and 21 buttons. Launch settings enable the same PS5 hint by
default and accept SDL mapping entries through `input.sdl_mappings`. Neither
controller path initializes SDL audio.

Combining `NativePad::sample` with the public SDL
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
| PS | GUIDE, button 5 | **Missing from runtime mapping**; visible in manager raw input |

Stick values map −32768…32767 to 0…255 (center 128); no application dead zone is
applied. Trigger pressure rounds the positive 0…32767 range to 0…255 and is sent
only when the guest requests pressure mode. Other button pressures are binary
0/255. `pad_service` advertises sticks/conformity, without pressure, motion or
actuator capabilities. Motion requests explicitly stop as unimplemented; guest
rumble returns unsupported. Touchpad, microphone, adaptive triggers and other
DualSense-specific controls have no guest mapping. These runtime gaps remain
separate work requiring native regression tests and a fresh game build/run.

The GUI [controller screen](../../anyps3_gui/app.py), `Controllers`, shows device
name/GUID/serial/battery/rumble capability and live raw button/axis arrays, including
PS as button index 5. Its rumble test is host feedback, not game-controlled
rumble. It explicitly marks custom port assignment, remapping, dead zones and
in-game rumble unavailable in the current runtime.

**Verified upstream; local permissions untested:** Linux HIDAPI's hidraw backend
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
