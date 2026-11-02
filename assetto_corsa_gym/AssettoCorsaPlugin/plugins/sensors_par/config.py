
import os


def _env_int(name, default):
    if name in os.environ:
        return int(os.environ[name])
    return default


class Config(object):
    def __init__(self):
        self.version = "0.0.1"
        self.sampling_freq = 50  # in Hz  # main tick sampling (FPS of AC)
        self.sampling_time = 1.0 / self.sampling_freq
        self.ego_server_host_name = "0.0.0.0"
        self.ego_server_port = 2345
        self.ego_sampling_freq = 25  # in Hz # downsampled tick sampling (FPS of ego server)
        self.telemetry_sampling_freq = 0  # in Hz # 0 disables it

        self.opponents_server_host_name = "0.0.0.0"
        self.opponents_server_port = 2346

        self.simulation_management_server_host_name = "0.0.0.0"
        self.simulation_management_server_port = 2347

        self.enable_profiler = True
        self.vjoy_executed_by_server = False
        self.enable_debug_hotkeys = False
        if "ACGYM_DEBUG_HOTKEYS" in os.environ:
            self.enable_debug_hotkeys = os.environ["ACGYM_DEBUG_HOTKEYS"].strip().lower() in ("1", "true", "yes")

        # Alternative python interpreter
        self.enable_alternative_python_interpreter = False
        self.config_python_env_name = "p309"
        self.config_python_executable = None
        self.screen_capture_worker = "screen_capture_worker.py"

        # Multi-worker runtime (C4B): ACGYM_* via os.environ, defaults = single worker 0
        if "ACGYM_WORKER_ID" in os.environ:
            self.worker_id = int(os.environ["ACGYM_WORKER_ID"])
            worker_suffix = "_W{0}".format(self.worker_id)
        else:
            self.worker_id = 0
            worker_suffix = ""
        self.vjoy_device_id = _env_int("ACGYM_VJOY_DEVICE", 1)
        self.ego_server_port = _env_int("ACGYM_EGO_PORT", self.ego_server_port)
        self.opponents_server_port = _env_int("ACGYM_OPPONENTS_PORT", self.opponents_server_port)
        self.simulation_management_server_port = _env_int(
            "ACGYM_MGMT_PORT", self.simulation_management_server_port)
        self._load_worker_ports_from_python_ini()

        self.ego_sampling_freq_event_name = "Local\\EgoSamplingFreqEvent{0}".format(worker_suffix)

        self.presentation_mode = False
        self.presentation_camera = ""
        self.presentation_drivable_index = 0
        if "ACGYM_PRESENTATION_MODE" in os.environ:
            self.presentation_mode = os.environ["ACGYM_PRESENTATION_MODE"].strip().lower() in ("1", "true", "yes")
        if "ACGYM_PRESENTATION_CAMERA" in os.environ:
            self.presentation_camera = os.environ["ACGYM_PRESENTATION_CAMERA"].strip()
        if "ACGYM_PRESENTATION_DRIVABLE_INDEX" in os.environ:
            self.presentation_drivable_index = int(os.environ["ACGYM_PRESENTATION_DRIVABLE_INDEX"])
        self._load_presentation_from_python_ini()
        if self.presentation_drivable_index not in (0, 1):
            self.presentation_drivable_index = 0

        self.screen_capture_enable = False
        self.final_image_width = 640 // 2
        self.final_image_height = 480 // 2
        self.screen_capture_freq = 25
        self.screen_capture_save_to_disk = False
        self.screen_capture_save_path = "captures"
        self.screen_capture_verbose = False
        self.relocate_screen = True
        self.trigger_image_capture_event_name = "Local\\TriggerImageCapture{0}".format(worker_suffix)
        self.color_mode = "gray"

        if self.vjoy_executed_by_server:
            assert self.sampling_freq == 100, (
                "vJoy is executed by the server. The sampling frequency must be 100 Hz to avoid lagging.")
        else:
            assert self.sampling_freq == 50, (
                "vJoy is executed by the client. The sampling frequency must be 50 Hz to avoid lagging.")

    def _load_presentation_from_python_ini(self):
        """Read presentation keys once at startup from Documents cfg/python.ini."""
        if os.environ.get("ACGYM_PRESENTATION_MODE"):
            return
        home = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(home, "Documents", "Assetto Corsa", "cfg", "python.ini"),
            os.path.join(home, "OneDrive", "Documents", "Assetto Corsa", "cfg", "python.ini"),
        ]
        ini_path = None
        for path in candidates:
            if os.path.isfile(path):
                ini_path = path
                break
        if not ini_path:
            return
        section = "sensors_par"
        in_section = False
        data = {}
        try:
            with open(ini_path, "r") as ini_file:
                for raw_line in ini_file:
                    line = raw_line.strip()
                    if not line or line.startswith(";"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        in_section = (line[1:-1].strip() == section)
                        continue
                    if in_section and "=" in line:
                        key, val = line.split("=", 1)
                        data[key.strip()] = val.split(";")[0].strip()
        except (IOError, OSError):
            return
        if "PRESENTATION_MODE" in data:
            self.presentation_mode = data["PRESENTATION_MODE"].lower() in ("1", "true", "yes")
        if "PRESENTATION_CAMERA" in data and data["PRESENTATION_CAMERA"]:
            self.presentation_camera = data["PRESENTATION_CAMERA"]
        if "PRESENTATION_DRIVABLE_INDEX" in data:
            try:
                self.presentation_drivable_index = int(data["PRESENTATION_DRIVABLE_INDEX"])
            except ValueError:
                pass

    def _load_worker_ports_from_python_ini(self):
        """Fallback ports from Documents cfg/python.ini when ACGYM_* env absent in plugin."""
        if all(k in os.environ for k in ("ACGYM_EGO_PORT", "ACGYM_MGMT_PORT", "ACGYM_VJOY_DEVICE")):
            return
        home = os.environ.get("USERPROFILE", "")
        candidates = [
            os.path.join(home, "Documents", "Assetto Corsa", "cfg", "python.ini"),
            os.path.join(home, "OneDrive", "Documents", "Assetto Corsa", "cfg", "python.ini"),
        ]
        ini_path = None
        for path in candidates:
            if os.path.isfile(path):
                ini_path = path
                break
        if not ini_path:
            return
        section = "sensors_par"
        in_section = False
        data = {}
        try:
            with open(ini_path, "r") as ini_file:
                for raw_line in ini_file:
                    line = raw_line.strip()
                    if not line or line.startswith(";"):
                        continue
                    if line.startswith("[") and line.endswith("]"):
                        in_section = (line[1:-1].strip() == section)
                        continue
                    if in_section and "=" in line:
                        key, val = line.split("=", 1)
                        data[key.strip()] = val.split(";")[0].strip()
        except (IOError, OSError):
            return
        if "ACGYM_WORKER_ID" in data and "ACGYM_WORKER_ID" not in os.environ:
            self.worker_id = int(data["ACGYM_WORKER_ID"])
        if "ACGYM_EGO_PORT" in data and "ACGYM_EGO_PORT" not in os.environ:
            self.ego_server_port = int(data["ACGYM_EGO_PORT"])
        if "ACGYM_OPPONENTS_PORT" in data and "ACGYM_OPPONENTS_PORT" not in os.environ:
            self.opponents_server_port = int(data["ACGYM_OPPONENTS_PORT"])
        if "ACGYM_MGMT_PORT" in data and "ACGYM_MGMT_PORT" not in os.environ:
            self.simulation_management_server_port = int(data["ACGYM_MGMT_PORT"])
        if "ACGYM_VJOY_DEVICE" in data and "ACGYM_VJOY_DEVICE" not in os.environ:
            self.vjoy_device_id = int(data["ACGYM_VJOY_DEVICE"])


config = Config()
