"""Runner procedur Programatora: wykonuje kroki bez własnego wątku.

Projekt: runner żyje w GUI-wątku, bo jego "praca" to wyłącznie
PumpHandle.submit (thread-safe, kolejka workera) i QTimer dla WAIT.
Ani chwili blokującego I/O — okna aplikacji nie marzną.

Pętle: wskaźnik + stos liczników (struktura spłaszczona z steps.py):
  LoopEndStep -> dekrementuj szczyt stosu; >0: skocz za sparowany
  LoopStartStep; ==0: zdejmij parę i kontynuuj.

Stany: idle -> running -> finished | stopped. run() odrzuca przy
biegu; stop() ucieka bieżące czekanie i kończy.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from core.pump.base import Op
from core.pump.worker import PumpHandle
from core.proc.steps import (
    CommandStep, LoopEndStep, LoopStartStep, ProcCmd,
    describe, validate,
)

# bezpiecznik: procedura z pętlami ma twardy limit wykonanych kroków
# (nieograniczona pętla nie zawiesi GUI na zawsze)
MAX_TOTAL_STEPS = 100_000

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


class ProcedureRunner(QObject):
    step_changed = pyqtSignal(int, int, str)   # (index, total, opis kroku)
    finished = pyqtSignal()
    stopped = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, handles: dict[int, PumpHandle], parent=None):
        super().__init__(parent)
        self._handles = dict(handles)     # pid -> handle (snapshot)
        self._steps: list = []
        self._pos = -1                    # indeks bieżącego kroku
        self._loop_stack: list[list] = []  # [start_idx, pozostalo]
        self._running = False
        self._timer: QTimer | None = None

    # ------------------------------------------------------------- API

    def is_running(self) -> bool:
        return self._running

    def is_waiting(self) -> bool:
        """True gdy procedura wstrzymana na WAIT (timer aktywny)."""
        return self._timer is not None

    def run(self, steps: list) -> bool:
        """Zwaliduj i wykonaj procedurę. False = odrzucono (błąd walidacji
        albo runner już aktywny) — szczegóły w sygnale error."""
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
        """Przerwij bieg: ucieka bieżące czekanie, nic nie emituje z błędu."""
        if not self._running:
            return
        self._running = False
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        self.stopped.emit()

    # ------------------------------------------------------------ silnik

    def _advance(self) -> None:
        """Wykonuj kroki aż do WAIT/konca; wołane natychmiast i z timera."""
        if not self._running:
            return
        guard = 0
        while self._running:
            guard += 1
            if guard > MAX_TOTAL_STEPS:
                self._running = False
                self.error.emit("przekroczono limit wykonanych kroków "
                                f"({MAX_TOTAL_STEPS}) — przerywam")
                self.stopped.emit()
                return
            self._pos += 1
            if self._pos >= len(self._steps):
                self._running = False
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
                    self._running = False     # walidacja powinna złapać,
                    self.error.emit("pętla bez początku")  # tu fail-safe
                    self.stopped.emit()
                    return
                top = self._loop_stack[-1]
                top[1] -= 1
                if top[1] > 0:
                    self._pos = top[0]        # skok za LoopStart
                else:
                    self._loop_stack.pop()
                continue
            # CommandStep
            self._emit_progress(step)
            if step.cmd is ProcCmd.WAIT:
                self._start_wait(float(step.args["seconds"]))
                return                          # _advance z timera
            self._dispatch(step)

    def _dispatch(self, step: CommandStep) -> None:
        op, arg_names = _CMD_TO_OP[step.cmd]
        kw = {name: step.args[name] for name in arg_names}
        pids = list(self._handles) if step.pumps is None \
            else [p for p in step.pumps if p in self._handles]
        for pid in pids:
            self._handles[pid].submit(op, **kw)

    def _start_wait(self, seconds: float) -> None:
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_wait_done)
        self._timer.start(int(seconds * 1000))

    def _on_wait_done(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        self._advance()

    def _emit_progress(self, step) -> None:
        self.step_changed.emit(self._pos, len(self._steps), describe(step))


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import time

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

    def wait_for(cond, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()      # dostarcza timeout QTimera
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

    # 1. prosta procedura: START -> WAIT 0.2 s -> STOP
    r = ProcedureRunner(handles)
    r.step_changed.connect(lambda i, t, s: seen.append((i, s)))
    r.finished.connect(lambda: finished.append(True))
    r.error.connect(errors.append)
    r.stopped.connect(lambda: stopped.append(True))

    from core.proc.steps import CommandStep, ProcCmd
    proc = [
        CommandStep(ProcCmd.START, None),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 0.2}),
        CommandStep(ProcCmd.STOP, (1,)),      # stop tylko pompy 1
    ]
    assert r.run(proc)
    assert h1.ops[-1] == (Op.START, {}) and h2.ops[-1] == (Op.START, {})
    assert r.is_running() and r.is_waiting()  # czeka na WAIT 0.2 s            # czeka na WAIT
    assert wait_for(lambda: finished == [True])
    assert h1.ops[-1] == (Op.STOP, {})        # stop poszedł do obu
    # STOP (1,) po wait — ostatni op h2 to nadal START:
    assert h2.ops[-1] == (Op.START, {})
    assert seen[0][1] == "Start pomp [wszystkie]"
    assert "Czekaj" in seen[1][1]
    assert [i for i, _ in seen] == [0, 1, 2]

    # 2. pętla ×2: kroki wykonane dwukrotnie, potem finish
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
    from core.proc.steps import LoopStartStep, LoopEndStep
    assert r.run(proc2)
    assert wait_for(lambda: finished == [True])
    starts = [op for op, _ in h1.ops if op is Op.START]
    assert len(starts) == 2                    # pętla ×2
    purges = [op for op, _ in h1.ops if op is Op.PURGE]
    assert len(purges) == 1                    # po pętli
    # kolejność: CLD, [START, STOP] ×2, PURGE
    seq = [op for op, _ in h1.ops if op is not Op.STOP]
    assert seq == [Op.CLEAR_DISPENSED, Op.START, Op.START, Op.PURGE]

    # 3. zagnieżdżone pętle ×2/×2: wewn. 4×, zewn. kroki 2×
    h1.ops.clear(); seen.clear(); finished.clear()
    proc3 = [
        LoopStartStep(2),  # zewnętrzna
        CommandStep(ProcCmd.START, (1,)),
        LoopStartStep(2),  # wewnętrzna
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
    assert len(starts) == 2 and len(stops) == 2    # zewnętrzna ×2
    assert len(waits) == 4                         # wewnętrzna 2×2

    # 4. stop w trakcie WAIT: przerwane, brak finish
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
    assert wait_for(lambda: True)                  # pętla zdarzeń: nic nie leci
    assert finished == []
    # STOP nie doszedł

    # 5. run po stopie działa; błąd walidacji zgłaszany; pusta -> finish
    assert r2.run([CommandStep(ProcCmd.START, (1,))])
    assert wait_for(lambda: finished == [True])
    h1.ops.clear()
    errs_before = len(errors)
    assert not r2.run(
        [CommandStep(ProcCmd.SET_RATE, (1,), {"value": 1})])  # brak unit
    assert len(errors) > errs_before
    finished.clear()
    assert r2.run([])  # pusta: natychmiast finish
    assert wait_for(lambda: finished == [True])

    # 6. WAIT na granicy MIN_WAIT_S (0.1 s) przechodzi
    h1.ops.clear();
    finished.clear()
    assert r2.run([
        CommandStep(ProcCmd.START, (1,)),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 0.1}),
        CommandStep(ProcCmd.STOP, (1,)),
    ])
    assert wait_for(lambda: finished == [True])

    print("runner: OK")