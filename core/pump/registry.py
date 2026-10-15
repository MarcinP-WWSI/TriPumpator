"""Rejestr modeli pomp: nazwa w GUI -> klasa drivera.

Dodanie modelu B/C = nowy plik w core/pump/models/ + jeden wpis tutaj.
SelectorWindow buduje listę modeli z kluczy REGISTRY (kolejność
wstawienia = kolejność na liście w GUI).
"""

from __future__ import annotations

from core.pump.base import BasePumpDriver
from core.pump.demo import DemoDriver
from core.pump.models.al1000 import AL1000Driver
from core.pump.models.p11elite import P11EliteDriver

REGISTRY: dict[str, type[BasePumpDriver]] = {
    "WPI AL-1000": AL1000Driver,
    "Harvard Pump 11 Elite": P11EliteDriver,
    "Pompa demo (symulator)": DemoDriver,
}


def model_keys() -> list[str]:
    """Klucze modeli do combo w oknie wyboru."""
    return list(REGISTRY)


if __name__ == "__main__":
    # struktura rejestrów: klasy poprawne, nazwy capabilities unikalne
    names = [cls.capabilities.name for cls in REGISTRY.values()]
    assert len(names) == len(set(names)), "zdublowana capabilities.name"
    for key, cls in REGISTRY.items():
        assert issubclass(cls, BasePumpDriver), key
        assert cls.capabilities.name, f"{key}: pusta nazwa"
        assert cls.capabilities.rate_units, f"{key}: brak rate_units"

    # conf transportu: AL-1000 pełny (fabryczne 19200/8N1/ETX), demo brak
    conf = AL1000Driver.TRANSPORT_CONF
    conf11 = P11EliteDriver.TRANSPORT_CONF
    assert conf11 and conf11["terminator"] == b"\r"
    assert conf and conf["baudrate"] == 19200
    assert conf["terminator"] == b"\x03"
    assert DemoDriver.TRANSPORT_CONF is None        # dziedziczy None z base

    # fail-fast fabryki — rzuca PRZED startem wątku, więc test jest
    # bezpieczny (żaden wątek nie zostaje osierocony)
    from core.pump.worker import make_pump
    for label, bad in (
        ("realny model bez portu", lambda: make_pump(None, "WPI AL-1000")),
        ("demo z portem", lambda: make_pump("COM3", "Pompa demo (symulator)")),
    ):
        try:
            bad()
            raise AssertionError(f"powinno rzucić: {label}")
        except RuntimeError:
            pass

    print("registry: OK")