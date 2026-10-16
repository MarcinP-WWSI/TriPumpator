"""Pompa demo — generyczny symulator do developmentu bez sprzętu.

Odtwarza zachowania wspólne dla realnych pomp (na wzór AL-1000 i
Pump 11 Elite): liczniki I/W osobno, auto-stop po zadanym wolumenie
(mL/µL) LUB czasie biegu (sekundy), purge maksymalną prędkością,
zerowanie liczników przy zmianie średnicy, jednorazowe alarmy do
testów GUI. Force to pasywny procent (round-trip ustawienia).

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
        rate_units=("mL/h", "mL/min", "µL/h", "µL/min"),
        rate_min=0.01,
        rate_max=500.0,
        directions=("INF", "WDR"),
        volume_units=("mL", "µL"),
        target_volume=True,
        target_time=True,
        purge=True,
        diameter_setting=True,
        force_setting=True,
        set_rate_while_running=True,
    )

    def __init__(self, transport=None, clock=time.monotonic):
        super().__init__(transport)
        self._clock = clock
        self.rate, self.rate_unit = 100.0, "mL/h"
        self.running, self.purging = False, False
        self.direction = "INF"
        self.target_volume: float | None = None   # wartość w target_unit
        self.target_unit = "mL"
        self.target_time: float | None = None     # sekundy
        self.force: float | None = None           # procent
        self.diameter: float | None = None
        self.disp: dict[str, float] = {"INF": 0.0, "WDR": 0.0}   # w mL
        self._run_seconds = 0.0                   # czas biegu (pauza mrozi)
        self._pending_alarm: str | None = None
        self._t = clock()

    # ------------------------------------------------------------- narzędzia

    def inject_alarm(self, code: str) -> None:
        """Podrzuć alarm — pokaże się w następnej odpowiedzi (testy GUI)."""
        self._pending_alarm = code

    # ------------------------------------------------------------ cykl czasu

    def _rate_ml_per_h(self) -> float:
        """Rate w mL/h (liczniki trzymamy w mL — wspólny mianownik)."""
        rate = self.capabilities.rate_max if self.purging else self.rate
        u = norm_unit(self.rate_unit)
        if "µL" in u:
            rate /= 1000.0
        if u.endswith("/min"):
            rate *= 60.0
        return rate

    def _target_ml(self) -> float | None:
        if self.target_volume is None:
            return None
        return self.target_volume / 1000.0 \
            if self.target_unit == "µL" else self.target_volume

    def _tick(self) -> None:
        now = self._clock()
        dt = now - self._t
        self._t = now
        if self.running and dt > 0:
            self._run_seconds += dt
            self.disp[self.direction] += \
                self._rate_ml_per_h() * dt / 3600.0
            t_ml = self._target_ml()
            hit_vol = (t_ml is not None
                       and self.disp[self.direction] >= t_ml)
            hit_time = (self.target_time is not None
                        and self._run_seconds >= self.target_time)
            if hit_vol or hit_time:
                self.running, self.purging = False, False

    def _status(self) -> PumpStatus:
        st = PumpStatus(
            running=self.running,
            direction=self.direction,
            rate=self.rate,
            rate_unit=norm_unit(self.rate_unit),
            target_volume=self.target_volume,
            target_volume_unit=self.target_unit,
            target_time=self.target_time,
            force=self.force,
            diameter=self.diameter,
            dispensed=self.disp[self.direction],
            dispensed_unit="mL",
            alarm=self._pending_alarm,
        )
        self._pending_alarm = None
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
                unit = norm_unit(kw.get("unit", self.target_unit))
                if unit not in self.capabilities.volume_units:
                    raise ValueError(f"jednostka objętości: {unit}")
                v = float(kw["volume"])
                self.target_volume = v if v > 0 else None  # 0 = ciągła
                self.target_unit = unit
            case Op.SET_TARGET_TIME:
                secs = float(kw["seconds"])
                if secs < 0:
                    raise ValueError(f"czas ujemny: {secs}")
                self.target_time = secs if secs > 0 else None
                self._run_seconds = 0.0
            case Op.SET_FORCE:
                pct = float(kw["percent"])
                lo, hi = self.capabilities.force_range
                if not lo <= pct <= hi:
                    raise ValueError(f"siła poza zakresem {lo}-{hi}%")
                self.force = pct
            case Op.SET_DIAMETER:
                self.diameter = float(kw["mm"])
                self.disp = {"INF": 0.0, "WDR": 0.0}   # jak realne pompy
            case Op.CLEAR_DISPENSED:
                self.disp = {"INF": 0.0, "WDR": 0.0}
                self._run_seconds = 0.0
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
        def advance_s(self, seconds: float): self.t += seconds

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

    # 5. CLEAR zeruje oba liczniki i czas biegu
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

    # 6b. target time (izolowany): auto-stop po 30 s; pauza mrozi odliczanie
    clk_b = FakeClock();
    db = DemoDriver(clock=clk_b)
    db.execute(Op.SET_TARGET_TIME, seconds=30)
    st = db.execute(Op.POLL)
    assert st.target_time == 30.0
    db.execute(Op.START)
    clk_b.advance_s(20)
    assert db.poll().running  # 20 s < 30 s
    db.execute(Op.STOP)
    clk_b.advance_s(100)  # pauza: zegar biegu stoi
    db.execute(Op.START)
    clk_b.advance_s(11)  # 20+11=31 >= 30
    st = db.poll()
    assert not st.running and st.target_time == 30.0

    # 6c. clear kasuje czas biegu (izolowany): okno liczy od zera
    clk_c = FakeClock();
    dc = DemoDriver(clock=clk_c)
    dc.execute(Op.SET_TARGET_TIME, seconds=30)
    dc.execute(Op.START)
    clk_c.advance_s(30)
    assert not dc.poll().running  # 30 s = koniec
    dc.execute(Op.CLEAR_DISPENSED)  # reset okna czasu
    dc.execute(Op.START)
    clk_c.advance_s(10)
    assert dc.poll().running  # od zera: 10 < 30
    clk_c.advance_s(20)
    assert not dc.poll().running  # znowu 30 s

    # 6d. target time = 0 -> praca ciągła (izolowany)
    clk_d = FakeClock();
    dd = DemoDriver(clock=clk_d)
    dd.execute(Op.SET_TARGET_TIME, seconds=0)
    assert dd.poll().target_time is None
    dd.execute(Op.START)
    clk_d.advance_s(120)
    assert dd.poll().running

    # 6e. oba targety: pierwsze osiągnięte wygrywa (izolowany)
    clk_e = FakeClock();
    de = DemoDriver(clock=clk_e)
    de.execute(Op.SET_RATE, value=3600, unit="mL/h")  # 1 mL/s
    de.execute(Op.SET_TARGET_VOLUME, volume=100)
    de.execute(Op.SET_TARGET_TIME, seconds=10)  # czas wygra (10 s)
    de.execute(Op.START)
    clk_e.advance_s(10)
    st = de.poll()
    assert not st.running and abs(st.dispensed - 10.0) < 1e-6  # czas wygrał
    de.execute(Op.CLEAR_DISPENSED)
    de.execute(Op.SET_TARGET_VOLUME, volume=5)  # teraz volume (5 s)
    de.execute(Op.SET_TARGET_TIME, seconds=50)
    de.execute(Op.START)
    clk_e.advance_s(5)
    st = de.poll()
    assert not st.running and abs(st.dispensed - 5.0) < 1e-6  # volume wygrał

    # 7. purge: maksymalna prędkość (500 mL/h); STOP kończy
    d.execute(Op.STOP)
    d.execute(Op.SET_TARGET_VOLUME, volume=0)
    d.execute(Op.SET_TARGET_TIME, seconds=0)
    d.execute(Op.CLEAR_DISPENSED)
    d.execute(Op.PURGE)
    clk.advance(1.0)
    assert abs(d.poll().dispensed - 500.0) < 1e-6
    d.execute(Op.STOP)
    d.execute(Op.CLEAR_DISPENSED)
    clk.advance(1.0)
    assert d.poll().dispensed == 0.0

    # 8. zmiana rate w biegu działa od następnego ticka
    d.execute(Op.SET_RATE, value=200, unit="mL/h")
    d.execute(Op.START)
    clk.advance(1.0)
    assert abs(d.poll().dispensed - 200.0) < 1e-6

    # 9. SET_DIAMETER zeruje liczniki (jak realne pompy)
    d.execute(Op.SET_DIAMETER, mm=20.0)
    assert d.poll().dispensed == 0.0 and d.diameter == 20.0

    # 9b. force: set/odczyt, walidacja, NIE kasuje się przy średnicy
    d.execute(Op.SET_FORCE, percent=45)
    st = d.poll()
    assert st.force == 45.0
    for bad in (0, 101):
        try:
            d.execute(Op.SET_FORCE, percent=bad)
            raise AssertionError(f"powinno rzucić: {bad}")
        except ValueError:
            pass
    d.execute(Op.SET_DIAMETER, mm=14.43)
    assert d.poll().force == 45.0 and d.diameter == 14.43

    # 10. alarm: pokazany raz, kolejny POLL go czyści
    d.inject_alarm("S")
    assert d.poll().alarm == "S"
    assert d.poll().alarm is None

    # 11. walidacja rate (regresja)
    assert d.validate_rate(120.0, "mL/h") is None
    assert d.validate_rate(700.0, "mL/h") is not None
    assert d.validate_rate(2.0, "mL/min") is None
    assert d.validate_rate(1.0, "L/h") is not None
    assert d.validate_rate(120000.0, "µL/h") is None

    # 12. µL: rate i target w mikrolitrach (regresja)
    d.execute(Op.STOP)
    d.execute(Op.CLEAR_DISPENSED)
    d.execute(Op.SET_DIRECTION, direction="INF")
    d.execute(Op.SET_RATE, value=120000, unit="µL/h")
    d.execute(Op.START)
    clk.advance(0.5)
    st = d.poll()
    assert st.rate == 120000 and st.rate_unit == "µL/h"
    assert abs(st.dispensed - 60.0) < 1e-6
    d.execute(Op.STOP)
    d.execute(Op.CLEAR_DISPENSED)
    d.execute(Op.SET_TARGET_VOLUME, volume=500, unit="µL")
    st = d.execute(Op.POLL)
    assert st.target_volume == 500 and st.target_volume_unit == "µL"

    print("demo: OK")