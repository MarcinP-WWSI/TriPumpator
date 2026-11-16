"""Runner procedur Programatora: wykonuje kroki bez własnego wątku
wyjąwszy start pomiaru IviumSoft (blokuje ~3 s — osobny wątek).

Projekt: runner żyje w GUI-wątku; jego praca to PumpHandle.submit
(thread-safe) i QTimer. Kroki IviumSoft:
  MEAS_START   -> ivium.start(path) w wątku jednorazowym (GUI nie marznie)
  MEAS_RUNNING -> bramka oceniana tyknieniem 0.5 s (ivium nie wypycha
  MEAS_FINISHED   statusów jak pompy — sami pytamy)

Status kroku (sygnał step_changed) DOTYKANY co 250 ms:
  WAIT          -> "... — zostało X s"      (odliczanie)
  bramka pompy   -> "... — timeout: zostało X s"
  bramka MEAS_*  -> "... — punkty: N | timeout: zostało X s"

Pętle: wskaźnik + stos liczników. Stany: idle -> running ->
finished | stopped. Ivium importowany LENIWIE (SIM czytany przy
imporcie — env musi stać przed pierwszym użyciem w procesie).
"""

from __future__ import annotations

import threading
import time

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.pump.base import Op, PumpStatus, to_ml
from core.pump.worker import PumpHandle
from core.proc.steps import (
    CommandStep, CondCmd, ConditionStep, LoopEndStep, LoopStartStep,
    ProcCmd, describe, validate,
)

MAX_TOTAL_STEPS = 100_000    # bezpiecznik na niekończące się pętle
MEAS_TICK_MS = 500           # tyk oceny bramek MEAS
STATUS_TICK_MS = 250         # tyk odliczania/punktów w statusie kroku

MEAS_CONDS = (CondCmd.MEAS_RUNNING, CondCmd.MEAS_FINISHED)

_CMD_TO_OP = {
    ProcCmd.START: (Op.START, {}),
    ProcCmd.STOP: (Op.STOP, {}),
    ProcCmd.PURGE: (Op.PURGE, {}),
    ProcCmd.SET_RATE: (Op.SET_RATE, ("value", "unit")),
    ProcCmd.SET_DIRECTION: (Op.SET_DIRECTION, ("direction",)),
    ProcCmd.SET_TARGET_VOLUME: (Op.SET_TARGET_VOLUME, ("volume", "unit")),
    ProcCmd.SET_TARGET_TIME: (Op.SET_TARGET_TIME, ("seconds",)),
    ProcCmd.SET_DIAMETER: (Op.SET_DIAMETER, ("mm",)),
    ProcCmd.SET_FORCE: (Op.SET_FORCE, ("percent",)),
    ProcCmd.CLEAR_DISPENSED: (Op.CLEAR_DISPENSED, {}),
}


def _ivium():
    """Import ivium_measurement (leniwy — SIM czytany przy imporcie)."""
    from Automation import ivium_measurement
    return ivium_measurement


class ProcedureRunner(QObject):
    step_changed = pyqtSignal(int, int, str)   # (index, total, opis kroku)
    finished = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)
    step_changed = pyqtSignal(int, int, str)
    finished = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)
    meas_started = pyqtSignal(int, int, str)  # z wątku startu pomiaru
    meas_failed = pyqtSignal(str)  # z wątku startu pomiaru

    def __init__(self, handles: dict[int, PumpHandle], parent=None):
        super().__init__(parent)
        self._handles = dict(handles)       # pid -> handle (snapshot)
        self._steps: list = []
        self._pos = -1                       # indeks bieżącego kroku
        self._loop_stack: list[list] = []    # [start_idx, pozostalo]
        self._running = False
        # WAIT
        self._timer: QTimer | None = None          # single-shot: koniec WAIT
        self._wait_deadline: float | None = None    # monotonic
        # bramki
        self._cond: tuple[ConditionStep, float] | None = None
        self._cond_seq0 = 0
        self._cond_timer: QTimer | None = None     # single-shot: timeout bramki
        self._tick_timer: QTimer | None = None     # tyk oceny bramek MEAS
        self.meas_started.connect(self._on_meas_started)
        self.meas_failed.connect(self._on_meas_failed)
        # status kroku (odliczanie / punkty)
        self._status_timer: QTimer | None = None
        # słuchamy statusów WSZYSTKICH pomp (ocena bramek na żywo)
        self._relays: list = []
        for pid, h in self._handles.items():
            relay = _CondRelay(pid, self)
            h.status.connect(relay.on_status)
            self._relays.append(relay)

    # ------------------------------------------------------------- API

    def is_running(self) -> bool:
        return self._running

    def is_waiting(self) -> bool:
        """True gdy procedura wstrzymana na WAIT (timer aktywny)."""
        return self._timer is not None

    def run(self, steps: list) -> bool:
        """Zwaliduj i wykonaj procedurę. False = odrzucono."""
        if self._running:
            self.error.emit("runner już działa")
            return False
        caps = {pid: h.capabilities for pid, h in self._handles.items()}
        errs = validate(steps, caps)
        if errs:
            self.error.emit(";\n".join(errs))
            return False
        self._steps = list(steps)
        self._pos = -1
        self._loop_stack = []
        self._running = True
        self._advance()
        return True

    def stop(self) -> None:
        """Przerwij bieg: ucieka WAIT/bramkę/status-ticker, nic nie
        wysyła do pomp ani do IviumSoft."""
        if not self._running:
            return
        self._running = False
        self._leave_condition()
        self._stop_wait()
        self._stop_status_ticker()
        self.stopped.emit()

    # ------------------------------------------------------------ silnik

    def _advance(self) -> None:
        """Wykonuj kroki aż do WAIT/bramki/konca."""
        if not self._running:
            return
        guard = 0
        while self._running:
            guard += 1
            if guard > MAX_TOTAL_STEPS:
                self._fail(f"przekroczono limit wykonanych kroków "
                           f"({MAX_TOTAL_STEPS}) — przerywam")
                return
            self._pos += 1
            if self._pos >= len(self._steps):
                self._running = False
                self._stop_status_ticker()
                self.finished.emit()
                return
            step = self._steps[self._pos]
            if isinstance(step, LoopStartStep):
                self._loop_stack.append([self._pos, step.count])
                self._emit_progress(step)
                continue
            if isinstance(step, LoopEndStep):
                self._emit_progress(step)
                if not self._loop_stack:
                    self._fail("pętla bez początku")   # fail-safe
                    return
                top = self._loop_stack[-1]
                top[1] -= 1
                if top[1] > 0:
                    self._pos = top[0]        # skok za LoopStart
                else:
                    self._loop_stack.pop()
                continue
            if isinstance(step, ConditionStep):
                self._emit_progress(step)
                self._enter_condition(step)
                return                          # _advance z timera/statusu
            # CommandStep
            self._emit_progress(step)
            if step.cmd is ProcCmd.WAIT:
                self._start_wait(float(step.args["seconds"]))
                return                          # _advance z timera
            self._dispatch(step)

    def _dispatch(self, step: CommandStep) -> None:
        if step.cmd is ProcCmd.MEAS_START:
            self._start_measurement(step)
            return
        op, arg_names = _CMD_TO_OP[step.cmd]
        kw = {name: step.args[name] for name in arg_names}
        pids = list(self._handles) if step.pumps is None \
            else [p for p in step.pumps if p in self._handles]
        for pid in pids:
            self._handles[pid].submit(op, **kw)

    def _fail(self, msg: str) -> None:
        """Koniec z błędem (z dowolnego miejsca, także z wątku)."""
        self._running = False
        self._leave_condition()
        self._stop_wait()
        self._stop_status_ticker()
        self.error.emit(msg)
        self.stopped.emit()

    # ----------------------------------------------------------- WAIT

    def _start_wait(self, seconds: float) -> None:
        self._wait_deadline = time.monotonic() + seconds
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_wait_done)
        self._timer.start(int(seconds * 1000))
        self._start_status_ticker()

    def _stop_wait(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        self._wait_deadline = None

    def _on_wait_done(self) -> None:
        self._stop_wait()
        self._stop_status_ticker()
        self._advance()

    # -------------------------------------------------- pomiar IviumSoft

    def _start_measurement(self, step: CommandStep) -> None:
        """MEAS_START: ivium.start() blokuje do ~3 s — wątek jednorazowy.

        Wątek NIE woła metod runnera wprost (race na polach — patrz
        meas_started/meas_failed): emituje sygnały, sloty w GUI-wątku
        robią _fail/step_changed sekwencyjnie z _advance.
        """
        path = step.args.get("file_path", "")

        def worker():
            try:
                iv = _ivium()
            except Exception as e:
                self.meas_failed.emit(
                    f"brak modułu pomiaru (pyvium/IviumSoft): {e}")
                return
            try:
                iv.start(path)
                self.meas_started.emit(
                    self._pos, len(self._steps),
                    f"{describe(step)} — wystartował")
            except Exception as e:
                self.meas_failed.emit(f"pomiar nie wystartował: {e}")

        threading.Thread(target=worker, daemon=True,
                         name="ivium-start").start()

    def _on_meas_started(self, index: int, total: int, text: str) -> None:
        """Slot w GUI-wątku (sygnał queued z wątku startu)."""
        self.step_changed.emit(index, total, text)

    def _on_meas_failed(self, msg: str) -> None:
        """Slot w GUI-wątku — _fail sekwencyjnie z _advance, zero race."""
        self._fail(msg)

    # ------------------------------------------------------- bramki

    def _enter_condition(self, step: ConditionStep) -> None:
        """Otwórz bramkę: timeout zawsze; MEAS_* dodatkowo tyk oceny."""

        self._cond = (step, time.monotonic() + step.timeout_s)
        # PRÓG: odpowiedzi na komendy dispatchowane w TEJ SAMEJ serii
        # _advance (CLEAR/SET_TARGET/START przed bramką) są emitowane
        # przez worker PO zbrojeniu — stary próg (sam licznik) ich nie
        # łapał (wykryte na procedures3). seq jest rezerwowany przy
        # zdejmowaniu z kolejki, więc licznik obejmuje op w locie i
        # próg = licznik + qsize jest DOKŁADNIE "za serią".
        self._cond_seq0 = 0
        if step.pump is not None:
            h = self._handles.get(step.pump)
            if h is not None and hasattr(h, "status_seq") \
                    and hasattr(h, "pending_status"):
                self._cond_seq0 = h.status_seq() + h.pending_status()
        # timeout bramki (ZAWSZE — dla pomp i dla MEAS)
        self._cond_timer = QTimer(self)
        self._cond_timer.setSingleShot(True)
        self._cond_timer.timeout.connect(self._on_cond_timeout)
        self._cond_timer.start(int(step.timeout_s * 1000))

        # tyk oceny — tylko MEAS (pompy oceniają się na statusach)
        if self._tick_timer is not None:
            self._tick_timer.stop()
            self._tick_timer.deleteLater()
            self._tick_timer = None
        if step.cond in MEAS_CONDS:
            self._tick_timer = QTimer(self)
            self._tick_timer.timeout.connect(self._tick)
            self._tick_timer.start(MEAS_TICK_MS)

        self._start_status_ticker()

    def _leave_condition(self) -> None:
        """Zamknij bramkę — sprzątnij wszystkie trzy timery."""
        self._cond = None
        for t in (self._cond_timer, self._tick_timer):
            if t is not None:
                t.stop()
                t.deleteLater()
        self._cond_timer = None
        self._tick_timer = None
        self._stop_status_ticker()

    def _tick(self) -> None:
        """Tyk oceny bramki MEAS (co 0.5 s)."""
        if self._cond is None or not self._running:
            return
        step, _ = self._cond
        try:
            iv = _ivium()
            if step.cond is CondCmd.MEAS_RUNNING:
                met = iv.is_running()
            elif step.cond is CondCmd.MEAS_FINISHED:
                met = iv.is_finished()
            else:
                return
        except Exception:
            return          # chwilowy brak łączności — tyknie za 0.5 s
        if met:
            self._leave_condition()
            self._advance()

    def _on_pump_status(self, pid: int, st: PumpStatus) -> None:
        if self._cond is None or not self._running:
            return
        step, _ = self._cond
        if step.pump != pid:
            return
        if st.seq is not None and st.seq <= self._cond_seq0:
            return

        if self._cond_met(step, st):
            self._leave_condition()
            self._advance()

    @staticmethod
    def _cond_met(step: ConditionStep, st: PumpStatus) -> bool:
        match step.cond:
            case CondCmd.STOPPED:
                return st.running is False
            case CondCmd.RUNNING:
                return st.running is True
            case CondCmd.ALARM:
                return st.alarm is not None
            case CondCmd.DISPENSED_GE:
                if st.dispensed is None:
                    return False
                target = to_ml(float(step.args["value"]),
                               step.args.get("unit"))
                have = to_ml(st.dispensed, st.dispensed_unit)
                return have >= target
        return False

    def _on_cond_timeout(self) -> None:
        step, _ = self._cond if self._cond else (None, 0.0)
        self._leave_condition()
        if not self._running or step is None:
            return
        if step.on_timeout == "continue":
            self._advance()  # polityka: jedź dalej
        else:
            self._fail(f"warunek '{describe(step)}' niespełniony po "
                       f"{step.timeout_s:g} s")

    # --------------------------------------------- status kroku (ticker)

    def _start_status_ticker(self) -> None:
        self._stop_status_ticker()
        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._on_status_tick)
        self._status_timer.start(STATUS_TICK_MS)

    def _stop_status_ticker(self) -> None:
        if self._status_timer is not None:
            self._status_timer.stop()
            self._status_timer.deleteLater()
            self._status_timer = None

    def _on_status_tick(self) -> None:
        """Odśwież status BIEŻĄCEGO kroku (odliczanie / punkty)."""
        if not self._running or self._status_timer is None:
            return
        if not 0 <= self._pos < len(self._steps):
            return
        step = self._steps[self._pos]
        text = self._status_text(step)
        if text is not None:
            self.step_changed.emit(self._pos, len(self._steps), text)

    def _status_text(self, step) -> str | None:
        base = describe(step)
        if isinstance(step, CommandStep) and step.cmd is ProcCmd.WAIT:
            left = self._left_of(self._wait_deadline)
            return f"{base} — zostało {left:.0f} s"
        if isinstance(step, ConditionStep):
            _, deadline = self._cond if self._cond else (None, None)
            left = self._left_of(deadline)
            if step.cond in MEAS_CONDS:
                pts = None
                try:
                    pts = _ivium().get_point_count()
                except Exception:
                    pass
                pts_s = f"punkty: {pts}" if pts is not None else "punkty: ?"
                return f"{base} — {pts_s} | timeout: zostało {left:.0f} s"
            return f"{base} — timeout: zostało {left:.0f} s"
        return None

    @staticmethod
    def _left_of(deadline: float | None) -> float:
        if deadline is None:
            return 0.0
        return max(0.0, deadline - time.monotonic())

    def _emit_progress(self, step) -> None:
        self.step_changed.emit(self._pos, len(self._steps), describe(step))


class _CondRelay(QObject):
    """Przekaźnik statusu jednej pompy do runnera (GUI-wątek)."""

    def __init__(self, pid: int, runner: "ProcedureRunner"):
        super().__init__()
        self._pid = pid
        self._runner = runner

    def on_status(self, st: PumpStatus) -> None:
        self._runner._on_pump_status(self._pid, st)


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    from core.pump.base import PumpCapabilities
    from core.pump.demo import DemoDriver

    class FakeHandle:
        """Handle zbierający submity; sygnały jak PumpHandle."""
        from PyQt6.QtCore import QObject, pyqtSignal

        class _S(QObject):
            status = pyqtSignal(object)
            version = pyqtSignal(str)
            error = pyqtSignal(str)
            online = pyqtSignal()
            offline = pyqtSignal()

        def __init__(self, caps):
            self._s = FakeHandle._S()
            self.capabilities = caps
            self.ops: list = []

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return getattr(self._s, name)

        def submit(self, op, **kw):
            self.ops.append((op, kw))
            return True

        def validate_rate(self, v, u):
            return None

    def wait_for(cond, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()      # dostarcza timeouty QTimerów
            if cond():
                return True
            time.sleep(0.02)
        return False

    demo = DemoDriver.capabilities
    h1, h2 = FakeHandle(demo), FakeHandle(demo)
    handles = {1: h1, 2: h2}

    seen: list = []
    finished: list = []
    errors: list = []
    stopped: list = []

    # 1. prosta procedura: START -> WAIT 1.0 s -> STOP
    #    (WAIT > 250 ms, by ticker odliczania zdążył strzelić)
    r = ProcedureRunner(handles)
    r.step_changed.connect(lambda i, t, s: seen.append((i, s)))
    r.finished.connect(lambda: finished.append(True))
    r.error.connect(errors.append)
    r.stopped.connect(lambda: stopped.append(True))

    from core.proc.steps import CommandStep, LoopEndStep, LoopStartStep, \
        ProcCmd
    proc = [
        CommandStep(ProcCmd.START, None),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 1.0}),
        CommandStep(ProcCmd.STOP, (1,)),
    ]
    assert r.run(proc)
    assert h1.ops[-1] == (Op.START, {}) and h2.ops[-1] == (Op.START, {})
    assert r.is_running() and r.is_waiting()
    assert wait_for(lambda: finished == [True])
    assert h1.ops[-1] == (Op.STOP, {})
    assert h2.ops[-1] == (Op.START, {})
    assert seen[0][1] == "Start pomp [wszystkie]"

    # 1a. kolejność PIERWSZYCH wykonań kroków (full historia ma też tyki)
    first_seen = []
    for i, s in seen:
        if i not in first_seen:
            first_seen.append(i)
    assert first_seen == [0, 1, 2], f"kolejność: {seen}"

    # 1b. odliczanie w statusie WAIT ("zostało X s")
    waits = [s for i, s in seen if "zostało" in s]
    assert waits, f"brak odliczania w {seen}"

    # 2. pętla ×2
    h1.ops.clear(); h2.ops.clear(); seen.clear(); finished.clear()
    proc2 = [
        CommandStep(ProcCmd.CLEAR_DISPENSED, None),
        LoopStartStep(2),
        CommandStep(ProcCmd.START, (1,)),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 0.1}),
        CommandStep(ProcCmd.STOP, (1,)),
        LoopEndStep(),
        CommandStep(ProcCmd.PURGE, (1,)),
    ]
    assert r.run(proc2)
    assert wait_for(lambda: finished == [True])
    starts = [op for op, _ in h1.ops if op is Op.START]
    assert len(starts) == 2
    purges = [op for op, _ in h1.ops if op is Op.PURGE]
    assert len(purges) == 1
    seq = [op for op, _ in h1.ops if op is not Op.STOP]
    assert seq == [Op.CLEAR_DISPENSED, Op.START, Op.START, Op.PURGE]

    # 3. zagnieżdżone pętle ×2/×2
    h1.ops.clear(); seen.clear(); finished.clear()
    proc3 = [
        LoopStartStep(2),
        CommandStep(ProcCmd.START, (1,)),
        LoopStartStep(2),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 0.1}),
        LoopEndStep(),
        CommandStep(ProcCmd.STOP, (1,)),
        LoopEndStep(),
    ]
    assert r.run(proc3)
    assert wait_for(lambda: finished == [True])
    starts = [op for op, _ in h1.ops if op is Op.START]
    stops = [op for op, _ in h1.ops if op is Op.STOP]
    waits = [s for i, s in seen if "Czekaj" in s]
    assert len(starts) == 2 and len(stops) == 2
    assert len(waits) == 4

    # 4. stop w trakcie WAIT
    seen.clear(); finished.clear(); stopped.clear()
    r2 = ProcedureRunner(handles)
    r2.finished.connect(lambda: finished.append(True))
    r2.stopped.connect(lambda: stopped.append(True))
    r2.error.connect(errors.append)
    assert r2.run([
        CommandStep(ProcCmd.START, None),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 5.0}),
        CommandStep(ProcCmd.STOP, None),
    ])
    assert r2.is_running() and r2.is_waiting()
    r2.stop()
    assert stopped == [True] and finished == []
    assert not r2.is_running() and not r2.is_waiting()

    # 5. run po stopie; błąd walidacji; pusta -> finish
    assert r2.run([CommandStep(ProcCmd.START, (1,))])
    assert wait_for(lambda: finished == [True])
    h1.ops.clear()
    errs_before = len(errors)
    assert not r2.run(
        [CommandStep(ProcCmd.SET_RATE, (1,), {"value": 1})])  # brak unit
    assert len(errors) > errs_before
    finished.clear()
    assert r2.run([])
    assert wait_for(lambda: finished == [True])

    # 6. WAIT na granicy MIN_WAIT_S (krótki — bez asercji odliczania)
    h1.ops.clear()
    finished.clear()
    assert r2.run([
        CommandStep(ProcCmd.START, (1,)),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 0.1}),
        CommandStep(ProcCmd.STOP, (1,)),
    ])
    assert wait_for(lambda: finished == [True])

    # 7. bramka STOPPED + odliczanie timeoutu w statusie
    h1.ops.clear(); h2.ops.clear()
    seen.clear(); finished.clear()
    from core.proc.steps import ConditionStep, CondCmd

    r3 = ProcedureRunner(handles)
    r3.step_changed.connect(lambda i, t, s: seen.append((i, s)))
    r3.finished.connect(lambda: finished.append(True))
    r3.error.connect(errors.append)
    proc4 = [
        CommandStep(ProcCmd.START, (1,)),
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=5.0),
        CommandStep(ProcCmd.START, (2,)),
    ]
    assert r3.run(proc4)
    assert r3.is_running() and not r3.is_waiting()
    # bramka wisi: najpierw czekamy na tyki odliczania (patrzymy w seen),
    # dopiero potem zwalniamy bramkę — inaczej ticker nie zdążyłby
    h1._s.status.emit(PumpStatus(running=True))
    assert wait_for(lambda: any("timeout: zostało" in s for _, s in seen),
                    timeout=2.0), "brak ticków odliczania timeoutu"
    h1._s.status.emit(PumpStatus(running=False))
    assert wait_for(lambda: finished == [True])
    assert h2.ops[-1] == (Op.START, {})

    # 8. timeout error
    errors.clear(); finished.clear(); stopped.clear()
    r4 = ProcedureRunner(handles)
    r4.finished.connect(lambda: finished.append(True))
    r4.stopped.connect(lambda: stopped.append(True))
    r4.error.connect(errors.append)
    assert r4.run([
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=0.2),
    ])
    assert wait_for(lambda: stopped == [True])
    assert any("niespełniony" in e for e in errors)
    assert finished == []

    # 9. timeout continue
    finished.clear(); stopped.clear(); errors.clear()
    r5 = ProcedureRunner(handles)
    r5.finished.connect(lambda: finished.append(True))
    r5.error.connect(errors.append)
    assert r5.run([
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=0.2,
                      on_timeout="continue"),
        CommandStep(ProcCmd.STOP, (2,)),
    ])
    assert wait_for(lambda: finished == [True])
    assert h2.ops[-1] == (Op.STOP, {})

    # 10. DISPENSED_GE
    h2.ops.clear(); finished.clear()
    r6 = ProcedureRunner(handles)
    r6.finished.connect(lambda: finished.append(True))
    assert r6.run([
        CommandStep(ProcCmd.START, (2,)),
        ConditionStep(CondCmd.DISPENSED_GE, 2,
                      {"value": 1.0, "unit": "mL"}, timeout_s=5.0),
    ])
    h2._s.status.emit(PumpStatus(running=True, dispensed=0.5,
                                 dispensed_unit="mL"))
    assert r6.is_running()
    h2._s.status.emit(PumpStatus(running=True, dispensed=1500.0,
                                 dispensed_unit="µL"))
    assert wait_for(lambda: finished == [True])

    # 11. bramka obcej pompy nie zwalnia
    h1.ops.clear(); finished.clear()
    r7 = ProcedureRunner(handles)
    r7.finished.connect(lambda: finished.append(True))
    assert r7.run([
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=30.0),
    ])
    h2._s.status.emit(PumpStatus(running=False))     # pompa 2 — nie ta
    assert r7.is_running()
    r7.stop()
    assert not r7.is_running()

    # 12. Ivium: MEAS_START (wątek) + bramki MEAS_* + punkty w statusie
    #     SIM i timingi PRZED importem (FakePyvium czyta env w __init__)
    os.environ["SIM"] = "1"
    os.environ["SIM_STARTUP"] = "0.6"     # bramka RUNNING wisi > 1 tyk
    os.environ["SIM_PREPHASE"] = "0.4"
    os.environ["SIM_DURATION"] = "1.2"
    os.environ["SIM_POINTS"] = "40"
    os.environ["SIM_FAIL"] = ""
    from Automation import ivium_measurement as iv
    import tempfile
    from pathlib import Path as _P
    imf = _P(tempfile.gettempdir()) / "runner_test.imf"
    imf.write_text("fake imf", "utf-8")
    iv.sim.reset()
    iv.shutdown(abort=False)              # czysty stan startu testu

    seen.clear(); finished.clear(); stopped.clear(); errors.clear()
    r8 = ProcedureRunner(handles)
    r8.step_changed.connect(lambda i, t, s: seen.append((i, s)))
    r8.finished.connect(lambda: finished.append(True))
    r8.stopped.connect(lambda: stopped.append(True))
    r8.error.connect(errors.append)
    proc_m = [
        CommandStep(ProcCmd.MEAS_START, None, {"file_path": str(imf)}),
        ConditionStep(CondCmd.MEAS_RUNNING, None, timeout_s=5.0),
        ConditionStep(CondCmd.MEAS_FINISHED, None, timeout_s=10.0),
        CommandStep(ProcCmd.START, (1,)),
    ]
    assert r8.run(proc_m)
    assert wait_for(lambda: finished == [True], timeout=15.0)
    assert ("start_method", str(imf).replace("\\", "/")) in iv.sim.calls
    assert h1.ops[-1] == (Op.START, {})
    pts_states = [s for i, s in seen if "punkty:" in s]
    assert pts_states, f"brak 'punkty:' w statusach: {seen}"
    assert any("wystartował" in s for i, s in seen)
    assert not r8.is_running()

    # 12b. start pomiaru pada -> _fail z wątku; bramka powstrzymuje finish
    iv.sim.fail_mode = "start"
    iv.sim.reset()
    seen.clear(); finished.clear()
    stopped.clear(); errors.clear()
    r9 = ProcedureRunner(handles)
    r9.finished.connect(lambda: finished.append(True))
    r9.stopped.connect(lambda: stopped.append(True))
    r9.error.connect(errors.append)
    assert r9.run([
        CommandStep(ProcCmd.MEAS_START, None, {"file_path": str(imf)}),
        ConditionStep(CondCmd.MEAS_RUNNING, None, timeout_s=5.0),
        CommandStep(ProcCmd.START, (1,)),
    ])
    assert wait_for(lambda: stopped == [True], timeout=5.0)
    assert any("pomiar nie wystartował" in e for e in errors)
    assert finished == []                 # bramka powstrzymała finish
    assert not r9.is_running()
    iv.sim.fail_mode = ""
    iv.shutdown(abort=False)

    # 13. odpowiedzi na KOMENDY serii zbrojenia (emitowane PO zbrojeniu,
    #     seq > starego progu!) nie spełniają bramki — wykryte na
    #     procedures3: STOPED spełniony natychmiast odpowiedzią na
    #     CLEAR (running=False sprzed startu pompy). Próg =
    #     seq(zarezerwowane) + qsize = dokładnie za serią.
    import queue as _queue


    class SeqFake(FakeHandle):
        def __init__(self, caps):
            super().__init__(caps)
            self._seq = 0  # licznik rezerwacji workera
            self._q = _queue.Queue()  # kolejka niedebranych ops

        def status_seq(self):
            return self._seq

        def pending_status(self):
            return self._q.qsize()

        def submit(self, op, **kw):
            self.ops.append((op, kw))
            self._q.put((op, kw))  # jak worker: kolejka rośnie
            return True


    hs = SeqFake(demo)
    hs._seq = 3  # historia: 3 polle sprzed procedury
    fin13: list = []
    r10 = ProcedureRunner({1: hs})
    r10.finished.connect(lambda: fin13.append(True))
    assert r10.run([
        CommandStep(ProcCmd.START, (1,)),  # seria = 1 komenda
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=10.0),
    ])
    # zbrojenie: próg = 3 (licznik) + 1 (kolejka) = 4

    # odpowiedź na komendę serii (seq 4 == próg) → ODFILTROWANA:
    stale = PumpStatus(running=False)
    stale.seq = 4
    hs._s.status.emit(stale)
    assert r10.is_running(), "odpowiedź serii spełniła bramkę!"

    # dopiero poll za serią (seq > próg) ma głos: bieg → stop
    for run_, seq in ((True, 5), (False, 6)):
        st13 = PumpStatus(running=run_)
        st13.seq = seq
        hs._s.status.emit(st13)
    assert wait_for(lambda: fin13 == [True])

    print("runner: OK")