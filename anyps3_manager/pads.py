"""Lazy SDL2 controller diagnostics; the audio subsystem is never initialised."""

import ctypes as C
import os
import time

from .storage import ManagerError


class Guid(C.Structure):
    _fields_ = [("data", C.c_uint8*16)]


class Pads:
    def __init__(self):
        os.environ["SDL_AUDIODRIVER"] = "dummy"
        os.environ["SDL_JOYSTICK_HIDAPI_PS5"] = "1"
        os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
        try:
            self.sdl = C.CDLL("libSDL2-2.0.so.0")
        except OSError as exc:
            raise ManagerError("missing-sdl", str(exc), 6) from exc
        signatures = {
            "SDL_InitSubSystem": (C.c_int, [C.c_uint32]),
            "SDL_QuitSubSystem": (None, [C.c_uint32]),
            "SDL_GetError": (C.c_char_p, []),
            "SDL_NumJoysticks": (C.c_int, []),
            "SDL_IsGameController": (C.c_int, [C.c_int]),
            "SDL_GameControllerOpen": (C.c_void_p, [C.c_int]),
            "SDL_GameControllerClose": (None, [C.c_void_p]),
            "SDL_GameControllerUpdate": (None, []),
            "SDL_GameControllerName": (C.c_char_p, [C.c_void_p]),
            "SDL_GameControllerGetJoystick": (C.c_void_p, [C.c_void_p]),
            "SDL_GameControllerGetType": (C.c_int, [C.c_void_p]),
            "SDL_GameControllerGetSerial": (C.c_char_p, [C.c_void_p]),
            "SDL_GameControllerHasRumble": (C.c_int, [C.c_void_p]),
            "SDL_GameControllerHasSensor": (C.c_int, [C.c_void_p, C.c_int]),
            "SDL_GameControllerRumble": (C.c_int, [C.c_void_p, C.c_uint16, C.c_uint16, C.c_uint32]),
            "SDL_GameControllerGetAxis": (C.c_int16, [C.c_void_p, C.c_int]),
            "SDL_GameControllerGetButton": (C.c_uint8, [C.c_void_p, C.c_int]),
            "SDL_JoystickGetGUID": (Guid, [C.c_void_p]),
            "SDL_JoystickInstanceID": (C.c_int32, [C.c_void_p]),
            "SDL_JoystickCurrentPowerLevel": (C.c_int, [C.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self.sdl, name, None)
            if function is None and name in ("SDL_GameControllerHasRumble", "SDL_GameControllerHasSensor", "SDL_GameControllerRumble"):
                continue
            if function is None:
                raise ManagerError("missing-sdl-symbol", f"SDL2 is missing {name}; use SDL >= 2.0.14.", 6)
            function.restype, function.argtypes = result, args
        # SDL_INIT_GAMECONTROLLER includes joystick support, not audio.
        if self.sdl.SDL_InitSubSystem(0x2000):
            raise ManagerError("sdl-error", self.error())

    def has_rumble(self, controller):
        rumble = getattr(self.sdl, "SDL_GameControllerRumble", None)
        has = getattr(self.sdl, "SDL_GameControllerHasRumble", None)
        # SDL 2.0.14 caches a zero amplitude and may return success without
        # consulting the driver. Only the actual support query is evidence.
        return bool(rumble and has and has(controller))

    def has_sensor(self, controller, kind):
        has = getattr(self.sdl, "SDL_GameControllerHasSensor", None)
        return bool(has and has(controller, kind))

    def error(self):
        return (self.sdl.SDL_GetError() or b"SDL error").decode(errors="replace")

    def close(self):
        self.sdl.SDL_QuitSubSystem(0x2000)

    def devices(self, rumble=None):
        self.sdl.SDL_GameControllerUpdate()
        records = []
        found = False
        for index in range(self.sdl.SDL_NumJoysticks()):
            if not self.sdl.SDL_IsGameController(index):
                continue
            controller = self.sdl.SDL_GameControllerOpen(index)
            if not controller:
                continue
            try:
                joystick = self.sdl.SDL_GameControllerGetJoystick(controller)
                guid = bytes(self.sdl.SDL_JoystickGetGUID(joystick).data).hex()
                serial = (self.sdl.SDL_GameControllerGetSerial(controller) or b"").decode(errors="replace")
                instance = self.sdl.SDL_JoystickInstanceID(joystick)
                record = dict(id=str(instance), guid=guid, serial=serial,
                    name=(self.sdl.SDL_GameControllerName(controller) or b"Controller").decode(errors="replace"),
                    type=self.sdl.SDL_GameControllerGetType(controller),
                    rumble=self.has_rumble(controller),
                    sensors=dict(accelerometer=self.has_sensor(controller, 1),
                                 gyro=self.has_sensor(controller, 2)),
                    battery=self.sdl.SDL_JoystickCurrentPowerLevel(joystick),
                    axes=[self.sdl.SDL_GameControllerGetAxis(controller, n) for n in range(6)],
                    buttons=[self.sdl.SDL_GameControllerGetButton(controller, n) for n in range(21)])
                record["motion"] = all(record["sensors"].values())
                records.append(record)
                if rumble == str(instance):
                    found = True
                    if not record["rumble"] or self.sdl.SDL_GameControllerRumble(controller, 16384, 16384, 250):
                        raise ManagerError("rumble-unavailable", self.error())
                    time.sleep(.3)
            finally:
                self.sdl.SDL_GameControllerClose(controller)
        if rumble is not None and not found:
            raise ManagerError("missing-pad", "Controller is no longer connected.", 6)
        return records
