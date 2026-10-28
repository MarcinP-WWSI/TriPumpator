"""Session: spina pompy (handles), okna pomp i panel główny.

Cykl życia:
    Session()           — konstrukcja (pump_factory: seam testowy)
    start(config)       — pompy + okna + MainPanel; wątki robocze
                          startują PO zasubskrybowaniu wszystkich slotów
    remove_pump(pid)    — detach okna, zamknięcie wątku, rebuild panelu
    shutdown_all()      — wszystko w dół + session_ended.emit()

Routing międzywątkowy idzie WYŁĄCZNIE przez _PumpRelay (QObject w
GUI-wątku): bound methods mają afiliację wątku, więc Qt automatycznie
kolejkuje dostarczenie z wątku roboczego. Lambdy na ścieżce between-
thread są zakazane (brak receivera -> nieokreślone dostarczenie).
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QObject, pyqtSignal

from core.pump.base import Op, PumpStatus
from core.pump.worker import PumpHandle, make_pump
from ui.device_window import PumpWindow
from ui.main_panel import MainPanel
from ui.programator_window import ProgramatorWindow


class _PumpRelay(QObject):
    """Przekaźnik jednej pompy: wątek roboczy -> GUI."""

    def __init__(self, pid: int, session: "Session"):
        super().__init__()          # bez parenta: żywotność przez _relays
        self._pid = pid
        self._session = session

    def on_status(self, st: PumpStatus) -> None:
        self._session._on_status(self._pid, st)

    def on_online(self) -> None:
        self._session._set_link(self._pid, True)

    def on_offline(self) -> None:
        self._session._set_link(self._pid, False)

    def on_version(self, v: str) -> None:
        self._session._on_version(self._pid, v)


class Session(QObject):
    session_ended = pyqtSignal()

    def __init__(self, pump_factory: Callable = make_pump, parent=None):
        super().__init__(parent)
        self._factory = pump_factory
        self._pumps: dict[int, PumpHandle] = {}
        self._relays: dict[int, _PumpRelay] = {}
        self._windows: dict[int, PumpWindow] = {}
        self._link_state: dict[int, bool] = {}
        self._last_version: dict[int, str] = {}
        self.main_panel: MainPanel | None = None
        self._programator: ProgramatorWindow | None = None
        self._shutting_down = False

    # ------------------------------------------------------------- start

    def start(self, config: list) -> None:
        """config z selectora: [(port|None, model_key, setup|None), ...].

        Wpisy 2-elementowe (bez setupu) też są akceptowane.
        """
        if self._pumps:
            raise RuntimeError("sesja już wystartowana")

        for pid, item in enumerate(config, start=1):
            port, model_key = item[0], item[1]
            setup = item[2] if len(item) > 2 else None
            try:
                handle = self._factory(port, model_key)
            except Exception:
                self.shutdown_all()  # posprzątaj pompy już utworzone
                raise

            # subskrypcja PRZED startem wątku: _identify emituje
            # version/online jednorazowo, natychmiast po starcie
            relay = _PumpRelay(pid, self)
            handle.status.connect(relay.on_status)
            handle.online.connect(relay.on_online)
            handle.offline.connect(relay.on_offline)
            handle.version.connect(relay.on_version)
            self._relays[pid] = relay

            self._pumps[pid] = handle

            win = PumpWindow(pid, handle)   # _attach podpina okno
            win.closed.connect(lambda pid=pid: self.remove_pump(pid))
            self._windows[pid] = win
            win.show()

            handle.start()                  # wątek w górę jako OSTATNI

            # wartości startowe z selectora: średnica PIERW (jej zmiana
            # kasuje rate/limity na pompie), potem siła; rate/target
            # ustawia użytkownik w oknie pompy po starcie
            if setup:
                if "diameter" in setup:
                    handle.submit(Op.SET_DIAMETER, mm=setup["diameter"])
                if "force" in setup:
                    handle.submit(Op.SET_FORCE, percent=setup["force"])

        self.main_panel = MainPanel()
        self.main_panel.set_pumps(
            [(pid, h.capabilities.name)
             for pid, h in sorted(self._pumps.items())])
        # polisa: gdyby link zdążył się dostarczyć przed budową tabeli
        for pid, up in self._link_state.items():
            if pid in self._pumps:
                self.main_panel.set_link(pid, up)

        self.main_panel.start_all.connect(self.start_all)
        self.main_panel.stop_all.connect(self.stop_all)
        self.main_panel.end_session.connect(self.shutdown_all)
        self.main_panel.open_programator.connect(self.open_programator)
        self.main_panel.show()

    # --------------------------------------------------- operacje globalne

    def start_all(self) -> None:
        for h in self._pumps.values():
            h.submit(Op.START)

    def stop_all(self) -> None:
        for h in self._pumps.values():
            h.submit(Op.STOP)

    # -------------------------------------------------------- programator

    def open_programator(self) -> None:
        """Pokaż Programator (jedna instancja na sesję, nieomalne)."""
        if self._programator is None:
            self._programator = ProgramatorWindow(self._pumps)
        self._programator.show()
        self._programator.raise_()
        self._programator.activateWindow()

    # --------------------------------------------------- routing do panelu

    def _on_status(self, pid: int, st: PumpStatus) -> None:
        if self.main_panel is not None and pid in self._pumps:
            self.main_panel.update_pump(pid, st)

    def _set_link(self, pid: int, online: bool) -> None:
        self._link_state[pid] = online
        if self.main_panel is not None and pid in self._pumps:
            self.main_panel.set_link(pid, online)

    def _on_version(self, pid: int, v: str) -> None:
        self._last_version[pid] = v

    # -------------------------------------------------------- usuwanie

    def remove_pump(self, pid: int) -> None:
        if pid not in self._pumps:
            return                     # już usunięta (guard rekurencji)
        win = self._windows.pop(pid)
        win.detach()
        try:
            win.closed.disconnect()    # Session zamyka okno sama: bez echa
        except TypeError:
            pass
        win.close()                    # no-op, gdy zamknięcie zainicjował
                                       # użytkownik (okno i tak znika)
        handle = self._pumps.pop(pid)
        self._relays.pop(pid, None)
        handle.close()                 # sentinel + quit + wait(2000)
        self._link_state.pop(pid, None)
        self._last_version.pop(pid, None)
        self._rebuild_panel()

    def _rebuild_panel(self) -> None:
        if self.main_panel is None:
            return
        self.main_panel.set_pumps(
            [(pid, h.capabilities.name)
             for pid, h in sorted(self._pumps.items())])
        for pid, up in self._link_state.items():
            if pid in self._pumps:
                self.main_panel.set_link(pid, up)

    # -------------------------------------------------------- koniec sesji

    def shutdown_all(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        for pid in list(self._pumps):
            self.remove_pump(pid)
        if self._programator is not None:
            prog, self._programator = self._programator, None
            prog.close()  # closeEvent stopuje runnera, jeśli biegnie
        if self.main_panel is not None:
            panel, self.main_panel = self.main_panel, None
            panel.close()              # end_session -> shutdown_all: no-op
        self.session_ended.emit()


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time

    from PyQt6.QtCore import QObject as _QObj, pyqtSignal as _sig
    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    from core.pump.base import PumpCapabilities

    # ================= A: routing na fake handles (bez wątków) ==========
    class FakeHandle(_QObj):
        status = _sig(object)
        version = _sig(str)
        error = _sig(str)
        online = _sig()
        offline = _sig()

        capabilities = PumpCapabilities(name="Fake")

        def __init__(self):
            super().__init__()
            self.ops: list = []
            self.closed = False

        def submit(self, op, **kw):
            self.ops.append((op, kw))
            return True

        def start(self):               # w prawdziwych: start wątku
            pass

        def validate_rate(self, value, unit):
            return None

        def close(self):
            self.closed = True

    fakes = [FakeHandle(), FakeHandle()]
    made: list = []
    session = Session(pump_factory=lambda port, key: made.append(
        (port, key)) or fakes[len(made) - 1])

    AL, DEMO = "WPI AL-1000", "Pompa demo (symulator)"
    session.start([("COM3", AL), (None, DEMO)])
    assert made == [("COM3", AL), (None, DEMO)]

    t = session.main_panel.table
    assert t.rowCount() == 2 and t.item(0, 1).text() == "Fake"
    assert t.item(0, 2).text() == "OFFLINE"

    fakes[0].status.emit(PumpStatus(running=True, rate=100.0,
                                    rate_unit="mL/h"))
    assert t.item(0, 3).text() == "pracuje"
    fakes[1].online.emit()
    assert t.item(1, 2).text() == "ONLINE"
    assert session.main_panel.online_label.text() == "Online: 1/2"

    fakes[0].version.emit("V-TEST")
    assert session._windows[1].version_label.text() == "V-TEST"
    assert session._last_version[1] == "V-TEST"

    session.main_panel.start_all_btn.click()
    assert fakes[0].ops[-1] == (Op.START, {}) \
        and fakes[1].ops[-1] == (Op.START, {})
    session.main_panel.stop_all_btn.click()
    assert fakes[1].ops[-1] == (Op.STOP, {})

    session._windows[1].close()
    assert fakes[0].closed and 1 not in session._pumps
    assert t.rowCount() == 1 and t.item(0, 0).text() == "2"
    fakes[0].status.emit(PumpStatus(running=True))    # po usunięciu: no-op
    assert t.item(0, 3).text() in ("—", "stop")

    got_end: list = []
    session.session_ended.connect(lambda: got_end.append(True))
    session.shutdown_all()
    assert fakes[1].closed and got_end == [True] and session._pumps == {}
    session.shutdown_all()            # idempotentne
    assert got_end == [True]

    # A2. programator: instancja + shutdown zamyka (świeża sescja A3)
    fakes_b = [FakeHandle(), FakeHandle()]
    idx = [0]
    session_p = Session(pump_factory=lambda port, key:
    idx.append(1) or fakes_b[len(idx) - 2])
    session_p.start([(None, DEMO), (None, DEMO)])
    session_p.main_panel.open_programator.emit()
    assert session_p._programator is not None
    pw1 = session_p._programator
    session_p.main_panel.open_programator.emit()
    assert session_p._programator is pw1
    session_p.shutdown_all()
    assert session_p._programator is None  # zamknięty w shutdown


    # ================= B: integracja z prawdziwymi wątkami (demo) =======
    def wait(cond, timeout: float = 6.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()       # dostarcza queued signals z QThread
            if cond():
                return True
            time.sleep(0.02)
        return False

    session2 = Session()              # prawdziwy make_pump
    session2.start([(None, DEMO), (None, DEMO)])
    t2 = session2.main_panel.table

    # wątki wystartowane PO subskrypcji: version/online zawsze dostarczone
    assert wait(lambda: t2.item(0, 2).text() == "ONLINE"
                and t2.item(1, 2).text() == "ONLINE"), "brak ONLINE"
    assert wait(lambda: session2._windows[1].version_label.text()
                == "DEMO-1.0"), "brak wersji"
    assert wait(lambda: session2._windows[2].version_label.text()
                == "DEMO-1.0"), "brak wersji (pompa 2)"
    assert wait(lambda: t2.item(0, 3).text() != "—"), "brak statusu"
    assert session2.main_panel.online_label.text() == "Online: 2/2"

    session2.main_panel.start_all_btn.click()
    assert wait(lambda: t2.item(0, 3).text() == "pracuje"
                and t2.item(1, 3).text() == "pracuje"), "brak pracuje"
    assert wait(lambda: t2.item(0, 6).text() != "—"), "brak dispensed"

    session2.main_panel.stop_all_btn.click()
    assert wait(lambda: t2.item(0, 3).text() == "stop"
                and t2.item(1, 3).text() == "stop"), "brak stop"

    session2.shutdown_all()
    assert session2._pumps == {} and session2.main_panel is None

    print("session: OK")