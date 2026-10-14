from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QPushButton, QLabel, QInputDialog, QGridLayout, QComboBox,
)
from PyQt6.QtCore import pyqtSignal

from core.pump.registry import REGISTRY


# ui/selector_window.py (szkic)
class SelectorWindow(QDialog):
    session_requested = pyqtSignal(list)  # [(port, model_key), ...]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Konfiguracja stanowiska")
        self.rows = []
        lay = QVBoxLayout(self)
        self.grid = QGridLayout()
        lay.addLayout(self.grid)
        add_btn = QPushButton("+ Dodaj pompę")
        add_btn.clicked.connect(lambda: self._add_row())
        lay.addWidget(add_btn)
        start_btn = QPushButton("Uruchom")
        start_btn.clicked.connect(self._emit)
        lay.addWidget(start_btn)
        self._add_row()   # pierwsza pompa domyślnie: AL-1000

    def _add_row(self):
        port_cb = QComboBox(); port_cb.addItems(scan_port_names())
        model_cb = QComboBox(); model_cb.addItems(REGISTRY.keys())
        rm = QPushButton("–")
        row = len(self.rows)
        rm.clicked.connect(lambda _, r=row: self._del_row(r))
        self.grid.addWidget(port_cb, row, 0)
        self.grid.addWidget(model_cb, row, 1)
        self.grid.addWidget(rm, row, 2)
        self.rows.append((port_cb, model_cb))

    def _emit(self):
        cfg = [(p.currentText(), m.currentText()) for p, m in self.rows]
        self.session_requested.emit(cfg)
        self.accept()