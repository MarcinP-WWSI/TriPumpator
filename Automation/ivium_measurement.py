# ivium_measurement.py
"""Thin wrapper around pyvium for starting IviumSoft measurements.

Public API:
    start(file_path)          -- begin a measurement from a .imf file
    is_running()              -- True while the measurement is active
    is_finished()            -- True once the device is idle again
    get_point_count()        -- number of data points collected so far
    shutdown(abort=True)     -- abort, remove the temp copy, close the driver

Set SIM=1 to run against a fake pyvium (no IviumSoft, no device).
In simulation mode the module attribute `sim` holds the FakePyvium
instance (call log, timing knobs, reset()/force_finish() helpers).
"""

import os
import shutil
import sys
import tempfile
import time
import threading

SIMULATION = os.environ.get("SIM") == "1"

if SIMULATION:
    import types

    # Mirror of pyvium.errors so app imports keep working in SIM mode
    class IviumSoftNotRunningError(Exception): pass
    class DeviceNotConnectedToIviumSoftError(Exception): pass
    class DeviceBusyError(Exception): pass
    class IllegalCommandError(Exception): pass

    errors = types.ModuleType("pyvium.errors")
    for _exc in (IviumSoftNotRunningError,
                 DeviceNotConnectedToIviumSoftError,
                 DeviceBusyError,
                 IllegalCommandError):
        setattr(errors, _exc.__name__, _exc)

    class FakePyvium:
        """Simulates pyvium.Pyvium with realistic timing.

        Timeline after start_method (t0):
          t0 .. t0+startup        idle (device takes a moment to start)
          t0+startup .. +prephase busy, 0 points (pre-measurement stages)
          .. t_end                busy, points ramp 0 -> total_points
          after t_end             idle again

        abort_method(): stays busy for abort_delay_s, then idle.
        Aborting with nothing running raises IllegalCommandError,
        starting while running raises DeviceBusyError — both match
        the real pyvium behaviour (notebooks 01/06).
        """

        def __init__(self):
            self.calls = []
            self.status_polls = 0
            self.point_polls = 0
            self.reload_timing()
            self._t0 = self._t_busy = self._t_end = None
            self._aborted_at = None

        def reload_timing(self):
            """Timing-i czytane Z ŻYCIA env (nie tylko z importu) —
            testy/debug zmieniają SIM_* bez restartu procesu."""
            self.startup_delay = float(os.environ.get("SIM_STARTUP", "0.3"))
            self.prephase_s = float(os.environ.get("SIM_PREPHASE", "2.0"))
            self.duration_s = float(os.environ.get("SIM_DURATION", "10.0"))
            self.total_points = int(os.environ.get("SIM_POINTS", "160"))
            self.abort_delay_s = float(
                os.environ.get("SIM_ABORT_DELAY", "0.5"))
            self.fail_mode = os.environ.get("SIM_FAIL", "").lower()

        # -- test helpers --
        def reset(self):
            self.calls.clear()
            self.status_polls = self.point_polls = 0
            self._t0 = self._t_busy = self._t_end = None
            self._aborted_at = None

        def force_finish(self):
            if self._t0 is not None:
                self._t_end = time.time()

        def _record(self, *call):
            self.calls.append(call)

        def _running(self):
            if self._t0 is None:
                return False
            now = time.time()
            if self._aborted_at is not None:
                return now - self._aborted_at < self.abort_delay_s
            if now >= self._t_end:
                return False
            return now >= self._t_busy

        def _points(self):
            if self._t0 is None:
                return 0
            elapsed = time.time() - self._t_busy
            if self._aborted_at is not None:
                elapsed = self._aborted_at - self._t_busy
            if elapsed < self.prephase_s:
                return 0
            frac = min(1.0, (elapsed - self.prephase_s) / self.duration_s)
            return int(frac * self.total_points)

        # -- pyvium API --
        def open_driver(self):
            self._record("open_driver")
            if self.fail_mode == "no_iviumsoft":
                raise IviumSoftNotRunningError("SIM_FAIL=no_iviumsoft")

        def close_driver(self):
            self._record("close_driver")

        def start_method(self, path=""):
            # ORYGINALNA logika fake'a — ŻADNEJ pętli budżetu tutaj:
            # czekanie na busy siedzi w module-level start() (wrapper).
            # Ta wersja ustawia _t0 natychmiast i nie ma prawa paść.
            self._record("start_method", path)
            if self.fail_mode == "start":
                raise DeviceNotConnectedToIviumSoftError("SIM_FAIL=start")
            if self._running():
                raise DeviceBusyError("measurement already running (sim)")
            self._aborted_at = None
            self._t0 = time.time()
            self._t_busy = self._t0 + self.startup_delay
            self._t_end = self._t_busy + self.prephase_s + self.duration_s

        def abort_method(self):
            self._record("abort_method")
            if not self._running():
                raise IllegalCommandError("no active measurement (sim)")
            self._aborted_at = time.time()

        def get_device_status(self):
            self.status_polls += 1
            return (2, "busy_measure") if self._running() \
                else (1, "available_idle")

        def get_available_data_points_number(self):
            self.point_polls += 1
            return self._points()

    fake = types.ModuleType("pyvium")
    fake.Pyvium = FakePyvium()
    fake.errors = errors
    sys.modules["pyvium"] = fake
    sys.modules["pyvium.errors"] = errors

from pyvium import Pyvium

sim = Pyvium if SIMULATION else None   # exposed for tests

# Module state
_temp_copy_path = None
_driver_open = False
_saw_busy = False
_assumed_done = False
_lock = threading.Lock()


def start(file_path):
    """Start a measurement from the given .imf file.

    Returns the path handed to the DLL (a temp copy when the
    original path contains non-ASCII characters).
    Raises FileNotFoundError if the file does not exist.
    """
    global _temp_copy_path, _saw_busy, _assumed_done
    if not os.path.isfile(file_path):
        raise FileNotFoundError(file_path)

    with _lock:
        _cleanup_temp_copy()
        path = file_path
        _saw_busy, _assumed_done = False, False

        # The Ivium DLL reads paths as ANSI while pyvium encodes UTF-8 —
        # non-ASCII in the path means "not found" inside the DLL.
        if not path.isascii():
            path = _copy_to_ascii_dir(path)

        _open_driver()
        Pyvium.start_method(path.replace("\\", "/"))

        # Wait for busy (status 2) so is_finished() does not lie during
        # startup. Budżet SKALOWANY z czasu startu: fake z SIM_STARTUP
        # > 3 s wchodzi w busy poźniej niż stary budżet — wtedy
        # _assumed_done fałszował bramki MEAS_RUNNING (procedures3/4).
        # Realny pyvium nie ma startup_delay -> getattr default 0.0
        # -> budżet zostaje 3 s jak dotąd.
        t0 = time.time()
        budget = max(3.0, getattr(Pyvium, "startup_delay", 0.0) + 1.0)
        while time.time() - t0 < budget:
            if _device_status_code() == 2:
                _saw_busy = True
                break
            time.sleep(0.1)
        if not _saw_busy:
            _assumed_done = True   # method ended before busy showed up
        return path


def is_running():
    if _assumed_done:
        return False
    return _device_status_code() == 2


def is_finished():
    return not is_running()


def get_point_count():
    _open_driver()
    return Pyvium.get_available_data_points_number()


def shutdown(abort=True, timeout_s=30):
    """Abort (optional), remove the temp .imf copy, close the driver.
    Returns True if a measurement was actually aborted."""
    global _driver_open
    aborted = False
    with _lock:
        if abort and is_running():
            try:
                Pyvium.abort_method()
                aborted = True
            except Exception:
                pass   # e.g. nothing was running after all
            t0 = time.time()
            while _device_status_code() == 2 and time.time() - t0 < timeout_s:
                time.sleep(0.2)

        _cleanup_temp_copy()
        if _driver_open:
            try:
                Pyvium.close_driver()
            except Exception:
                pass
            _driver_open = False
    return aborted


# ------------------------- internals -------------------------

def _device_status_code():
    _open_driver()
    code, _label = Pyvium.get_device_status()
    return code


def _open_driver():
    global _driver_open
    if not _driver_open:
        Pyvium.open_driver()
        _driver_open = True


def _copy_to_ascii_dir(src_path):
    global _temp_copy_path
    dst = os.path.join(tempfile.gettempdir() or "C:", "pyvium_imf",
                       os.path.basename(src_path))
    if not os.path.dirname(dst).isascii():   # non-ASCII %TEMP%
        dst = os.path.join("C:", os.sep, "pyvium_imf",
                           os.path.basename(src_path))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src_path, dst)
    _temp_copy_path = dst
    return dst


def _cleanup_temp_copy():
    global _temp_copy_path
    if _temp_copy_path is None:
        return
    try:
        os.remove(_temp_copy_path)
        os.rmdir(os.path.dirname(_temp_copy_path))   # only if now empty
    except OSError:
        pass   # the DLL may still hold a handle; next start() overwrites
    _temp_copy_path = None