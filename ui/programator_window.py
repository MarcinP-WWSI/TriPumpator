"""Programator: projektowanie i bieg procedur pomp w krokach.

Paleta (z PROC_COMMANDS) -> listwa kroków (describe) -> Run.
Edycja kroku: _StepDialog (argumenty wg CommandSpec, pompy jako
checkboxy, walidacja z capabilities sesji przy OK).

Runner: ProcedureRunner (GUI-wątek, WAIT na QTimer). Okno pokazuje
postęp krok po kroku; zamknięcie okna w biegu -> stop.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QMainWindow, QMessageBox, QPushButton, QVBoxLayout, QWidget, QFileDialog,
)

from core.pump.base import PumpCapabilities, PumpStatus
from core.pump.worker import PumpHandle
from core.proc.runner import ProcedureRunner
from core.proc.steps import (
    CommandStep, CommandSpec, ConditionStep, LoopEndStep, LoopStartStep,
    ProcCmd, PROC_COMMANDS, CONDITIONS, describe, CondCmd
)
from core.proc.storage import (
    PROC_DIR, PROC_SUFFIX, ProcStorageError, load_procedure,
    save_procedure,
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

    def __init__(self, step, pids: list[int],
                 caps: dict[int, PumpCapabilities], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Krok")
        self._step = step
        self._pids = pids
        self._caps = caps
        self.count_spin = None
        self.extra_box = None
        self._fields: dict[str, QWidget] = {}
        self._pump_checks: dict[int, QCheckBox] = {}
        self._all_box = None
        lay = QVBoxLayout(self)

        if isinstance(step, LoopStartStep):
            lay.addWidget(QLabel("Powtórzenia pętli:"))
            self.count_spin = _spin(1, 999, 0, float(step.count))
            lay.addWidget(self.count_spin)
            self.extra_box = None
        elif isinstance(step, ConditionStep):
            from core.proc.steps import CONDITIONS, ON_TIMEOUT
            lay.addWidget(QLabel("Warunek:"))
            self.cond_combo = QComboBox()
            for spec in CONDITIONS.values():
                self.cond_combo.addItem(spec.label)
            self.cond_combo.setCurrentText(CONDITIONS[step.cond].label)
            lay.addWidget(self.cond_combo)

            lay.addWidget(QLabel("Pompa:"))
            self.cond_pump_combo = QComboBox()
            for pid in pids:
                self.cond_pump_combo.addItem(f"pompa {pid}", pid)
            i = self.cond_pump_combo.findData(step.pump)
            self.cond_pump_combo.setCurrentIndex(max(i, 0))
            lay.addWidget(self.cond_pump_combo)

            # argumenty (widoczne tylko dla warunków, które je mają)
            self.cond_value_spin = _spin(val=float(
                step.args.get("value", 1.0)))
            self.cond_unit_combo = QComboBox()
            ref = caps[pids[0]] if pids else None
            if ref:
                self.cond_unit_combo.addItems(list(ref.volume_units))
            self.cond_unit_combo.setCurrentText(
                step.args.get("unit", ref.volume_units[0] if ref
                else "mL"))
            lay.addWidget(self.cond_value_spin)
            lay.addWidget(self.cond_unit_combo)
            self.cond_combo.currentTextChanged.connect(
                self._on_cond_changed)
            self._on_cond_changed(self.cond_combo.currentText())

            lay.addWidget(QLabel("Timeout [s]:"))
            self.cond_timeout_spin = _spin(0.1, 99999.0, 1,
                                           float(step.timeout_s))
            lay.addWidget(self.cond_timeout_spin)

            lay.addWidget(QLabel("Po timeout:"))
            self.cond_on_timeout_combo = QComboBox()
            self.cond_on_timeout_combo.addItems(list(ON_TIMEOUT))
            self.cond_on_timeout_combo.setCurrentText(step.on_timeout)
            lay.addWidget(self.cond_on_timeout_combo)
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

    def _on_cond_changed(self, label: str) -> None:
        """Pokaż/ukryj pola argumentów wg warunku (tylko DISPENSED_GE)."""
        from core.proc.steps import CondCmd
        has_args = label == "dozowane >="
        self.cond_value_spin.setVisible(has_args)
        self.cond_unit_combo.setVisible(has_args)
        # etykieta nie jest potrzebna — spin pojawia się tuż pod "Pompa:"
        # poprawka UX: doklejamy opis wartości
        self.cond_value_spin.setToolTip(
            "próg dozowanej objętości (mL/µL)" if has_args else "")

    def _cond_result_step(self, step: ConditionStep) -> ConditionStep:
        from core.proc.steps import CONDITIONS
        cond = next(c for c, s in CONDITIONS.items()
                    if s.label == self.cond_combo.currentText())
        pump = self.cond_pump_combo.currentData()
        args: dict = {}
        if cond is CondCmd.DISPENSED_GE:
            args = {"value": self.cond_value_spin.value(),
                    "unit": self.cond_unit_combo.currentText()}
        return ConditionStep(cond, pump, args,
                             timeout_s=self.cond_timeout_spin.value(),
                             on_timeout=self.cond_on_timeout_combo
                             .currentText())

    def result_step(self):
        """Nowy krok z pól dialogu (bez walidacji — robi caller)."""
        if self.count_spin is not None:
            return LoopStartStep(int(self.count_spin.value()))
        if isinstance(self._step, ConditionStep):
            return self._cond_result_step(self._step)
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
        self.palette.addItem("Warunek…")
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
        self.save_btn = QPushButton("💾 Zapisz")
        self.save_btn.clicked.connect(self._on_save)
        self.load_btn = QPushButton("📂 Wczytaj")
        self.load_btn.clicked.connect(self._on_load)
        run_row.addWidget(self.save_btn)
        run_row.addWidget(self.load_btn)
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
        pos = self._sel() + 1 if self._sel() >= 0 else len(self._steps)
        caps_map = {p: h.capabilities for p, h in self._handles.items()}
        pids = list(self._handles)
        if label == "Warunek…":
            step = ConditionStep(CondCmd.STOPPED,
                                 pids[0] if pids else 1)
        else:
            spec = next(s for s in PROC_COMMANDS.values()
                        if s.label == label)
            step = CommandStep(spec.cmd)
        dlg = _StepDialog(step, pids, caps_map, self)
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

    # ------------------------------------------------------ persystencja

    def _on_save(self) -> None:
        if self._runner.is_running():
            self.error_label.setText("stop procedury przed zapisem")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Zapisz procedurę", str(PROC_DIR),
            f"Procedury (*{PROC_SUFFIX});;Wszystkie (*)")
        if not path:
            return
        try:
            save_procedure(self._steps, path)
            self.status_label.setText(f"zapisano: {Path(path).name}")
            self.error_label.setText("")
        except ProcStorageError as e:
            self.error_label.setText(str(e))

    def _on_load(self) -> None:
        if self._runner.is_running():
            self.error_label.setText("stop procedury przed wczytaniem")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Wczytaj procedurę", str(PROC_DIR),
            f"Procedury (*{PROC_SUFFIX});;Wszystkie (*)")
        if not path:
            return
        try:
            steps = load_procedure(path)
        except ProcStorageError as e:
            self.error_label.setText(str(e))
            return
        self._steps = steps
        self._refresh_list()
        self.status_label.setText(
            f"wczytano: {Path(path).name} ({len(steps)} kroków)")
        self.error_label.setText("")

    def _on_stop(self) -> None:
        self._runner.stop()

    def _on_step(self, index: int, total: int, text: str) -> None:
        if "Czekaj aż" in text:
            self.status_label.setText(f"krok {index + 1}/{total}: {text}")
        else:
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

    # 1. struktura: paleta = komendy + jedno wejście "Warunek…"
    assert w.palette.count() == len(PROC_COMMANDS) + 1
    labels = {w.palette.item(i).text()
              for i in range(w.palette.count())}
    assert "Start pomp" in labels and "Warunek…" in labels
    assert w.steps_list.count() == 0
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

    # 9. warunki: NIE na palecie (jedno wejście "Warunek…"); opis na
    #    liście dalej z describe
    assert "pompa zatrzymana" not in labels
    from core.proc.steps import ConditionStep, CondCmd

    w._steps.append(ConditionStep(CondCmd.STOPPED, 1, timeout_s=60.0))
    w._refresh_list()
    assert w.steps_list.item(w.steps_list.count() - 1).text() == \
           "Czekaj aż pompa zatrzymana [pompa 1] (max 60 s)"

    # 10. procedura z bramką przechodzi walidację i biegnie
    #     (świeże handl'e, żeby nie szumiały poprzednie testy)
    h1b, h2b = FakeHandle(demo), FakeHandle(demo)
    wb = ProgramatorWindow({1: h1b, 2: h2b})
    wb._steps.extend([
        CommandStep(ProcCmd.START, (1,)),
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=2.0),
        CommandStep(ProcCmd.STOP, (1,)),
    ])
    wb._refresh_list(0)
    wb._on_run()
    assert wb._runner.is_running()  # czeka na bramkę
    h1b._s.status.emit(PumpStatus(running=True))
    assert wb._runner.is_running()  # pracuje -> czeka dalej
    h1b._s.status.emit(PumpStatus(running=False))  # STOPPED spełnione
    # direct-emit w GUI-wątku: bramka zwolniona synchronicznie, STOP poszedł
    assert h1b.ops[-1] == (Op.STOP, {})
    assert wb.status_label.text() == "zakończono"
    wb.close()

    # 11. persystencja: round-trip przez przyciskowe API + guard w biegu
    import tempfile
    from pathlib import Path
    from core.proc.storage import save_procedure, load_procedure

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "t.proc.json"
        w._steps = [CommandStep(ProcCmd.START, None),
                    ConditionStep(CondCmd.STOPPED, 1, timeout_s=15.0)]
        w._refresh_list()
        save_procedure(w._steps, f)
        w._steps = []
        w._refresh_list()
        loaded = load_procedure(f)
        assert len(loaded) == 2
        assert isinstance(loaded[1], ConditionStep)
        assert loaded[1].timeout_s == 15.0
        w._steps = loaded  # kompletacja obiegu: jak _on_load
        w._refresh_list()
        assert w.save_btn is not None and w.load_btn is not None
        w._on_run()  # START + bramka STOPPED (15 s)
        assert w._runner.is_running()
        w._on_save()
        assert "stop procedury" in w.error_label.text()
        w._on_load()
        assert "stop procedury" in w.error_label.text()
        w._on_stop()
        assert not w._runner.is_running()

        # 12. dialogi bez exec(): result_step dla WSZYSTKICH typów kroków
        #     (regresja crasha 0xC0000409 — zapomniany count_spin)
        caps_map = {p: h.capabilities for p, h in handles.items()}
        pids = list(handles)

        d_loop = _StepDialog(LoopStartStep(2), pids, caps_map)
        d_loop.count_spin.setValue(7)
        assert d_loop.result_step() == LoopStartStep(7)

        d_cmd = _StepDialog(CommandStep(ProcCmd.SET_RATE, (1,)), pids,
                            caps_map)
        d_cmd._fields["value"].setValue(120.0)
        d_cmd._fields["unit"].setCurrentText("µL/min")
        st = d_cmd.result_step()
        assert isinstance(st, CommandStep)
        assert st.args["value"] == 120.0 and st.args["unit"] == "µL/min"
        assert st.pumps == (1,)

        d_cond = _StepDialog(ConditionStep(CondCmd.STOPPED, 1), pids,
                             caps_map)
        d_cond.cond_combo.setCurrentText("dozowane >=")
        d_cond.cond_value_spin.setValue(2.5)
        d_cond.cond_unit_combo.setCurrentText("mL")
        d_cond.cond_timeout_spin.setValue(30.0)
        d_cond.cond_on_timeout_combo.setCurrentText("continue")
        st = d_cond.result_step()
        assert isinstance(st, ConditionStep)
        assert st.cond is CondCmd.DISPENSED_GE and st.pump == 1
        assert st.args == {"value": 2.5, "unit": "mL"}
        assert st.timeout_s == 30.0 and st.on_timeout == "continue"

    print("programator_window: OK")