import os
import logging

logger = logging.getLogger(__name__)

SCALE = 16384


def _has_package():
    pkg = globals().get('__package__')
    return pkg is not None and pkg != ''


def _import_plugin_module(module_name):
    if _has_package():
        pkg = globals().get('__package__')
        try:
            return __import__(pkg + '.' + module_name, globals(), locals(), [module_name], 0)
        except (ImportError, SystemError):
            pass
    try:
        import local_import
        return local_import.import_sibling(module_name)
    except ImportError:
        return __import__(module_name)


def _load_vjoy_class():
    if os.name == 'posix':
        _mod = _import_plugin_module('vjoy_linux')
    else:
        _mod = _import_plugin_module('vjoy')
    return _mod.vJoy


vJoy = _load_vjoy_class()


def _vjoy_device_id():
    value = os.environ.get("ACGYM_VJOY_DEVICE")
    if value is not None:
        return int(value)
    return 1


class Controls(object):
    def __init__(self):
        self.onButtons = 0
        self.vj = vJoy(reference=_vjoy_device_id())
        # Only the collector process enables the feeder. The in-game plugin must
        # never AcquireVJD or write axes — AC reads vJoy; the collector feeds it.
        self._feeder_enabled = False

        self.steer = 1.0
        self.acc = 0.0
        self.brake = 0.0
        self.enable_gear_shift = 0.
        self.shift_up = 0.
        self.shift_down = 0.

        self.steer_cmd = 0.0
        self.pedal_cmd = 0.0
        self.brake_cmd = 0.0

        self.ct_12_stop = False

    def enable_feeder(self):
        self._feeder_enabled = True

    def feeder_enabled(self):
        return bool(self._feeder_enabled)

    def close(self):
        self.vj.close()

    def _ensure_vjoy_acquired(self):
        if getattr(self.vj, "acquired", False):
            return True
        return bool(self.vj.open())

    def trigger_emergency_stop(self):
        self.ct_12_stop = True
        self.steer = 1.0
        self.acc = 0.0
        self.brake = 0.5
        logger.info("CT12 triggered")
        self.update()

    def set_controls(self, steer, acc, brake, enable_gear_shift=False, shift_up=False, shift_down=False):
        self.steer_cmd = steer
        self.pedal_cmd = acc
        self.brake_cmd = brake

        if not self.ct_12_stop:
            self.steer = self.steer_cmd + 1
            if self.steer < 0:
                self.steer = 0
            elif self.steer > 2:
                self.steer = 2

            self.acc = (self.pedal_cmd + 1) / 2
            if self.acc < 0:
                self.acc = 0
            elif self.acc > 1:
                self.acc = 1

            self.brake = (self.brake_cmd + 1) / 2
            if self.brake < 0:
                self.brake = 0
            elif self.brake > 1:
                self.brake = 1

            if enable_gear_shift:
                if shift_up:
                    self.onButtons = 0x00000001
                elif shift_down:
                    self.onButtons = 0x00000002
                else:
                    self.onButtons = 0
            else:
                self.onButtons = 0
        self.update()

    def update(self):
        if not self._feeder_enabled:
            return
        self._ensure_vjoy_acquired()
        self.setJoy(self.steer, self.acc, self.brake, self.onButtons, SCALE)

    def setJoy(self, valueX, valueY, valueZ, onButtons, scale):
        xPos = int(valueX * scale)
        yPos = int(valueY * 2 * scale)
        zPos = int(valueZ * 2 * scale)
        joystickPosition = self.vj.generateJoystickPosition(
            wAxisX=xPos, wAxisY=yPos, wAxisZ=zPos, lButtons=onButtons)
        self.vj.update(joystickPosition)
