"""Okno wyboru: wiersze (port + model pompy) -> konfiguracja sesji.

Umowa:
    session_requested(list[(port|None, model_key)])   po walidacji
    SelectorWindow(scan_source=..., settings=..., error_reporter=...)
        scan_source    — seam skanera portów (testy podają fake)
        settings       — obiekt o API QSettings (value/setValue)
        error_reporter — callable(msg); domyślnie QMessageBox

Walidacja przy "Uruchom":
    - model realny bez portu      -> błąd
    - port realny użyty 2 razy    -> błąd
    - zero wierszy                -> błąd
    - model demo ignoruje port (emit None)
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QSettings, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout, QWidget,
)

from core.pump.demo import DemoDriver
from core.pump.registry import REGISTRY
from core.port_scanner import PortInfo, scan_ports

_PLACEHOLDER = "— brak portu (demo) —"


class _Row(QWidget):
    """Jeden wiersz: combo portu + combo modelu + przycisk usuń."""

    def __init__(self, on_remove: Callable[["_Row"], None]):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.port_cb = QComboBox()
        self.model_cb = QComboBox()
        self.rm_btn = QPushButton("–")
        self.rm_btn.setFixedWidth(32)
        self.rm_btn.clicked.connect(lambda: on_remove(self))
        lay.addWidget(self.port_cb, 3)
        lay.addWidget(self.model_cb, 2)
        lay.addWidget(self.rm_btn)

    def set_ports(self, ports: list[PortInfo]) -> None:
        """Odśwież porty, zachowaj wybór PO DANYCH (nie po indeksie)."""
        prev = self.port_cb.currentData()
        self.port_cb.clear()
        self.port_cb.addItem(_PLACEHOLDER, None)
        for p in ports:
            self.port_cb.addItem(p.label, p.device)
        for i in range(self.port_cb.count()):
            if self.port_cb.itemData(i) == prev:
                self.port_cb.setCurrentIndex(i)
                break
        else:
            self.port_cb.setCurrentIndex(0)


class SelectorWindow(QDialog):
    session_requested = pyqtSignal(list)   # [(port|None, model_key), ...]

    def __init__(self, scan_source=None, settings=None,
                 error_reporter: Callable[[str], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("TriPumpator — konfiguracja stanowiska")
        self._scan_source = scan_source
        self._settings = settings if settings is not None \
            else QSettings("WWSI", "TriPumpator")
        self._error = error_reporter or self._default_error
        self._ports: list[PortInfo] = []
        self.rows: list[_Row] = []

        root = QVBoxLayout(self)
        root.addWidget(QLabel(
            "Wiersz = jedna pompa. Model demo nie wymaga portu COM."))

        self._rows_box = QVBoxLayout()
        root.addLayout(self._rows_box)

        btns = QHBoxLayout()
        add_btn = QPushButton("+ Dodaj pompę")
        add_btn.clicked.connect(lambda: self._add_row())
        refresh_btn = QPushButton("Odśwież porty")
        refresh_btn.clicked.connect(self.refresh_ports)
        btns.addWidget(add_btn)
        btns.addWidget(refresh_btn)
        btns.addStretch()
        root.addLayout(btns)

        start_btn = QPushButton("▶ Uruchom sesję")
        start_btn.setStyleSheet("font-weight: bold;")
        start_btn.clicked.connect(self._emit)
        root.addWidget(start_btn)

        self.refresh_ports()
        self._restore()

    # ------------------------------------------------------------- API

    def refresh_ports(self) -> None:
        """Rescan portów + aktualizacja wszystkich combo (zachowaj wybór)."""
        self._ports = scan_ports(source=self._scan_source)
        for row in self.rows:
            row.set_ports(self._ports)

    # ------------------------------------------------------- wiersze

    def _add_row(self, port: str | None = None,
                 model_key: str | None = None) -> _Row:
        row = _Row(self._remove_row)
        row.model_cb.addItems(list(REGISTRY))
        row.model_cb.currentTextChanged.connect(
            lambda _, r=row: self._on_model_changed(r))
        self._rows_box.addWidget(row)
        self.rows.append(row)
        row.set_ports(self._ports)
        if model_key:
            row.model_cb.setCurrentText(model_key)
        if port:
            i = row.port_cb.findData(port)
            if i >= 0:
                row.port_cb.setCurrentIndex(i)
        self._on_model_changed(row)
        return row

    def _remove_row(self, row: _Row) -> None:
        self.rows.remove(row)
        self._rows_box.removeWidget(row)
        row.deleteLater()

    def _on_model_changed(self, row: _Row) -> None:
        demo = REGISTRY[row.model_cb.currentText()] is DemoDriver
        row.port_cb.setEnabled(not demo)

    # ---------------------------------------------------------- start

    def _emit(self) -> None:
        if not self.rows:
            self._error("Dodaj przynajmniej jedną pompę.")
            return
        cfg: list[tuple[str | None, str]] = []
        seen: set[str] = set()
        for row in self.rows:
            key = row.model_cb.currentText()
            demo = REGISTRY[key] is DemoDriver
            port = row.port_cb.currentData()
            if not demo:
                if port is None:
                    self._error(f"{key}: wybierz port COM dla tego modelu.")
                    return
                if port in seen:
                    self._error(
                        f"Port {port} jest już przypisany innej pompie.")
                    return
                seen.add(port)
            cfg.append((None if demo else port, key))
        self._settings.setValue("rows", [[p, k] for p, k in cfg])
        self.session_requested.emit(cfg)
        self.accept()

    def _restore(self) -> None:
        """Odtwórz wiersze z ustawień; porażka -> jeden wiersz domyślny."""
        saved = self._settings.value("rows", None)
        if saved:
            try:
                for port, key in saved:
                    self._add_row(port, key)
                return
            except Exception:
                pass                     # zepsute ustawienia -> układ domyślny
        self._add_row()

    def _default_error(self, msg: str) -> None:
        QMessageBox.warning(self, "Konfiguracja", msg)


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtWidgets import QApplication

    app = QApplication([])

    class FakePort:
        def __init__(self, device, desc="", vid=None, pid=None, sn=None,
                     hwid=""):
            self.device, self.description = device, desc
            self.vid, self.pid = vid, pid
            self.serial_number, self.hwid = sn, hwid

    class FakeSettings:
        def __init__(self):        self.d = {}
        def value(self, k, default=None): return self.d.get(k, default)
        def setValue(self, k, v):   self.d[k] = v

    fake_ports = [
        FakePort("COM3", "USB Serial Port", vid=0x0403, pid=0x6001, sn="A1"),
        FakePort("COM5", "CH340", vid=0x1A86, pid=0x7523, sn=None),
    ]
    src = lambda: fake_ports

    AL, DEMO = "WPI AL-1000", "Pompa demo (symulator)"
    errs: list[str] = []
    got: list = []

    st = FakeSettings()
    s = SelectorWindow(scan_source=src, settings=st,
                       error_reporter=errs.append)
    s.session_requested.connect(got.append)

    # 1. układ domyślny: 1 wiersz, pierwszy model rejestru, porty zaskanowane
    assert len(s.rows) == 1
    r0 = s.rows[0]
    assert r0.model_cb.currentText() == AL
    assert r0.port_cb.count() == 3          # placeholder + COM3 + COM5
    assert r0.port_cb.isEnabled()

    # 2. demo wyłącza combo portu; powrót do realnego włącza
    r0.model_cb.setCurrentText(DEMO)
    assert not r0.port_cb.isEnabled()
    r0.model_cb.setCurrentText(AL)
    assert r0.port_cb.isEnabled()

    # 3. realny model bez portu -> błąd, brak emita, okno żyje
    s._emit()
    assert errs and not got and s.result() == 0
    errs.clear()

    # 4. zdublowany port -> błąd
    r0.port_cb.setCurrentIndex(1)            # COM3
    r1 = s._add_row()
    r1.port_cb.setCurrentIndex(1)            # też COM3
    s._emit()
    assert errs and not got and s.result() == 0
    errs.clear()

    # 5. poprawna konfiguracja -> emit + zapis ustawień + accept
    r1.port_cb.setCurrentIndex(2)            # COM5
    s._emit()
    assert not errs
    assert got == [[("COM3", AL), ("COM5", AL)]]
    assert st.d["rows"] == [["COM3", AL], ["COM5", AL]]
    assert s.result() == 1

    # 6. odtworzenie z ustawień po restarcie "aplikacji"
    s2 = SelectorWindow(scan_source=src, settings=st,
                        error_reporter=lambda m: None)
    assert len(s2.rows) == 2
    assert s2.rows[0].port_cb.currentData() == "COM3"
    assert s2.rows[1].port_cb.currentData() == "COM5"
    assert s2.rows[1].model_cb.currentText() == AL

    # 7. demo w konfiguracji: port None w emicie, brak wymogu portu
    st3 = FakeSettings()
    got.clear()
    s3 = SelectorWindow(scan_source=src, settings=st3,
                        error_reporter=errs.append)
    s3.session_requested.connect(got.append)
    s3.rows[0].port_cb.setCurrentIndex(1)    # AL na COM3
    rd = s3._add_row()
    rd.model_cb.setCurrentText(DEMO)
    assert not rd.port_cb.isEnabled()
    s3._emit()
    assert not errs
    assert got == [[("COM3", AL), (None, DEMO)]]

    # 8. wszystkie wiersze usunięte -> start blokowany
    s4 = SelectorWindow(scan_source=src, settings=FakeSettings(),
                        error_reporter=errs.append)
    errs.clear()
    for row in list(s4.rows):
        s4._remove_row(row)
    assert s4.rows == []
    s4._emit()
    assert errs and s4.result() == 0
    errs.clear()

    # 9. refresh: nowe porty dochodzą, wybór zachowany po danych
    s5 = SelectorWindow(scan_source=src, settings=FakeSettings(),
                        error_reporter=errs.append)
    s5.rows[0].port_cb.setCurrentIndex(1)    # COM3
    fake_ports.append(FakePort("COM7", "FTDI", vid=0x0403, pid=0x6001,
                               sn="B2"))
    s5.refresh_ports()
    assert s5.rows[0].port_cb.count() == 4    # placeholder + 3 porty
    assert s5.rows[0].port_cb.currentData() == "COM3"

    print("selector_window: OK")