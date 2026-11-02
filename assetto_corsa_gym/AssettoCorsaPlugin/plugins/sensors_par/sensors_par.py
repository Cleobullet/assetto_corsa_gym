import json
import pickle
import sys
import os
import platform
import time
import traceback
import datetime

try:
    import bootstrap_log
    bootstrap_log.write("import start")
except Exception:
    pass

import ac
import acsys

# setup logger
import ac_logger
ac_logger.setup_logger()

import logging
logger = logging.getLogger(__name__)
logger.info("Starting sensors_par...")

base_path = os.path.dirname(sys.executable)
logger.info("Python path: {}".format(sys.executable))
logger.info("Base path: {}".format(base_path))
logger.info("Python version: {}".format(sys.version))
logger.info("Architecture: {}".format(platform.architecture()))

# Define the subdirectories to add
subdirs = [
    "system\\x64\\DLLs",
    "system\\x64\\Lib"
]

# Combine the base path with each subdirectory
for subdir in subdirs:
    full_path = os.path.join(base_path, subdir)

    # Check if the full path exists and raise an exception if it does not
    if not os.path.exists(full_path):
        raise FileNotFoundError("The specified path does not exist: {}".format(full_path))
    else:
        sys.path.insert(0, full_path)

try:
    from structures import Car
    from structures import Track
    from structures import Opponent
    from structures import StaticInfo
    from car_control import Controls
    from record_telemetry import Telemetry
    from ego_server import EgoServer
    from config import config
    from drive_state import (
        get_driving_state_json,
        queue_reset_car,
        queue_resume_drive,
        tick_drive_state,
    )
    try:
        bootstrap_log.write(
            "config OK worker={0} ego={1} opp={2} mgmt={3} vjoy={4}".format(
                config.worker_id,
                config.ego_server_port,
                config.opponents_server_port,
                config.simulation_management_server_port,
                config.vjoy_device_id,
            ))
    except Exception:
        pass
    if config.vjoy_executed_by_server:
        controls = Controls()
    else:
        controls = None
    if config.enable_alternative_python_interpreter:
        import alternative_python
except Exception:
    try:
        bootstrap_log.write_exception("plugin import failed")
    except Exception:
        pass
    logging.exception("An error occurred")
    raise

try:
    import socket
    import threading
    import time
except:
    logging.exception("An error occurred")
    raise

car_export = str()
telemetry = Telemetry()

DONE_STATIC_INFO = False
alternative_interpreter = None

presentation_camera_state = "idle"
presentation_camera_try_count = 0
presentation_camera_wait_until = 0.0
PRESENTATION_CAMERA_MAX_TRIES = 3
PRESENTATION_CAMERA_DELAY_SEC = 0.5


def _presentation_chase_label(index):
    if index == 1:
        return "Chase2"
    return "Chase1"


def ApplyPresentationCamera():
    """Set Drivable chase camera from acUpdate main thread only."""
    ac.focusCar(0)
    ac.setCameraMode(acsys.CM.Drivable)
    if hasattr(ac, "ext_setCurrentDrivableCamera"):
        ac.ext_setCurrentDrivableCamera(config.presentation_drivable_index)
    mode = ac.getCameraMode()
    if mode == acsys.CM.Track:
        logger.warning("presentation camera: Track mode forbidden, forcing Drivable")
        ac.setCameraMode(acsys.CM.Drivable)
        if hasattr(ac, "ext_setCurrentDrivableCamera"):
            ac.ext_setCurrentDrivableCamera(config.presentation_drivable_index)
        mode = ac.getCameraMode()
    return mode == acsys.CM.Drivable


def tick_presentation_camera():
    global presentation_camera_state, presentation_camera_try_count, presentation_camera_wait_until
    if presentation_camera_state == "done":
        return
    if not config.presentation_mode or config.presentation_camera != "ThirdPerson":
        return
    now = time.time()
    if presentation_camera_state == "idle":
        try:
            if not ac.isConnected(0):
                return
        except Exception:
            return
        label = _presentation_chase_label(config.presentation_drivable_index)
        logger.info("presentation camera requested: Drivable {0}".format(label))
        presentation_camera_state = "waiting"
        presentation_camera_wait_until = now + PRESENTATION_CAMERA_DELAY_SEC
        return
    if presentation_camera_state == "waiting":
        if now < presentation_camera_wait_until:
            return
        presentation_camera_state = "applying"
    if presentation_camera_state == "applying":
        if presentation_camera_try_count >= PRESENTATION_CAMERA_MAX_TRIES:
            presentation_camera_state = "done"
            logger.warning("presentation camera: max retries reached")
            return
        ok = ApplyPresentationCamera()
        logger.info(
            "presentation camera mode={0} index={1}".format(
                ac.getCameraMode(), config.presentation_drivable_index))
        if ok:
            logger.info("presentation camera ready")
            presentation_camera_state = "done"
            return
        presentation_camera_try_count += 1
        presentation_camera_state = "waiting"
        presentation_camera_wait_until = now + PRESENTATION_CAMERA_DELAY_SEC


def reset_car():
    queue_reset_car()

def opponents_server_task():
    global opponents, config

    host = config.opponents_server_host_name
    port = config.opponents_server_port

    ss = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ss.bind((host, port))
    logger.info("[OPP SERV] Start opponents cars socket on: {}:{}".format(host, port))
    ss.listen(5)

    while True:
        logger.info("[OPP SERV] Waiting for connections.")
        (cs, addr) = ss.accept()

        # Sending number of opponents
        logger.info("[OPP SERV] Client connected, sending data.")
        cs.send(str(len(opponents)).encode('utf8'))

        try:
            while True:
                opponents_list = []
                for opp in opponents:
                    opp.update()
                    opponents_list.append(opp)
                msg = str(len(str(opponents_list))) + "HEADER-END" + str(opponents_list)
                cs.send(msg.encode())
        except:
            logger.info("[OPP SERV] Client disconnected.")
            cs.close()

def simulation_management_server_task():
    global opponents, config

    try:
        host = config.simulation_management_server_host_name
        port = config.simulation_management_server_port

        ss = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ss.bind((host, port))
        logger.info("[MGMT SERV] Start simulation management socket on: {}:{}".format(host, port))
        ss.listen(5)

        while True:
            cs = None
            try:
                logger.info("[MGMT SERV] Waiting for connections.")
                (cs, addr) = ss.accept()
                cs.settimeout(2.0)

                # Sending number of opponents
                logger.info("[MGMT SERV] Client connected, sending data.")

                while True:
                    # Receive data from the client
                    data = cs.recv(1024).decode("utf8")
                    if not data:
                        break
                    logger.info("[MGMT SERV] Received: {}".format(data))
                    if data == "reset":
                        reset_car()
                    elif data.startswith("spawn "):
                        try:
                            from drive_state import queue_spawn
                            queue_spawn(float(data.split()[1]))
                        except Exception:
                            logger.exception("[MGMT SERV] spawn failed")
                    elif data == "resume_drive":
                        queue_resume_drive()
                    elif data == "get_driving_state":
                        cs.sendall(get_driving_state_json(controls).encode("utf8"))
                        logger.info("Sending driving state")
                    elif data == "get_track_info":
                        track_export = track.export()
                        logger.info(type(track_export))
                        logger.info(len(track_export))
                        cs.sendall(track.export().encode('utf8'))
                        logger.info("Sending track info")
                    elif data == "get_static_info":
                        static_info.collect_static_info()
                        cs.sendall(static_info.export().encode('utf8'))
                        logger.info("Sending static info")
                    elif data == "get_config":
                        cs.sendall(str(config.__dict__).encode('utf8'))
                        logger.info("Sending config")
                    else:
                        logger.info("[MGMT SERV] Unknown command: {}".format(data))
                        break
            except (ConnectionResetError, socket.timeout):
                logger.exception("[MGMT SERV] Client disconnected")
            except:
                logger.exception("[MGMT SERV] An error occurred")
            finally:
                if cs is not None:
                    cs.close()
    except:
        logger.exception("[OPP SERV] An error occurred")

# The function has to return a string with the plugin name
# Not all the tracks are saved equal in AC, so it can retrieve wrong info
# about the track. (Tested on track "Magione")
def acMain(ac_version):
    global car, track, static_info, opponents, ego_server, telemetry, config, controls, alternative_interpreter

    try:
        bootstrap_log.write("acMain called version={0}".format(ac_version))
    except Exception:
        pass

    #Only for specific configurations of the track
    conf = ac.getTrackConfiguration(0)
    if conf == -1:
        track = Track(ac.getTrackName(0))
    else:
        track = Track(ac.getTrackName(0), conf)

    car_id = 0
    car = Car(car_id)
    static_info = StaticInfo(car_id)
    opponents = []

    #Opponents
    carIds = range(1, ac.getCarsCount(), 1)
    for carId in carIds:
        #first we'll check wether there is a car for this id; as soon it returns -1
        if str(ac.getCarName(carId)) == '-1':
            continue#break
        else:
            temp_opp = Opponent(carId)
            opponents.append(temp_opp)
            logger.info("added opponent")


    logger.info("[MAIN] Started.")
    appWindow = ac.newApp("sensors_par")
    ac.setSize(appWindow, 333, 173)

    try:
        ego_server = EgoServer(config, car, controls, telemetry, track=track)
        ego_server.start()  # This will start the server in a separate thread
    except:
        logger.error()
        raise



    # Starting opponents task
    try:
        if len(opponents) != 0:
            logger.info("[MAIN] Starting opponent task")
            thread_opps = threading.Thread(target=opponents_server_task)
            thread_opps.daemon = True
            thread_opps.start()
    except:
        logger.error()
        raise

    # Starting management task
    try:
        logger.info("[MAIN] Starting MGR task")
        time.sleep(1)
        thread_mgr = threading.Thread(target=simulation_management_server_task)
        thread_mgr.daemon = True
        thread_mgr.start()
        try:
            bootstrap_log.write(
                "management thread started port={0}".format(config.simulation_management_server_port))
        except Exception:
            pass
    except Exception:
        try:
            bootstrap_log.write_exception("management thread failed")
        except Exception:
            pass
        logger.error()
        raise

    # Starting screen capture worker in a new process and Python interpreter
    # using a newer version than default in AC (Python3.3)
    if config.enable_alternative_python_interpreter:
        alternative_interpreter = alternative_python.ProducerSpawner(config.screen_capture_worker,
                                                                     config.config_python_executable,
                                                                     config.config_python_env_name)
    else:
        alternative_interpreter = None
    return "sensors_par"

def acShutdown():
    global controls, alternative_interpreter
    if controls:
        controls.close()
    if alternative_interpreter:
        alternative_interpreter.close()
    logger.info("[MAIN] Stopped.")

# This constantly updates with dynamic car info
def acUpdate(deltaT):
    global car_export, car, track, static_info, telemetry, ego_server

    if not static_info.done_static_info:
        static_info.collect_static_info()
        telemetry.set_static_info(static_info.export())
        return

    if config.enable_debug_hotkeys and ac.ext_isAltPressed():
        if ac.ext_isButtonPressed("W"):
            ac.ext_takeAStepBack() # same as reset
            logger.info("[MAIN] Step back.")

        if ac.ext_isButtonPressed("R"):
            if telemetry.recording:
                telemetry.stop_recording()
                telemetry.save_telemetry()
                logger.info("[MAIN] Stop recording.")

        if ac.ext_isButtonPressed("E"):
            if not telemetry.recording:
                logger.info("[MAIN] Start recording.")
                telemetry.start_recording()

        if ac.ext_isButtonPressed("R"):
            reset_car()

        if ac.ext_isButtonPressed("T"):
            #log_message("Car: {}".format(ac.getCarName(car.car_id)))
            if controls:
                controls.set_controls(.01, 5., -1)

    tick_presentation_camera()
    tick_drive_state(controls, deltaT, getattr(config, "worker_id", 0))

    # call ego server tick
    ego_server.tick()