"""Diagnostic bootstrap log for sensors_par (Python 3.3 compatible)."""
import os
import time
import traceback

_LOG_NAME = "sensors_par_bootstrap.log"


def _log_path():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(here, _LOG_NAME)
    except Exception:
        return _LOG_NAME


def write(msg):
    try:
        line = "{0} {1}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        path = _log_path()
        with open(path, "a") as handle:
            handle.write(line)
    except Exception:
        pass


def write_exception(msg):
    try:
        write(msg)
        write(traceback.format_exc())
    except Exception:
        pass
