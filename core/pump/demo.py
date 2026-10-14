"""Pompa demo — generyczny symulator do developmentu bez sprzętu.

Odtwarza zachowania wspólne dla realnych pomp (na wzór AL-1000):
liczniki I/W osobno, auto-stop po zadanym wolumenie, purge
maksymalną prędkością, zerowanie liczników przy zmianie średnicy,
jednorazowe alarmy do testów GUI.

Zegar wstrzykiwalny (clock=...) — testy przesuwają czas ręcznie.
"""

from __future__ import annotations

import time

from core.pump.base import (
    BasePumpDriver, Op, PumpCapabilities, PumpStatus, norm_unit,
)


class DemoDriver(BasePumpDriver):
    capabilities = PumpCapabilities(
        name="Pompa demo",
        rate_units=("mL/h", "mL/min"),
        rate_min=0.01,
        rate_max=500.0,
        directions=("INF", "WDR"),
        volume_units=("mL",),
        target_volume=True,
        dispensed_query=True,
        purge=True,
        diameter_setting=True,
        set_rate_while_running=True,
    )

    def __init__(self, transport=None, clock=time.monotonic):
        super().__init__(transport)
        self._clock = clock
        self.rate, self.rate_unit = 100.0, "mL/h"
        self.running, self.purging = False, False
        self.direction = "INF"
        self.target_volume: float | None = None
        self.diameter: float | None = None
        self.disp: dict[str, float] = {"INF": 0.0, "WDR": 0.0}
        self._pending_alarm: str | None = None
        self._t = clock()

    # ------------------------------------------------------------- narzędzia

    def inject_alarm(self, code: str) -> None:
        """Podrzuć alarm — pokaże się w następnej odpowiedzi (testy GUI)."""
        self._pending_alarm = code

    # ------------------------------------------------------------ cykl czasu

    def _tick(self) -> None:
        now = self._clock()
        dt = now - self._t
        self._t = now
        if self.running and dt > 0:
            rate = self.capabilities.rate_max if self.purging else self.rate
            if self.rate_unit.endswith("/min"):
                rate *= 60.0                      # do mL/h
            self.disp[self.direction] += rate * dt / 3600.0
            if (self.target_volume is not None
                    and self.disp[self.direction] >= self.target_volume):
                self.running, self.purging = False, False

    def _status(self, **over) -> PumpStatus:
        st = PumpStatus(
            running=self.running,
            direction=self.direction,
            rate=self.rate,
            rate_unit=norm_unit(self.rate_unit),
            dispensed=self.disp[self.direction],
            dispensed_unit="mL",
            alarm=self._pending_alarm,
        )
        self._pending_alarm = None
        for k, v in over.items():
            setattr(st, k, v)
        return st

    # ------------------------------------------------------------- API drivera

    def query_version(self) -> str:
        return "DEMO-1.0"

    def execute(self, op: Op, **kw) -> PumpStatus:
        self._tick()
        match op:
            case Op.START:
                self.running = True
            case Op.STOP:
                self.running, self.purging = False, False
            case Op.PURGE:
                self.running, self.purging = True, True
            case Op.SET_RATE:
                unit = norm_unit(kw.get("unit", self.rate_unit))
                if unit not in self.capabilities.rate_units:
                    raise ValueError(f"nieobsługiwana jednostka: {unit}")
                self.rate, self.rate_unit = float(kw["value"]), unit
            case Op.SET_DIRECTION:
                if kw["direction"] not in self.capabilities.directions:
                    raise ValueError(f"kierunek: {kw['direction']}")
                self.direction = kw["direction"]
            case Op.SET_TARGET_VOLUME:
                self.target_volume = float(kw["volume"]) or None
            case Op.SET_DIAMETER:
                self.diameter = float(kw["mm"])
                self.disp = {"INF": 0.0, "WDR": 0.0}   # jak AL-1000
            case Op.CLEAR_DISPENSED:
                self.disp = {"INF": 0.0, "WDR": 0.0}
            case Op.POLL:
                pass
            case _:
                raise ValueError(op)
        return self._status()

    def poll(self) -> PumpStatus | None:
        return self.execute(Op.POLL)


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    class FakeClock:
        def __init__(self):      self.t = 1000.0
        def __call__(self) -> float: return self.t
        def advance(self, hours: float): self.t += hours * 3600.0

    clk = FakeClock()
    d = DemoDriver(clock=clk)

    # 1. identyfikacja
    assert d.query_version() == "DEMO-1.0"

    # 2. infuzja: 120 mL/h przez 0.5 h -> 60 mL
    d.execute(Op.SET_RATE, value=120, unit="mL/h")
    d.execute(Op.START)
    clk.advance(0.5)
    st = d.poll()
    assert st.running and st.direction == "INF"
    assert abs(st.dispensed - 60.0) < 1e-6 and st.dispensed_unit == "mL"

    # 3. stop wstrzymuje akumulację; 60 mL/h przez 1 h -> +60
    d.execute(Op.STOP)
    clk.advance(1.0)
    assert d.poll().dispensed == 60.0
    d.execute(Op.SET_RATE, value=60, unit="mL/h")
    d.execute(Op.START)
    clk.advance(1.0)
    assert abs(d.poll().dispensed - 120.0) < 1e-6

    # 4. WDR: osobny licznik; POLL pokazuje licznik bieżącego kierunku
    d.execute(Op.STOP)
    d.execute(Op.SET_DIRECTION, direction="WDR")
    d.execute(Op.START)
    clk.advance(1.0)
    st = d.poll()
    assert abs(st.dispensed - 60.0) < 1e-6          # WDR: 60
    d.execute(Op.SET_DIRECTION, direction="INF")
    assert d.poll().dispensed == 120.0              # INF nietknięty

    # 5. CLEAR zeruje oba liczniki
    d.execute(Op.CLEAR_DISPENSED)
    assert d.poll().dispensed == 0.0
    d.execute(Op.SET_DIRECTION, direction="WDR")
    assert d.poll().dispensed == 0.0

    # 6. target volume: 100 mL/h, target 10 mL -> auto-stop po 0.1 h
    d.execute(Op.STOP)
    d.execute(Op.SET_DIRECTION, direction="INF")
    d.execute(Op.SET_RATE, value=100, unit="mL/h")
    d.execute(Op.SET_TARGET_VOLUME, volume=10)
    d.execute(Op.START)
    clk.advance(0.05)
    assert d.poll().running                         # 5 mL < 10
    clk.advance(0.05)
    st = d.poll()
    assert not st.running and abs(st.dispensed - 10.0) < 1e-6

    # 7. purge: maksymalna prędkość (500 mL/h); STP kończy
    d.execute(Op.CLEAR_DISPENSED)
    d.execute(Op.PURGE)
    clk.advance(1.0)
    assert abs(d.poll().dispensed - 500.0) < 1e-6
    d.execute(Op.STP if hasattr(Op, "STP") else Op.STOP)
    d.execute(Op.CLEAR_DISPENSED)
    clk.advance(1.0)
    assert d.poll().dispensed == 0.0

    # 8. zmiana rate w biegu działa od następnego ticka
    d.execute(Op.SET_RATE, value=200, unit="mL/h")
    d.execute(Op.START)
    clk.advance(1.0)
    assert abs(d.poll().dispensed - 200.0) < 1e-6

    # 9. SET_DIAMETER zeruje liczniki (jak AL-1000)
    d.execute(Op.SET_DIAMETER, mm=20.0)
    assert d.poll().dispensed == 0.0 and d.diameter == 20.0

    # 10. alarm: pokazany raz, kolejny POLL go czyści
    d.inject_alarm("S")
    assert d.poll().alarm == "S"
    assert d.poll().alarm is None

    # 11. walidacja domyślna z capabilities (statyczna)
    assert d.validate_rate(120.0, "mL/h") is None
    assert d.validate_rate(700.0, "mL/h") is not None   # > 500
    assert d.validate_rate(2.0, "mL/min") is None       # 120 mL/h
    assert d.validate_rate(1.0, "L/h") is not None      # nieznana

    print("demo: OK")