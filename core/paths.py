"""Gdzie mieszka aplikacja — zgodnie dla 'python -m' i PyInstaller.

PyInstaller rozpakowuje do tymczasowego _MEIPASS; __file__ wskazuje
tam, NIE do katalogu exe. Dane użytkownika (ustawienia, procedury)
muszą leżeć obok exe (portable) — stąd app_dir().
"""
from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    """Katalog aplikacji: obok .exe w buildzie, root projektu w dev."""
    if getattr(sys, "frozen", False):          # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent