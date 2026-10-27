"""Programator: projektowanie i bieg procedur pomp w krokach.

Paleta (z PROC_COMMANDS) -> listwa kroków (describe) -> Run.
Edycja kroku: _StepDialog (argumenty wg CommandSpec, pompy jako
checkboxy, walidacja z capabilities sesji przy OK).

Runner: ProcedureRunner (GUI-wątek, WAIT na QTimer). Okno pokazuje
postęp krok po kroku; zamknięcie okna w biegu -> stop.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.pump.base import PumpCapabilities
from core.pump.worker import PumpHandle
from core.proc.runner import ProcedureRunner
from core.proc.steps import (
    CommandStep, CommandSpec, LoopEndStep, LoopStartStep, ProcCmd,
    PROC_COMMANDS, describe,
)

_TIME_UNITS = ("s", "min", "h")


def _spin(lo=0.0, hi=99999.0, dec=3, val=1.0) -> QDoubleSpinBox:
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(dec)
    sb.setValue(val)
    return sb


class _StepDialog(QDialog):
    """Edycja jednego CommandStep (lub pętli). OK = walidacja z caps."""

    def __init__(self, step, pids: list[int], caps: dict[int, PumpCapabilities],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Krok")
        self._step = step
        self._pids = pids
        self._caps = caps
        lay = QVBoxLayout(self)

        if isinstance(step, LoopStartStep):
            lay.addWidget(QLabel("Powtórzenia pętli:"))
            self.count_spin = _spin(1, 999, 0, float(step.count))
            lay.addWidget(self.count_spin)
            self.extra_box = None
        else:
            self.count_spin = None
            spec = PROC_COMMANDS[step.cmd]
            grid = QGridLayout()
            lay.addLayout(grid)
            self._fields: dict[str, QWidget] = {}
            row = 0
            for name in spec.args:
                grid.addWidget(QLabel(f"{name}:"), row, 0)
                if name in ("unit",) and step.cmd is ProcCmd.SET_RATE:
                    ref_caps = caps[pids[0]] if pids else None
                    w = QComboBox()
                    if ref_caps:
                        w.addItems(list(ref_caps.rate_units))
                    self._fields[name] = w
                elif name == "unit" and step.cmd is ProcCmd.SET_TARGET_VOLUME:
                    ref_caps = caps[pids[0]] if pids else None
                    w = QComboBox()
                    if ref_caps:
                        w.addItems(list(ref_caps.volume_units))
                    self._fields[name] = w
                elif name == "direction":
                    ref_caps = caps[pids[0]] if pids else None
                    w = QComboBox()
                    if ref_caps:
                        w.addItems(list(ref_caps.directions))
                    self._fields[name] = w
                elif name == "seconds":
                    w = _spin(0.1, 99999.0, 1,
                              float(step.args.get(name, 1.0)))
                    self._fields[name] = w
                else:
                    v = float(step.args.get(name, 1.0))
                    w = _spin(val=v)
                    self._fields[name] = w
                grid.addWidget(w, row, 1)
                row += 1

            # pompy: checkboxy + "wszystkie"
            if PROC_COMMANDS[step.cmd].needs_pumps:
                lay.addWidget(QLabel("Pompy:"))
                self._pump_checks: dict[int, QCheckBox] = {}
                all_box = QCheckBox("wszystkie")
                if step.pumps is None:
                    all_box.setChecked(True)
                lay.addWidget(all_box)
                for pid in pids:
                    cb = QCheckBox(f"pompa {pid} ({caps[pid].name})")
                    if step.pumps is not None and pid in step.pumps:
                        cb.setChecked(True)
                    self._pump_checks[pid] = cb
                    lay.addWidget(cb)
                self._all_box = all_box

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def result_step(self):
        """Nowy krok z pól dialogu (bez walidacji — robi caller)."""
        if self.count_spin is not None:
            return LoopStartStep(int(self.count_spin.value()))
        spec = PROC_COMMANDS[self._step.cmd]
        args = {}
        for name, w in self._fields.items():
            if isinstance(w, QComboBox):
                args[name] = w.currentText()
            else:
                args[name] = w.value()
        if getattr(self, "_all_box", None) is not None:
            if self._all_box.isChecked():
                pumps = None
            else:
                pumps = tuple(pid for pid, cb in self._pump_checks.items()
                              if cb.isChecked())
        else:
            pumps = self._step.pumps
        return CommandStep(self._step.cmd, pumps, args)


class ProgramatorWindow(QMainWindow):
    closed = pyqtSignal()

    def __init__(self, handles: dict[int, PumpHandle], parent=None):
        super().__init__(parent)
        self.setWindowTitle("TriPumpator — Programator")
        self._handles = dict(handles)
        self._runner = ProcedureRunner(self._handles)
        self._steps: list = []

        self._runner.step_changed.connect(self._on_step)
        self._runner.finished.connect(self._on_finished)
        self._runner.stopped.connect(self._on_stopped)
        self._runner.error.connect(self._on_runner_error)

        central = QWidget()
        root = QVBoxLayout(central)

        top = QHBoxLayout()
        self.status_label = QLabel("gotowy")
        top.addWidget(self.status_label)
        top.addStretch()
        self.add_loop_btn = QPushButton("+ Pętla")
        self.add_loop_btn.clicked.connect(self._add_loop)
        top.addWidget(self.add_loop_btn)
        root.addLayout(top)

        cols = QHBoxLayout()
        # paleta
        pal_col = QVBoxLayout()
        pal_col.addWidget(QLabel("Komendy:"))
        self.palette = QListWidget()
        for spec in PROC_COMMANDS.values():
            self.palette.addItem(spec.label)
        self.palette.itemClicked.connect(self._on_palette)
        pal_col.addWidget(self.palette)
        cols.addLayout(pal_col, 1)

        # listwa procedury
        proc_col = QVBoxLayout()
        proc_col.addWidget(QLabel("Procedura:"))
        self.steps_list = QListWidget()
        self.steps_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.steps_list.itemDoubleClicked.connect(self._on_edit)
        proc_col.addWidget(self.steps_list)
        edit_row = QHBoxLayout()
        self.up_btn = QPushButton("▲")
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn = QPushButton("▼")
        self.down_btn.clicked.connect(lambda: self._move(+1))
        self.edit_btn = QPushButton("Edytuj")
        self.edit_btn.clicked.connect(self._on_edit_btn)
        self.del_btn = QPushButton("Usuń")
        self.del_btn.clicked.connect(self._on_delete)
        for b in (self.up_btn, self.down_btn, self.edit_btn, self.del_btn):
            edit_row.addWidget(b)
        proc_col.addLayout(edit_row)
        cols.addLayout(proc_col, 3)
        root.addLayout(cols)

        run_row = QHBoxLayout()
        self.run_btn = QPushButton("▶ Uruchom")
        self.run_btn.setStyleSheet("font-weight: bold;")
        self.run_btn.clicked.connect(self._on_run)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        run_row.addWidget(self.run_btn)
        run_row.addWidget(self.stop_btn)
        run_row.addStretch()
        root.addLayout(run_row)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #e65100;")
        root.addWidget(self.error_label)

        self.setCentralWidget(central)

    # ------------------------------------------------------------ lista

    def _refresh_list(self, select: int | None = None) -> None:
        self.steps_list.clear()
        for step in self._steps:
            self.steps_list.addItem(describe(step))
        if select is not None and 0 <= select < len(self._steps):
            self.steps_list.setCurrentRow(select)

    def _sel(self) -> int:
        return self.steps_list.currentRow() if self.steps_list.count() else -1

    def _on_palette(self, item) -> None:
        if self._runner.is_running():
            return
        label = item.text()
        spec = next(s for s in PROC_COMMANDS.values() if s.label == label)
        step = CommandStep(spec.cmd)
        pos = self._sel() + 1 if self._sel() >= 0 else len(self._steps)
        dlg = _StepDialog(step, list(self._handles),
                          {p: h.capabilities
                           for p, h in self._handles.items()}, self)
        if dlg.exec():
            self._steps.insert(pos, dlg.result_step())
            self._refresh_list(pos)

    def _add_loop(self) -> None:
        pos = self._sel() + 1 if self._sel() >= 0 else len(self._steps)
        self._steps.insert(pos, LoopStartStep(2))
        self._steps.insert(pos + 1, LoopEndStep())
        self._refresh_list(pos)

    def _on_edit_btn(self) -> None:
        if self._sel() >= 0:
            self._on_edit(self.steps_list.currentItem())

    def _on_edit(self, item) -> None:
        if self._runner.is_running() or self._sel() < 0:
            return
        idx = self._sel()
        step = self._steps[idx]
        if isinstance(step, LoopEndStep):
            return
        dlg = _StepDialog(step, list(self._handles),
                          {p: h.capabilities for p, h in self._handles.items()},
                          self)
        if dlg.exec():
            self._steps[idx] = dlg.result_step()
            self._refresh_list(idx)

    def _on_delete(self) -> None:
        if self._runner.is_running() or self._sel() < 0:
            return
        idx = self._sel()
        del self._steps[idx]
        self._refresh_list(min(idx, len(self._steps) - 1))

    def _move(self, delta: int) -> None:
        if self._runner.is_running() or self._sel() < 0:
            return
        idx = self._sel()
        new = idx + delta
        if not 0 <= new < len(self._steps):
            return
        self._steps.insert(new, self._steps.pop(idx))
        self._refresh_list(new)

    # -------------------------------------------------------------- run

    def _on_run(self) -> None:
        self.error_label.setText("")
        from core.proc.steps import validate
        caps = {p: h.capabilities for p, h in self._handles.items()}
        errs = validate(self._steps, caps)
        if errs:
            self.error_label.setText(";\n".join(errs[:3]))
            if len(errs) > 3:
                self.error_label.setText(
                    self.error_label.text() + f"; …(+{len(errs) - 3})")
            return
        self._runner.run(self._steps)
        running = self._runner.is_running()
        self.run_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)

    def _on_stop(self) -> None:
        self._runner.stop()

    def _on_step(self, index: int, total: int, text: str) -> None:
        self.status_label.setText(f"krok {index + 1}/{total}: {text}")

    def _on_finished(self) -> None:
        self.status_label.setText("zakończono")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_stopped(self) -> None:
        self.status_label.setText("przerwano")
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def _on_runner_error(self, msg: str) -> None:
        self._on_stopped()
        self.error_label.setText(msg)

    def closeEvent(self, event) -> None:
        if self._runner.is_running():
            self._runner.stop()
        self.closed.emit()
        event.accept()


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    from core.pump.base import Op, PumpCapabilities
    from core.pump.demo import DemoDriver
    from core.proc.steps import validate

    class FakeHandle:
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

    demo = DemoDriver.capabilities
    h1, h2 = FakeHandle(demo), FakeHandle(demo)
    handles = {1: h1, 2: h2}

    w = ProgramatorWindow(handles)

    # 1. struktura: paleta wygenerowana z rejestru, listwa pusta
    assert w.palette.count() == len(PROC_COMMANDS)
    assert w.steps_list.count() == 0
    assert not w.run_btn.isEnabled() or w.run_btn.isEnabled()  # stan idle
    assert w.run_btn.isEnabled() and not w.stop_btn.isEnabled()

    # 2. pętla: para kroków wchodzi na listwę, edycja zmienia licznik
    w._add_loop()
    assert w.steps_list.count() == 2
    assert w.steps_list.item(0).text() == "PĘTLA ×2"
    assert w.steps_list.item(1).text() == "KONIEC PĘTLI"
    w.steps_list.setCurrentRow(0)
    from core.proc.steps import LoopStartStep, CommandStep, ProcCmd
    w._steps[0] = LoopStartStep(5)
    w._refresh_list(0)
    assert w.steps_list.item(0).text() == "PĘTLA ×5"

    # 3. dodanie komendy przez paletę z pominięciem dialogu
    #    (dialog modalny — w testach wstawiamy krok wprost do _steps)
    w._steps.insert(2, CommandStep(ProcCmd.START, None))
    w._refresh_list(2)
    assert w.steps_list.item(2).text() == "Start pomp [wszystkie]"

    # 4. walidacja z Run: zła procedura -> błąd, runner nie startuje
    w._steps.insert(3, CommandStep(ProcCmd.SET_FORCE, (1,),
                                   {"percent": 200}))  # poza zakresem
    w._refresh_list(3)
    w._on_run()
    assert "zakresem" in w.error_label.text()
    assert not w._runner.is_running() and w.run_btn.isEnabled()

    # 5. popraw i uruchom: pętla ×5 z ciałem (START wewnątrz pętli),
    #    SET_FORCE po pętli (przygotowane w 4 na pozycji za LoopEnd)
    w._steps.clear()
    w._steps.extend([
        LoopStartStep(5),
        CommandStep(ProcCmd.START, None),  # wewnątrz pętli
        LoopEndStep(),
        CommandStep(ProcCmd.SET_FORCE, (1,), {"percent": 50}),
    ])
    w._refresh_list(0)
    w._on_run()
    assert h1.ops[-1] == (Op.SET_FORCE, {"percent": 50.0})
    starts1 = [op for op, _ in h1.ops if op is Op.START]
    assert len(starts1) == 5  # ×5, ciało = START
    starts2 = [op for op, _ in h2.ops if op is Op.START]
    assert len(starts2) == 5  # None -> wszystkie pompy
    assert w.status_label.text() == "zakończono"
    assert w.run_btn.isEnabled() and not w.stop_btn.isEnabled()

    # 6. WAIT 10 s -> bieg; stop -> przerwano; przyciski wróciły
    w._steps.append(CommandStep(ProcCmd.WAIT, None, {"seconds": 10.0}))
    w._refresh_list()
    w._on_run()
    assert w._runner.is_running() and w.stop_btn.isEnabled()
    assert not w.run_btn.isEnabled()
    w._on_stop()
    assert w.status_label.text() == "przerwano"
    assert w.run_btn.isEnabled() and not w.stop_btn.isEnabled()

    # 7. zamknięcie okna w biegu zatrzymuje runnera
    w._steps.append(CommandStep(ProcCmd.WAIT, None, {"seconds": 30.0}))
    w._refresh_list()
    w._on_run()
    assert w._runner.is_running()
    w.close()  # closeEvent -> stop
    assert not w._runner.is_running()
    got_close = []
    # closed już wyemitowane przy close(); nowy ProgramatorWindow:
    w2 = ProgramatorWindow(handles)
    w2.closed.connect(lambda: got_close.append(True))
    w2.close()
    assert got_close == [True]

    # 8. usuwanie i przesuwanie (bez wyjątków; listwa spójna)
    n = w.steps_list.count()
    w.steps_list.setCurrentRow(3)
    w._on_delete()
    assert w.steps_list.count() == n - 1
    w.steps_list.setCurrentRow(0)
    before = w.steps_list.item(0).text()
    w._move(+1)
    assert w.steps_list.item(1).text() == before  # przesunięty w dół

    print("programator_window: OK")