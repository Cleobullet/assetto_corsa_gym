import sys
import socket
import json
import logging
import time
import threading
import pandas as pd
import numpy as np
import os
from contextlib import contextmanager
from typing import Optional
from AssettoCorsaEnv.pygame_joystick_handler import JoystickHandler
from AssettoCorsaPlugin.plugins.sensors_par.car_control import Controls

try:
    from ac_workers.udp_windows import configure_udp_client, recv_udp_safe
except ImportError:
    configure_udp_client = lambda _sock: None  # noqa: E731
    recv_udp_safe = None

if sys.platform.startswith("win"):
    import AssettoCorsaPlugin.plugins.sensors_par.dual_buffer as dual_buffer

logger = logging.getLogger(__name__)

MAX_MSG_SIZE = 2**18


class TelemetryConnectionTimeout(TimeoutError):
    """Ego UDP handshake did not complete within the allowed window."""

_DRIVER_CONTROLS_STARTUP_LOCK = threading.RLock()
NEUTRAL_STEER = 0
NEUTRAL_ACC = -1
NEUTRAL_BRAKE = -1

class SimulationManagement:
    def __init__(self, config):
        self.config = config
        self.host = config.simulation_management_server_host_name
        self.port = config.simulation_management_server_port
        self.max_msg_size = 2**20
        # Ephemeral TCP per request only. The in-game mgmt server is single-client:
        # it accept()s one socket and serves it in a loop until disconnect. A
        # persistent connection here blocks the PowerShell launcher from
        # resume_drive / get_driving_state during the Drive click sequence.

    def close(self) -> None:
        return

    def send_message(self, message, wait_response=False, *, max_attempts=4):
        from common.tcp_client import configure_client_socket, is_transient_tcp_error

        payload = message.encode()
        last_exc: BaseException | None = None
        for attempt in range(max_attempts):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    configure_client_socket(s, timeout=5.0)
                    s.connect((self.host, self.port))
                    s.sendall(payload)
                    if not wait_response:
                        return None
                    return self._recv_response(s)
            except OSError as exc:
                last_exc = exc
                if is_transient_tcp_error(exc) and attempt + 1 < max_attempts:
                    time.sleep(0.15 * (attempt + 1))
                    continue
                raise
        if last_exc is not None:
            raise last_exc
        return None

    def _recv_response(self, sock) -> str:
        """Read one mgmt response.

        The in-game mgmt server keeps the connection OPEN after replying (it
        serves a client in a loop and never closes), so we must NOT recv until
        EOF — that would block for the whole socket timeout and then discard the
        reply we already had. Instead: one blocking read for the first bytes,
        then a brief non-blocking drain for any continuation packets.
        """
        sock.settimeout(5.0)
        data = sock.recv(self.max_msg_size)
        if not data:
            return ""
        sock.settimeout(0.2)
        while len(data) < self.max_msg_size:
            try:
                more = sock.recv(min(65536, self.max_msg_size - len(data)))
            except (socket.timeout, OSError):
                break
            if not more:
                break
            data += more
        return data.decode()

    def send_reset(self):
        logger.info("sending reset to simulation management server")
        self.send_message("reset")
        time.sleep(1)

    def send_resume_drive(self):
        logger.info("sending resume_drive to simulation management server")
        self.send_message("resume_drive")

    def get_driving_state(self) -> dict:
        data = self.send_message("get_driving_state", wait_response=True)
        return json.loads(data)

    def get_track_info(self):
        data = self.send_message("get_track_info", wait_response=True)
        return eval(data)

    def get_static_info(self):
        static_info = eval(self.send_message("get_static_info", wait_response=True))
        track_full_name = f"{static_info['TrackName']}"
        if static_info['TrackConfiguration']:
            track_full_name += f"-{static_info['TrackConfiguration']}"
        static_info["TrackFullName"] = track_full_name
        return static_info

    def get_config(self):
        return eval(self.send_message("get_config", wait_response=True))


class Client():
    def __init__(self, config):
        self.config = config
        self.vjoy_executed_by_server = config.vjoy_executed_by_server
        self.server_host = config.ego_server_host_name
        self.server_port = config.ego_server_port
        self.simulation_management_server_host_name = config.simulation_management_server_host_name
        self.simulation_management_server_port = config.simulation_management_server_port
        self.state = ServerState()
        self._telemetry_frame_count = 0
        self.simulation_management = SimulationManagement(self.config)
        self.socket = None
        self.controls = DriverControls(self.vjoy_executed_by_server)
        self.record_controls_from_client = config.record_controls_from_client

        # Joystick handler (only used if needed)
        self.joystick_handler = None
        self.screen_capture_enable = self.config.screen_capture_enable
        self.camera = None
        self.current_image = None

        if self.screen_capture_enable:
            self.camera = dual_buffer.GrabberSharedMemoryDualBuffer(self.config.final_image_width,
                                                                       self.config.final_image_height,
                                                                       self.config.color_mode)

        if self.record_controls_from_client:
            self.joystick_handler = JoystickHandler()
            self.joystick_handler.initialize()

    def reply_to_server(self, msg):
        if not self.socket:
            return
        try:
            self.socket.sendto(msg.encode(), (self.server_host, self.server_port))
        except socket.error as emsg:
            logger.error(f"Error sending to server: {emsg}")
            raise TimeoutError

    def setup_connection(self, timeout_sec: Optional[float] = None):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        configure_udp_client(self.socket)
        self.socket.settimeout(2)
        deadline = None if timeout_sec is None else time.monotonic() + float(timeout_sec)
        logger.info("telemetry connect start port=%s", self.server_port)
        logger.info("AC Client. Listening at host: %s port: %s", self.server_host, self.server_port)

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TelemetryConnectionTimeout(
                    "sensors_par_not_streaming: no telemetry handshake on port {0} within {1:.1f}s".format(
                        self.server_port, float(timeout_sec)
                    )
                )
            self.reply_to_server("connect")
            logger.info("telemetry handshake sent port=%s", self.server_port)
            try:
                if recv_udp_safe:
                    data, addr = recv_udp_safe(self.socket, MAX_MSG_SIZE, "ego-client", self.server_port)
                else:
                    data, addr = self.socket.recvfrom(MAX_MSG_SIZE)
                if not data:
                    continue
                logger.info("telemetry packet received bytes=%s from=%s port=%s", len(data), addr, self.server_port)
                data = data.decode()
                logger.debug(f"Got from server: {data}")
                if data == "identified":
                    logger.info(f"Client connected on {self.server_port}")
                    break
            except socket.timeout:
                logger.warning("telemetry timeout port=%s", self.server_port)
                continue
            except ConnectionResetError:
                logger.warning("telemetry connection reset port=%s — retry handshake", self.server_port)
                continue
        self.get_servers_input(deadline=deadline)

    def get_current_image(self):
        if self.screen_capture_enable:
            return self.current_image
        else:
            return None

    def get_servers_input(self, deadline: Optional[float] = None):
        if not self.socket:
            return

        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TelemetryConnectionTimeout(
                    "no telemetry packet on port {0} before attach deadline".format(self.server_port)
                )
            try:
                if recv_udp_safe:
                    data, _ = recv_udp_safe(self.socket, MAX_MSG_SIZE, "ego-client", self.server_port)
                else:
                    data, _ = self.socket.recvfrom(MAX_MSG_SIZE)
                if not data:
                    continue
                data = data.decode()
                logger.debug(f"Got from server: {data}")
                if data == "disconnect":
                    logger.info("Server stopped the connection")
                    self.state["done"] = True
                elif data == "identified":
                    logger.info("Server identified")
                else:
                    try:
                        self.state.parse_server_str(data)
                        self._telemetry_frame_count += 1
                    except Exception as parse_exc:
                        logger.warning("telemetry parse error port=%s: %s", self.server_port, parse_exc)
                        continue
                    if self.screen_capture_enable:
                        _, _, img_array = self.camera.get_active_image()
                        self.current_image = img_array
                break
            except socket.timeout:
                logger.warning("telemetry timeout port=%s", self.server_port)
                continue
            except ConnectionResetError:
                logger.warning("telemetry connection reset port=%s — retry poll", self.server_port)
                continue

    def respond_to_server(self):
        if not self.vjoy_executed_by_server:
            if self.controls.local_vjoy_feeder_enabled():
                self.controls.apply_local_controls()
        else:
            if not self.socket:
                return
            self.controls["server_steps"] = self.state["steps"] # send back
            self.reply_to_server(self.controls.export())

    def reset(self, send_reset=True, connection_timeout_sec: Optional[float] = None):
        if self.socket:
            self.close()
        if send_reset:
            self.simulation_management.send_reset()
        self.controls.set_defaults()
        self.setup_connection(timeout_sec=connection_timeout_sec)

    def close(self):
        # Send disconnect message
        if not self.vjoy_executed_by_server:
            self.controls.set_defaults()
            if self.controls.local_vjoy_feeder_enabled():
                self.controls.apply_local_controls()
        self.reply_to_server("disconnect")
        time.sleep(0.1)
        if self.socket:
            self.socket.close()
        self.socket = None
        self.simulation_management.close()

    def discard_pending_telemetry(self):
        """Discard startup backlog only; normal training keeps every transition."""
        import select
        count = 0
        deadline = time.monotonic() + 0.25
        while self.socket and count < 4096 and time.monotonic() < deadline:
            ready, _, _ = select.select([self.socket], [], [], 0)
            if not ready:
                break
            self.socket.recvfrom(MAX_MSG_SIZE)
            count += 1
        return count

    def step_sim(self):
        self.get_servers_input()
        state = self.state.copy()

        if self.record_controls_from_client and self.joystick_handler and self.joystick_handler.is_initialized():
            state.update(self.joystick_handler.read_inputs())
        return state

    def export_track_and_racing_line(self, output_path="."):
        os.makedirs(output_path, exist_ok=True)

        track_info = self.simulation_management.get_track_info()
        static_info = self.simulation_management.get_static_info()

        left_border_x = np.array( track_info['left_lane'] )[:,0]
        left_border_y = np.array( track_info['left_lane'] )[:,1]

        right_border_x = np.array( track_info['right_lane'] )[:,0]
        right_border_y = np.array( track_info['right_lane'] )[:,1]

        fast_lane_x = np.array( track_info['fast_lane'] )[:,0]
        fast_lane_y = np.array( track_info['fast_lane'] )[:,1]

        # save track borders
        track_full_name = f"{static_info['TrackName']}"
        if static_info['TrackConfiguration']:
            track_full_name += f"-{static_info['TrackConfiguration']}"

        track_file_name = f"{output_path}/{track_full_name}.csv"
        df = pd.DataFrame({
            'left_border_x': left_border_x,
            'left_border_y': left_border_y,
            'right_border_x': right_border_x,
            'right_border_y': right_border_y,
            'pos_x': fast_lane_x,
            'pos_y': fast_lane_y
        })
        logger.info(f"Saving track to {track_file_name}")
        df.to_csv(f"{track_file_name}", index=False)

        print(static_info)

        # save racing line for that car -> TODO check if this is car dependent
        racing_line_file_name = f"{output_path}/{static_info['TrackName']}"
        if static_info['TrackConfiguration']:
            racing_line_file_name += f"-{static_info['TrackConfiguration']}"
        #racing_line_file_name += f"-{static_info['CarName']}"
        racing_line_file_name += f"-racing_line.csv"
        logger.info(f"Saving racing line {racing_line_file_name}")

        df = pd.DataFrame({
            'pos_x': fast_lane_x,
            'pos_y': fast_lane_y
        })
        df.to_csv(f"{racing_line_file_name}", index=False)
        return track_full_name, track_file_name, racing_line_file_name, static_info

class ServerState(dict):
    def __init__(self):
        self["done"] = False

    def parse_server_str(self, server_string):
        self.update( eval(server_string) )

class DriverControls(dict):
    def __init__(self, vjoy_executed_by_server=False, local_controls=None):
        self.vjoy_executed_by_server = vjoy_executed_by_server
        self._startup_override_active = False
        self._startup_override_action: tuple[float, float, float] | None = None
        self._startup_saved_state: dict | None = None
        self._startup_control_write_count = 0
        self._startup_last_control_writer = ""
        self._startup_neutral_overwrite_detected = False
        # Block vJoy writes until collector_live_attach completes ensure_vjoy_feeder().
        self._local_vjoy_enabled = False

        self.set_defaults()

        if not self.vjoy_executed_by_server:
            logger.warning("Controls will be executed locally and not by the server")
            self.local_controls = local_controls if local_controls is not None else Controls()

    def enable_local_vjoy_feeder(self) -> None:
        self._local_vjoy_enabled = True
        lc = getattr(self, "local_controls", None)
        if lc is not None and hasattr(lc, "enable_feeder"):
            lc.enable_feeder()

    def local_vjoy_feeder_enabled(self) -> bool:
        return bool(self._local_vjoy_enabled)

    def startup_diagnostics(self) -> dict:
        action = self._startup_override_action or (
            float(self["steer"]),
            float(self["acc"]),
            float(self["brake"]),
        )
        lc = getattr(self, "local_controls", None)
        readback_pedal = float(getattr(lc, "acc", 0.0)) if lc is not None else 0.0
        return {
            "startup_controls_object_id": id(lc) if lc is not None else None,
            "startup_override_active": bool(self._startup_override_active),
            "startup_requested_internal_action": [float(action[0]), float(action[1]), float(action[2])],
            "startup_requested_pedal_fraction": (float(action[1]) + 1.0) / 2.0,
            "startup_readback_pedal_fraction": readback_pedal,
            "startup_control_write_count": int(self._startup_control_write_count),
            "startup_neutral_overwrite_detected": bool(self._startup_neutral_overwrite_detected),
            "startup_last_control_writer": str(self._startup_last_control_writer),
        }

    @staticmethod
    def _is_neutral_command(steer: float, acc: float, brake: float) -> bool:
        return (
            float(steer) == NEUTRAL_STEER
            and float(acc) == NEUTRAL_ACC
            and float(brake) == NEUTRAL_BRAKE
        )

    def _write_local_controls(
        self,
        steer: float,
        acc: float,
        brake: float,
        *,
        enable_gear_shift: bool = False,
        shift_up: bool = False,
        shift_down: bool = False,
        writer: str = "",
    ) -> None:
        self["steer"] = steer
        self["acc"] = acc
        self["brake"] = brake
        self["enable_gear_shift"] = enable_gear_shift
        self["shift_up"] = shift_up
        self["shift_down"] = shift_down
        if writer:
            self._startup_control_write_count += 1
            self._startup_last_control_writer = writer
        if not self.vjoy_executed_by_server and hasattr(self, "local_controls"):
            if not self._local_vjoy_enabled:
                return
            self.local_controls.set_controls(
                steer=steer,
                acc=acc,
                brake=brake,
                enable_gear_shift=enable_gear_shift,
                shift_up=shift_up,
                shift_down=shift_down,
            )

    @contextmanager
    def startup_control_override(
        self,
        steer: float,
        acc: float,
        brake: float,
        *,
        writer: str = "startup_control_override",
    ):
        with _DRIVER_CONTROLS_STARTUP_LOCK:
            self._startup_saved_state = {
                "steer": self["steer"],
                "acc": self["acc"],
                "brake": self["brake"],
                "enable_gear_shift": self["enable_gear_shift"],
                "shift_up": self["shift_up"],
                "shift_down": self["shift_down"],
            }
            self._startup_override_active = True
            self._startup_override_action = (float(steer), float(acc), float(brake))
            self._startup_control_write_count = 0
            self._startup_neutral_overwrite_detected = False
            self._startup_last_control_writer = writer
            try:
                self._write_local_controls(steer, acc, brake, writer=writer)
                yield self
            finally:
                self._startup_override_active = False
                self._startup_override_action = None
                self["steer"] = NEUTRAL_STEER
                self["acc"] = NEUTRAL_ACC
                self["brake"] = NEUTRAL_BRAKE
                self["enable_gear_shift"] = 0
                self["shift_up"] = 0
                self["shift_down"] = 0
                if not self.vjoy_executed_by_server and hasattr(self, "local_controls"):
                    self.local_controls.set_controls(
                        steer=NEUTRAL_STEER,
                        acc=NEUTRAL_ACC,
                        brake=NEUTRAL_BRAKE,
                    )
                self._startup_saved_state = None

    def set_defaults(self):
        if self._startup_override_active:
            self._startup_neutral_overwrite_detected = True
            return
        self["steer"] = NEUTRAL_STEER
        self["acc"] = NEUTRAL_ACC
        self["brake"] = NEUTRAL_BRAKE
        self["enable_gear_shift"] = 0
        self["shift_up"] = 0
        self["shift_down"] = 0

    def set_controls(self, steer, acc, brake, enable_gear_shift=False, shift_up=False, shift_down=False):
        if self._startup_override_active:
            if self._is_neutral_command(steer, acc, brake):
                self._startup_neutral_overwrite_detected = True
                return
            self._startup_override_action = (float(steer), float(acc), float(brake))
        self._write_local_controls(
            steer,
            acc,
            brake,
            enable_gear_shift=enable_gear_shift,
            shift_up=shift_up,
            shift_down=shift_down,
            writer="set_controls",
        )

    def apply_local_controls(self):
        if self._startup_override_active and self._startup_override_action is not None:
            steer, acc, brake = self._startup_override_action
            self._write_local_controls(steer, acc, brake, writer="apply_local_controls")
            return
        self._write_local_controls(
            self["steer"],
            self["acc"],
            self["brake"],
            enable_gear_shift=self["enable_gear_shift"],
            shift_up=self["shift_up"],
            shift_down=self["shift_down"],
            writer="apply_local_controls",
        )

    def export(self):
        return json.dumps(self)