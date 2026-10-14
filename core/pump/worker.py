"""Wątek roboczy pompy: kolejka Op + polling + klasyfikacja błędów.

PumpHandle — obiekt widziany przez GUI:
    submit(Op, **kw) -> bool     bezpieczne z GUI-wątku
    sygnały (status/version/error/offline/online) — patrz PumpWorker
    close()                      czysty shutdown pompy (woła Session)

Pętla: komendy z kolejki mają priorytet; pusta kolejka -> POLL co
poll_period. Jeden worker = jedna pompa = jeden wątek.
"""

from __future__ import annotations

import queue
import time

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from core.pump.base import Op, PumpStatus
from core.pump.demo import DemoDriver
from core.transport.serial_port import TransportError

class PumpHandle:
    """Rękojmia GUI: wrzuca Op, odbiera sygnały. Nic więcej."""

    def __init__(self, worker: "PumpWorker", thread: QThread):
        self._worker = worker
        self._thread = thread
        self.capabilities = worker.driver.capabilities

        # sygnały przekierowane 1:1 — GUI podpina się pod handle, nie worker
        self.status = worker.status
        self.version = worker.version
        self.error = worker.error
        self.online = worker.online
        self.offline = worker.offline

    def submit(self, op: Op, **kw) -> bool:
        """Wrzuć operację. False = worker już zamknięty."""
        try:
            self._worker._queue.put((op, kw))
            return True
        except Exception:
            return False

    def close(self) -> None:
        """Czysty shutdown — wywoływać TYLKO z GUI-wątku (Session)."""
        self._worker._queue.put((None, {}))          # wyjście z pętli
        self._thread.quit()
        self._thread.wait(2000)


class PumpWorker(QObject):
    # statusy i wyniki komend (GUI rysuje z tego)
    status = pyqtSignal(object)                  # PumpStatus
    version = pyqtSignal(str)                    # z query_version()
    error = pyqtSignal(str)                      # tekst błędu komendy
    online = pyqtSignal()                        # pompa odpowiedziała
    offline = pyqtSignal()                       # pompa zamilkła (po N razach)
    closed = pyqtSignal()                        # pętla zakończona

    _OFFLINE_THRESHOLD = 3       # kolejnych cisz -> offline
    _RECONNECT_DELAY = 1.0       # s między próbami w stanie offline

    def __init__(self, driver, poll_period: float = 0.5, parent=None):
        super().__init__(parent)
        self.driver = driver
        self._poll_period = poll_period
        self._queue: queue.Queue = queue.Queue()
        self._running = True
        self._silent = 0            # licznik kolejnych braków odpowiedzi
        self._link_up = False


    # ------------------------------------------------------------- pętla

    @pyqtSlot()
    def run(self):
        self._identify()
        while self._running:
            if not self._link_up:
                self._probe()
                continue
            try:
                item = self._queue.get(timeout=self._poll_period)
            except queue.Empty:
                item = (Op.POLL, {})
            if item[0] is None:                       # sygnał zamknięcia
                break
            op, kw = item
            self._dispatch(op, kw)
        self.closed.emit()

    # ------------------------------------------------------- pomocnicze

    def _identify(self):
        """Identyfikacja pompy przy starcie; porażka -> tryb offline."""
        try:
            v = self.driver.query_version()
        except Exception:
            # start bez pompy — wchodzimy w offline i żyjemy (autoreconnect)
            self._link_up = False
            self.offline.emit()
            return
        self._link_up = True
        self.version.emit(v)
        self.online.emit()

    def _probe(self):
        """Próba odzyskania łączności; wołane w pętli gdy link down."""
        time.sleep(self._RECONNECT_DELAY)
        try:
            v = self.driver.query_version()
        except Exception:
            return
        self._link_up = True
        self._silent = 0
        self.version.emit(v)
        self.online.emit()

    def _dispatch(self, op: Op, kw: dict):
        try:
            if op is Op.POLL:
                # cisza policzona w _do_poll — tu NIE wolno wołać
                # _mark_online (zerowałaby licznik histererezy)
                if (st := self._do_poll()) is None:
                    return
            else:
                st = self.driver.execute(op, **kw)
            self.status.emit(st)
            self._mark_online()
        except TransportError as e:
            self._mark_offline(str(e), try_reopen=True)
        except TimeoutError:
            self._count_silence()      # komenda też może wygasnąć
        except Exception as e:         # PumpCommandError itd. — worker żyje
            self.error.emit(f"{self.driver.capabilities.name}: {e}")

    def _do_poll(self) -> PumpStatus | None:
        st = self.driver.poll()
        if st is None:
            self._count_silence()
            return None
        return st

    def _mark_online(self):
        if not self._link_up:
            self._link_up = True
            self.online.emit()
        self._silent = 0

    def _count_silence(self):
        self._silent += 1
        if self._silent >= self._OFFLINE_THRESHOLD and self._link_up:
            self._link_up = False
            self.offline.emit()

    def _mark_offline(self, msg: str, try_reopen: bool = False):
        was_up = self._link_up
        self._link_up = False
        if was_up:
            self.offline.emit()
            self.error.emit(msg)
        if try_reopen and hasattr(self.driver.transport, "reopen"):
            try:
                self.driver.transport.reopen()
            except TransportError:
                pass                                  # kolejna próba w _probe


def make_pump(port: str | None, model_key: str,
              poll_period: float = 0.5) -> PumpHandle:
    """Fabryka: (port | None, model) -> handle. Wołana z GUI-wątku.

    port=None + model demo -> symulator bez transportu.
    port=None + inny model -> RuntimeError (realna pompa wymaga portu).
    """
    from core.pump.registry import REGISTRY
    from core.transport.serial_port import SerialTransport

    driver_cls = REGISTRY[model_key]
    if port is None and driver_cls is not DemoDriver:
        raise RuntimeError(f"{model_key}: model wymaga portu COM")

    transport = None if port is None else SerialTransport(port)
    worker = PumpWorker(driver_cls(transport), poll_period=poll_period)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    return PumpHandle(worker, thread)


if __name__ == "__main__":
    import threading
    import time

    from PyQt6.QtCore import QCoreApplication, Qt

    from core.pump.demo import DemoDriver

    QCoreApplication([])   # sygnały Qt działają bez pętli zdarzeń —
                           # ale TYLKO dzięki DirectConnection niżej

    got_status: list = []
    versions: list = []
    errors_seen: list = []
    events: list = []

    class FaultyDemo(DemoDriver):
        """Demo z zaprogramowanymi awariami — do testu polityki błędów."""
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fault_mode = None    # "timeout"|"timeout3"|"transport"
            self._n = 0
        def poll(self):
            if self.fault_mode == "timeout":
                self.fault_mode = None
                return None                  # pojedyncza cisza
            if self.fault_mode == "timeout3":
                self._n += 1
                if self._n >= 3:
                    self.fault_mode = None
                    self._n = 0
                return None                  # cisza 3 razy z rzędu
            return super().poll()
        def execute(self, op, **kw):
            if self.fault_mode == "transport":
                self.fault_mode = None
                raise TransportError("testowy pad portu")
            return super().execute(op, **kw)

    def wait_for(cond, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.02)
        return False

    d = FaultyDemo()
    w = PumpWorker(d, poll_period=0.05)

    # KLUCZ: DirectConnection — lambda wykona się w wątku emitującym.
    # AutoConnection kolejkowałoby do nieistniejącej pętli zdarzeń
    # wątku głównego (przyczyna poprzedniego padu testu).
    direct = Qt.ConnectionType.DirectConnection
    w.status.connect(lambda st: got_status.append(st), direct)
    w.version.connect(lambda v: versions.append(v), direct)
    w.error.connect(lambda e: errors_seen.append(e), direct)
    w.online.connect(lambda: events.append("online"), direct)
    w.offline.connect(lambda: events.append("offline"), direct)

    t = threading.Thread(target=w.run, daemon=True)
    t.start()

    # 1. identyfikacja + pollowanie
    assert wait_for(lambda: len(got_status) >= 2)
    assert versions == ["DEMO-1.0"]
    assert events == ["online"]

    # 2. pojedyncza cisza — histerereza: NIE offline
    n1 = len(got_status)
    d.fault_mode = "timeout"
    assert wait_for(lambda: len(got_status) > n1)
    assert w._link_up and events == ["online"]

    # 3. trzy cisze z rzędu — offline (tu łapałaby martwa histerereza)
    d.fault_mode = "timeout3"
    assert wait_for(lambda: "offline" in events)
    assert w._link_up is False

    # 4. _probe odzyskuje link (~1 s) i emituje version ponownie
    assert wait_for(lambda: w._link_up)
    assert versions == ["DEMO-1.0", "DEMO-1.0"]
    assert events[-1] == "online"

    # 5. TransportError w komendzie — link down + kanał error
    d.fault_mode = "transport"
    w._queue.put((Op.START, {}))
    assert wait_for(lambda: errors_seen)
    assert w._link_up is False and events[-1] == "offline"

    # 6. odzyskanie po padzie portu
    w._queue.put((Op.START, {}))
    assert wait_for(lambda: w._link_up)

    # 7. czyste zamknięcie
    w._queue.put((None, {}))
    t.join(timeout=3)
    assert not t.is_alive()
    print("worker: OK")