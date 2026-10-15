"""Wspólny język pomp: operacje, status, capabilities, baza driverów.

GUI emituje WYŁĄCZNIE Op. Driver tłumaczy Op na protokół swojego modelu.
GUI buduje okno pompy WYŁĄCZNIE z PumpCapabilities. Nic poza driverem
modelu nie wie, jak wygląda ramka na przewodzie.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto


# ---------------------------------------------------------------- jednostki

# micro sign U+00B5 i greckie mu U+03BC wyglądają identycznie, ale to
# różne znaki — normalizujemy WSZYSTKIE jednostki przez tę funkcję,
# zanim cokolwiek trafi do lookupu/słownika/formatowania.
_UNIT_FIX = str.maketrans({"μ": "µ"})

RATE_UNITS = ("µL/min", "mL/min", "µL/h", "mL/h")
VOLUME_UNITS = ("µL", "mL")

def to_ml(value: float, unit: str | None) -> float:
    """Objętość w 'mL'/'µL' -> mililitry."""
    if unit is None:
        return value
    return value / 1000.0 if norm_unit(unit) == "µL" else value


def fmt_volume_ml(ml: float) -> str:
    """Auto-skalowana objętość: >= 0.1 mL -> mL, poniżej -> µL.

    Przy rate'ach rzędu µL/min licznik w mL długo stoi na 0.000 —
    jednostka dostosowuje się do wielkości, nie do średnicy pompy.
    """
    if abs(ml) >= 0.1:
        return f"{ml:.3f} mL"
    return f"{ml * 1000.0:.3f} µL"


def fmt_dispensed(value: float, unit: str | None) -> str:
    """Formatuj 'Dozowane' niezależnie od jednostek raportowanych przez
    pompę (AL-1000 raportuje mL albo µL zależnie od średnicy strzykawki)."""
    return fmt_volume_ml(to_ml(value, unit))

def norm_unit(unit: str) -> str:
    return unit.translate(_UNIT_FIX).strip()

class PumpCommandError(Exception):
    """Pompa odrzuciła komendę (błąd ?NA/?OOR/?COM albo wartość
    niesformatowalna w protokole modelu)."""

class Op(Enum):
    """Wspólny zestaw operacji. GUI emituje WYŁĄCZNIE te operacje."""
    START = auto()
    STOP = auto()
    PURGE = auto()
    SET_RATE = auto()            # value: float, unit: str
    SET_DIRECTION = auto()       # direction: "INF" | "WDR"
    SET_TARGET_VOLUME = auto()   # volume: float
    SET_DIAMETER = auto()        # mm: float
    CLEAR_DISPENSED = auto()
    POLL = auto()                # status + objętość


@dataclass
class PumpStatus:
    running: bool | None = None
    direction: str | None = None      # "INF" / "WDR"
    rate: float | None = None
    rate_unit: str | None = None      # znormalizowana (norm_unit)
    target_volume: float | None = None       # wartość ustawiona na pompie
    target_volume_unit: str | None = None    # jednostki objętości pompy
    diameter: float | None = None      # mm, ostatnio ustawiona
    dispensed: float | None = None
    dispensed_unit: str | None = None # "mL" / "µL" — co raportuje pompa
    alarm: str | None = None          # kod alarmu modelu, nie tekst
    raw: str = ""


@dataclass(frozen=True)
class PumpCapabilities:
    """Z czego GUI buduje okno pompy. Model deklaruje, co potrafi.

    rate_min/rate_max są w rate_units[0] (jednostce bazowej modelu) i są
    tylko ZGROSZOWANIEM zakresu — ostateczną walidację robi
    BasePumpDriver.validate_rate, bo np. AL-1000 ma limity zależne od
    średnicy strzykawki (manual, Appendix A).
    """
    name: str = "pompa"
    rate_units: tuple[str, ...] = ("mL/h",)
    rate_min: float = 0.0
    rate_max: float = float("inf")
    directions: tuple[str, ...] = ("INF",)
    volume_units: tuple[str, ...] = ("mL",)
    target_volume: bool = False
    dispensed_query: bool = False
    purge: bool = False
    diameter_setting: bool = False
    diameter_range: tuple[float, float] = (0.1, 50.0)  # mm, jak AL-1000
    set_rate_while_running: bool = False


class BasePumpDriver(ABC):
    """Jeden model pompy = jedna podklasa.

    Cykl życia: tworzony w wątku roboczym, wołany wyłącznie z niego
    (blokujące I/O). Metody mogą rzucać — worker łapie i raportuje.
    """
    capabilities = PumpCapabilities()
    TRANSPORT_CONF: dict | None = None

    def __init__(self, transport):
        self.transport = transport

    # ------------------------------------------------------------ abstrakty

    @abstractmethod
    def query_version(self) -> str:
        """Identyfikacja przy starcie; rzuca przy braku łączności."""

    @abstractmethod
    def execute(self, op: Op, **kw) -> PumpStatus:
        """Wykonaj operację, zwróć status. Rzuca przy błędzie."""

    @abstractmethod
    def poll(self) -> PumpStatus | None:
        """Odpytaj status. None = brak odpowiedzi (cisza na porcie)."""

    # ------------------------------------------------- walidacja (nadpisywalna)

    def validate_rate(self, value: float, unit: str) -> str | None:
        """Zwróć komunikat błędu, gdy rate nieakceptowalny; None = OK.

        Domyślnie: statyczny zakres z capabilities (w jednostce bazowej).
        Modele z limitami zależnymi od konfiguracji (AL-1000: średnica)
        nadpisują tę metodę.
        """
        unit = norm_unit(unit)
        if unit not in self.capabilities.rate_units:
            return f"nieobsługiwana jednostka: {unit}"
        base = self.capabilities.rate_units[0]
        v = self.to_base_unit(value, unit, base)
        if v is None:
            return None  # nie umiemy przeliczyć — przepuść, pompa rozstrzygnie
        if not (self.capabilities.rate_min <= v <= self.capabilities.rate_max):
            return (f"rate {value:g} {unit} poza zakresem "
                    f"{self.capabilities.rate_min:g}–"
                    f"{self.capabilities.rate_max:g} {base}")
        return None

    # ------------------------------------------------------------- pomocnicze

    @staticmethod
    def to_base_unit(value: float, unit: str, target: str) -> float | None:
        """Przelicz rate między jednostkami; None gdy nie da się."""
        unit, target = norm_unit(unit), norm_unit(target)
        if unit == target:
            return value
        per_min = unit.endswith("/min")
        tpm = target.endswith("/min")
        if per_min != tpm:
            if tpm:                    # źródło /h  -> cel /min
                value /= 60.0
            else:                      # źródło /min -> cel /h
                value *= 60.0
        u = "µL" if "µL" in unit else "mL"
        t = "µL" if "µL" in target else "mL"
        if u != t:
            if t == "µL":              # mL -> µL
                value *= 1000.0
            else:                      # µL -> mL
                value /= 1000.0
        return value


if __name__ == "__main__":
    # --- jednostki
    assert norm_unit("μL/h") == "µL/h"          # μ (U+03BC) -> µ (U+00B5)
    assert norm_unit(" mL/h ") == "mL/h"

    # --- przeliczenia (każda asercja prześledzona ręcznie)
    assert BasePumpDriver.to_base_unit(2.0, "mL/min", "mL/h") == 120.0
    assert BasePumpDriver.to_base_unit(120.0, "mL/h", "mL/min") == 2.0
    assert BasePumpDriver.to_base_unit(1.0, "µL/h", "mL/h") == 0.001
    assert BasePumpDriver.to_base_unit(1.0, "mL/h", "mL/h") == 1.0

    class _T(BasePumpDriver):
        capabilities = PumpCapabilities(name="t", rate_units=("mL/h",),
                                        rate_min=0.01, rate_max=500.0)
        def query_version(self): return "x"
        def execute(self, op, **kw): ...
        def poll(self): ...

    d = _T(transport=None)
    # model znający tylko mL/h:
    assert d.validate_rate(250.0, "mL/h") is None
    assert d.validate_rate(600.0, "mL/h") is not None
    assert "jednostka" in d.validate_rate(1.0, "L/h")
    assert d.validate_rate(2.0, "mL/min") is not None   # nieznana jednostka

    class _T2(_T):
        capabilities = PumpCapabilities(name="t2",
                                        rate_units=("mL/h", "mL/min"),
                                        rate_min=0.01, rate_max=500.0)
    d2 = _T2(transport=None)
    # model znający mL/h i mL/min — tu działa ścieżka przeliczenia:
    assert d2.validate_rate(2.0, "mL/min") is None       # -> 120 mL/h
    assert d2.validate_rate(20.0, "mL/min") is not None  # -> 1200 mL/h

    # --- formatowanie objętości
    assert to_ml(500.0, "µL") == 0.5
    assert to_ml(0.5, "mL") == 0.5 and to_ml(0.5, None) == 0.5
    assert fmt_volume_ml(1.5) == "1.500 mL"
    assert fmt_volume_ml(0.1) == "0.100 mL"
    assert fmt_volume_ml(0.0999) == "99.900 µL"
    assert fmt_volume_ml(0.0004) == "0.400 µL"
    assert fmt_dispensed(50.0, "µL") == "50.000 µL"
    assert fmt_dispensed(1.25, "mL") == "1.250 mL"

    print("base: OK")