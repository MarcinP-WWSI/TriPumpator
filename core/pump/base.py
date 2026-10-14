from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto


class Op(Enum):
    """Wspólny zestaw operacji. GUI emituje WYŁĄCZNIE te operacje."""
    START = auto()
    STOP = auto()
    PURGE = auto()
    SET_RATE = auto()            # value, unit
    SET_DIRECTION = auto()       # direction: "INF" | "WDR"
    SET_TARGET_VOLUME = auto()   # volume
    SET_DIAMETER = auto()        # mm
    CLEAR_DISPENSED = auto()
    POLL = auto()                # status + objętość


@dataclass
class PumpStatus:
    running: bool | None = None
    direction: str | None = None      # "INF" / "WDR"
    rate: float | None = None
    rate_unit: str | None = None
    dispensed: float | None = None
    alarm: str | None = None
    raw: str = ""


@dataclass
class PumpCapabilities:
    """Z czego GUI buduje okno pompy. Model deklaruje, co potrafi."""
    name: str = "pompa"
    rate_units: tuple[str, ...] = ("mL/h",)
    directions: tuple[str, ...] = ("INF",)
    target_volume: bool = False
    dispensed_query: bool = False
    purge: bool = False
    diameter_setting: bool = False
    set_rate_while_running: bool = False


class BasePumpDriver(ABC):
    """Jeden model pompy = jedna podklasa. Reszta aplikacji jest ślepa na protokół."""
    capabilities = PumpCapabilities()

    def __init__(self, transport):
        self.transport = transport

    @abstractmethod
    def query_version(self) -> str:
        """Weryfikacja łączności przy starcie (identyfikacja pompy)."""

    @abstractmethod
    def execute(self, op: Op, **kw) -> PumpStatus:
        """Wykonaj operację, zwróć status. Rzuca wyjątek przy błędzie."""

    @abstractmethod
    def poll(self) -> PumpStatus | None:
        """Odpytaj status. None = brak odpowiedzi / nic nowego."""