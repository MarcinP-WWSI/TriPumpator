"""Driver Harvard Apparatus Pump 11 Elite (manual 5420-002 Rev 1.0).

Łącze: natywny USB Type B (wirtualny COM, sterownik Harvard USB-CDC
z dołączonej płyty) — adres pompy 0, bez prefiksów. Protokół linii
z promptami (patrz core/transport/protocol_p11.py); transport wołany
z until=response_complete zamiast terminatora.

Ograniczenia modelu odwzorowane w capabilities (GUI dostosowuje się
samo): brak komendy purge (przycisk znika), tvolume w mL/µL (pompa
zna też nL/pl — v1 ich nie oferuje). GUI ma JEDNO pole rate, a pompa
dwie stawki (irate/wrate) — driver ustawia obie tą samą wartością.

UWAGA (manual str. 51): komendy parametrów działają z ekranu Quick
Start pompy; dotknięcie touch screena wstrzymuje sesję do wysłania
<CR> — kolejna nasza komenda to naprawia (każda ramka ma <CR>).
"""

from __future__ import annotations

import re

from core.pump.base import (
    BasePumpDriver, Op, PumpCapabilities, PumpCommandError, PumpStatus,
    norm_unit,
)
from core.transport.protocol_p11 import (
    GUI_RATE_TO_P11, GUI_VOL_TO_P11, VOL_LETTER_TO_GUI, parse_rate,
    parse_status_line, prompt_of, response_complete, split_lines,
    strip_addr_data,
)

FL_TO_ML = 1e-12          # 1 mL = 1e12 fL (status raportuje femtolitry)
FL_S_TO_ML_H = 3.6e-9    # fL/s -> mL/h


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s else "0"


class P11EliteDriver(BasePumpDriver):
    capabilities = PumpCapabilities(
        name="Harvard Pump 11 Elite",
        rate_units=("mL/h", "mL/min", "µL/h", "µL/min"),
        rate_min=0.0,
        rate_max=float("inf"),      # limity z 'irate lim' — zależne od strzykawki
        directions=("INF", "WDR"),
        volume_units=("mL", "µL"),
        target_volume=True,
        dispensed_query=True,
        purge=False,                # protokół nie ma odpowiednika purge
        diameter_setting=True,
        diameter_range=(0.103, 50.0),
        set_rate_while_running=True,
    )

    # USB-CDC: baudrate bez znaczenia; dla opcji RJ-11 wg pompy
    TRANSPORT_CONF = {
        "baudrate": 19200,
        "bytesize": 8,
        "parity": "N",
        "stopbits": 1,
        "terminator": b"\r",        # nieużywane: driver woła z until=
    }

    def __init__(self, transport):
        super().__init__(transport)
        self._direction = "INF"
        self._running = False
        self._stalled = False
        self._diameter: float | None = None
        self._target: float | None = None
        self._target_unit: str = "mL"
        self._rate: float | None = None
        self._rate_unit: str = "mL/h"
        self._min_ml_h: float | None = None
        self._max_ml_h: float | None = None

    # ------------------------------------------------------------ rdzeń

    def _req(self, cmd: str) -> tuple[list[str], str]:
        """Komenda -> (linie danych, prompt). Błędy -> PumpCommandError."""
        data = self.transport.request(cmd.encode("ascii") + b"\r",
                                      until=response_complete)
        if data is None:
            raise TimeoutError(f"Pump 11: brak odpowiedzi na {cmd!r}")
        lines = split_lines(data)
        prompt = prompt_of(lines[-1]) if lines else None
        if prompt is None:
            raise PumpCommandError(f"odpowiedź bez promptu: {lines!r}")
        body = [strip_addr_data(l) for l in lines[:-1]]
        if body and body[0].lower() == cmd.lower():    # echo USB, gdy włączone
            body = body[1:]
        for i, line in enumerate(body):
            low = line.lower()
            if "command error" in low or "argument error" in low:
                msg = body[i + 1].strip() if i + 1 < len(body) else ""
                raise PumpCommandError(f"{cmd!r}: {line} {msg}".strip())
        return body, prompt

    def _apply_prompt(self, prompt: str) -> None:
        if prompt == ">":
            self._running, self._direction, self._stalled = True, "INF", False
        elif prompt == "<":
            self._running, self._direction, self._stalled = True, "WDR", False
        elif prompt == "*":
            self._running, self._stalled = False, True
        else:                                    # ':' idle, 'T*' target
            self._running, self._stalled = False, False

    # ------------------------------------------------------ identyfikacja

    def query_version(self) -> str:
        try:
            self._req("echo off")                 # wyłącz echo USB
        except (TimeoutError, PumpCommandError):
            pass                                  # starsze firmware — trudno
        body, _ = self._req("ver")
        if not body:
            raise PumpCommandError("brak linii wersji")
        self._sync_setup()
        return body[0]

    def _sync_setup(self) -> None:
        body, _ = self._req("diameter")
        if body and (m := re.fullmatch(r"([\d.]+)\s*mm", body[0])):
            self._diameter = float(m[1])
        self._sync_target()
        self._sync_rate()
        self._sync_limits()

    def _sync_target(self) -> None:
        body, _ = self._req("tvolume")
        if body:
            if "not set" in body[0].lower():
                self._target = None
            elif m := re.fullmatch(r"([\d.]+)\s*([munp])", body[0]):
                self._target = float(m[1])
                self._target_unit = VOL_LETTER_TO_GUI[m[2]]

    def _sync_rate(self) -> None:
        body, _ = self._req("irate")
        if body and (r := parse_rate(body[0])):
            self._rate, self._rate_unit = r["value"], "mL/h"

    def _sync_limits(self) -> None:
        """'irate lim' -> min/max rate w mL/h (zależne od strzykawki)."""
        body, _ = self._req("irate lim")
        if not body or " to " not in body[0]:
            return
        lo_s, _, hi_s = body[0].partition(" to ")
        lo, hi = parse_rate(lo_s.strip()), parse_rate(hi_s.strip())
        if lo and hi:
            self._min_ml_h, self._max_ml_h = lo["ml_per_h"], hi["ml_per_h"]

    # ------------------------------------------------------------- operacje

    def execute(self, op: Op, **kw) -> PumpStatus:
        match op:
            case Op.START:
                _, prompt = self._req("irun" if self._direction == "INF"
                                      else "wrun")
                self._apply_prompt(prompt)
            case Op.STOP:
                _, prompt = self._req("stop")
                self._apply_prompt(prompt)
            case Op.SET_RATE:
                unit = norm_unit(kw.get("unit", "mL/h"))
                if unit not in GUI_RATE_TO_P11:
                    raise PumpCommandError(f"nieznana jednostka: {unit}")
                arg = f"{_fmt(float(kw['value']))} {GUI_RATE_TO_P11[unit]}"
                self._req(f"irate {arg}")
                _, prompt = self._req(f"wrate {arg}")
                self._apply_prompt(prompt)
                self._rate, self._rate_unit = float(kw["value"]), unit
            case Op.SET_DIRECTION:
                d = kw["direction"]
                if d not in ("INF", "WDR"):
                    raise PumpCommandError(f"kierunek: {d}")
                if self._running:
                    _, prompt = self._req("rrun")     # bieg w drugą stronę
                    self._apply_prompt(prompt)
                else:
                    self._direction = d               # użyty przy START
            case Op.SET_TARGET_VOLUME:
                v = float(kw["volume"])
                if v > 0:
                    unit = norm_unit(kw.get("unit", self._target_unit))
                    if unit not in GUI_VOL_TO_P11:
                        raise PumpCommandError(f"jednostka: {unit}")
                    self._req(f"tvolume {_fmt(v)} {GUI_VOL_TO_P11[unit]}")
                    self._target, self._target_unit = v, unit
                else:
                    self._req("ctvolume")
                    self._target = None
            case Op.SET_DIAMETER:
                self._req(f"diameter {_fmt(float(kw['mm']))}")
                self._diameter = float(kw["mm"])
                # zmiana strzykawki KASUJE rate/tvolume w pompie
                # (limity zależą od strzykawki) — odśwież cache z pompy
                self._sync_rate()
                self._sync_target()
                self._sync_limits()
            case Op.CLEAR_DISPENSED:
                self._req("cvolume")
            case Op.POLL:
                return self._poll()
            case _:
                raise ValueError(op)
        return self._status()

    def _poll(self) -> PumpStatus:
        body, prompt = self._req("status")
        self._apply_prompt(prompt)
        if body and (s := parse_status_line(body[0])):
            st = PumpStatus(raw=body[0])
            st.running = s["running"]
            st.direction = s["direction"]
            if s["stall"] or prompt == "*":
                st.alarm, st.running = "S", False
            if s["running"]:
                st.rate = round(s["rate_fl_s"] * FL_S_TO_ML_H, 6)
                st.rate_unit = "mL/h"
            else:
                st.rate, st.rate_unit = self._rate, self._rate_unit
            st.dispensed = s["volume_fl"] * FL_TO_ML
            st.dispensed_unit = "mL"
        else:
            st = self._status()
        st.target_volume = self._target
        st.target_volume_unit = self._target_unit
        st.diameter = self._diameter
        return st

    def poll(self) -> PumpStatus | None:
        try:
            return self._poll()
        except TimeoutError:
            return None

    # ------------------------------------------------------------- walidacja

    def validate_rate(self, value: float, unit: str) -> str | None:
        unit = norm_unit(unit)
        if unit not in self.capabilities.rate_units:
            return f"nieobsługiwana jednostka: {unit}"
        if self._max_ml_h is None:
            return None               # limity nieznane — pompa rozstrzygnie
        v = self.to_base_unit(value, unit, "mL/h")
        if v is None:
            return None
        if not self._min_ml_h <= v <= self._max_ml_h:
            return (f"rate {value:g} {unit} poza zakresem pompy: "
                    f"{self._min_ml_h:.3g}–{self._max_ml_h:.4g} mL/h")
        return None

    def _status(self) -> PumpStatus:
        return PumpStatus(
            running=self._running,
            direction=self._direction,
            rate=self._rate,
            rate_unit=self._rate_unit,
            target_volume=self._target,
            target_volume_unit=self._target_unit,
            diameter=self._diameter,
            alarm="S" if self._stalled else None,
        )


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    class FakeP11:
        """Pump 11 do testów drivera w izolacji (until ignorowane —
        zawsze zwraca kompletną odpowiedź; parsowanie testuje protocol_p11)."""
        def __init__(self):
            self.sent: list[str] = []
            self.running = False
            self.direction = "INF"
            self.diameter = 14.43
            self.rate = (90.0, "m/h")
            self.target: tuple[float, str] | None = None
            self.disp_fl = 5e11
            self.stall = False
            self.echo = True

        def request(self, frame, terminator=None, until=None):
            cmd = frame.decode("ascii").strip()
            self.sent.append(cmd)
            lines: list[str] = []

            # 1. najpierw EFEKT komendy — jak w realnej pompie
            if cmd == "echo off":
                self.echo = False
            elif cmd == "ver":
                lines = ["11 Elite 2.0.0.0"]
            elif cmd == "diameter":
                lines = [f"{self.diameter:.4f} mm"]
            elif cmd.startswith("diameter "):
                self.diameter = float(cmd.split()[1])
            elif cmd == "irate":
                lines = [f"{self.rate[0]:g} {self.rate[1]}"]
            elif cmd == "irate lim":
                lines = ["25.05 n/min to 26.02 m/min"]
            elif cmd.startswith("irate ") and len(cmd.split()) == 3:
                parts = cmd.split()
                self.rate = (float(parts[1]), parts[2])
            elif cmd == "tvolume":
                lines = [f"{self.target[0]:g} {self.target[1]}"
                         if self.target else "Target volume not set"]
            elif cmd.startswith("tvolume "):
                _, v, u = cmd.split()
                self.target = (float(v), u)
            elif cmd == "ctvolume":
                self.target = None
            elif cmd in ("irun", "wrun"):
                self.running = True
                self.direction = "INF" if cmd == "irun" else "WDR"
            elif cmd == "rrun":
                self.direction = ("WDR" if self.direction == "INF"
                                  else "INF")
                self.running = True
            elif cmd == "stop":
                self.running = False
            elif cmd == "cvolume":
                self.disp_fl = 0
            elif cmd == "status":
                vol = 1.0 if self.rate[1].startswith("m") else 1e-3
                per_h = 3600.0 if self.rate[1].endswith("h") else 60.0
                rate_fl = int(self.rate[0] * vol * 1e12 / per_h)
                f1 = ("I" if self.running and self.direction == "INF"
                      else "W" if self.running
                else "i" if self.direction == "INF" else "w")
                # 6 flag: kierunek, limit, stall, trigger, port, target
                flags = " ".join([f1, ".", "S" if self.stall else ".",
                                  ".", f1, "."])
                lines = [f"{rate_fl} 1000 {int(self.disp_fl)} {flags}"]

            # 2. dopiero TERAZ prompt — stan PO komendzie
            if self.stall:
                prompt = "*"
            elif self.running:
                prompt = ">" if self.direction == "INF" else "<"
            else:
                prompt = ":"

            data = b"".join(b"\n" + l.encode() + b"\r" for l in lines) \
                   + b"\n" + prompt.encode() + b"\r"
            if self.echo:
                data = b"\n" + cmd.encode() + b"\r" + data
            return data

    t = FakeP11()
    d = P11EliteDriver(t)

    # 1. identyfikacja: echo off, ver + sync DIA/TVOL/IRATE/IRATE LIM
    assert d.query_version() == "11 Elite 2.0.0.0"
    assert t.sent == ["echo off", "ver", "diameter",
                      "tvolume", "irate", "irate lim"]
    assert not t.echo                          # echo wyłączone
    assert d._diameter == 14.43 and d._target is None
    assert d._rate == 90.0 and d._rate_unit == "mL/h"
    assert abs(d._min_ml_h - 1.503e-3) < 1e-6  # 25.05 n/min
    assert abs(d._max_ml_h - 1561.2) < 1e-6   # 26.02 m/min

    # 2. SET_RATE: jedna wartość GUI -> irate + wrate
    d.execute(Op.SET_RATE, value=250, unit="mL/h")
    assert t.sent[-2:] == ["irate 250 m/h", "wrate 250 m/h"]
    d.execute(Op.SET_RATE, value=120000, unit="μL/h")     # μ -> µ
    assert t.sent[-2:] == ["irate 120000 u/h", "wrate 120000 u/h"]

    # 3. walidacja z 'irate lim' (w mL/h)
    assert d.validate_rate(26.0, "mL/min") is None       # 1560 < 1561.2
    assert d.validate_rate(27.0, "mL/min") is not None   # 1620 > max
    assert d.validate_rate(0.0001, "mL/h") is not None   # < min
    assert d.validate_rate(1.0, "L/h") is not None       # nieznana jednostka

    # 4. START w INF, zmiana kierunku w biegu przez rrun
    d.execute(Op.START)
    assert t.sent[-1] == "irun" and d._running
    d.execute(Op.SET_DIRECTION, direction="WDR")
    assert t.sent[-1] == "rrun" and d._direction == "WDR"

    # 5. POLL: status -> rate/objętość przeliczone z fL, kierunek WDR
    st = d.poll()
    assert st.running and st.direction == "WDR"
    assert st.rate == 120.0 and st.rate_unit == "mL/h"   # 120000 µL/h
    assert abs(st.dispensed - 0.5) < 1e-9 and st.dispensed_unit == "mL"
    assert st.diameter == 14.43 and st.alarm is None

    # 6. STOP; zmiana kierunku na postoju = tylko cache (użyta w START)
    d.execute(Op.STOP)
    assert t.sent[-1] == "stop" and not d._running
    d.execute(Op.SET_DIRECTION, direction="INF")
    assert t.sent[-1] == "stop"                # żadnej komendy nie było
    d.execute(Op.START)
    assert t.sent[-1] == "irun"

    # 7. target volume: mL, µL, 0 = praca ciągła (ctvolume)
    d.execute(Op.SET_TARGET_VOLUME, volume=12.5, unit="mL")
    assert t.sent[-1] == "tvolume 12.5 m"
    assert d._target == 12.5 and d._target_unit == "mL"
    d.execute(Op.SET_TARGET_VOLUME, volume=500, unit="µL")
    assert t.sent[-1] == "tvolume 500 u"
    d.execute(Op.SET_TARGET_VOLUME, volume=0)
    assert t.sent[-1] == "ctvolume" and d._target is None

    # 8. średnica + re-sync rate/tvolume/limitów (pompa kasuje rate
    #    przy zmianie strzykawki — limity zależą od strzykawki)
    d.execute(Op.SET_DIAMETER, mm=20.0)
    assert t.sent[-4:] == ["diameter 20", "irate", "tvolume", "irate lim"]
    assert d._diameter == 20.0

    # 8b. cache rate odświeżany z pompy, nie z GUI
    d._rate, d._rate_unit = 250.0, "mL/h"
    d.execute(Op.SET_DIAMETER, mm=26.59)
    assert t.sent[-4:] == ["diameter 26.59", "irate", "tvolume",
                           "irate lim"]
    # fake trzyma rate z testu 2 (120000 u/h) — nie nasze ręczne 250
    assert d._rate == 120000.0 and d._rate_unit == "mL/h"

    # 9. zerowanie liczników
    d.execute(Op.CLEAR_DISPENSED)
    assert t.sent[-1] == "cvolume" and t.disp_fl == 0

    # 10. stall: prompt '*' + flaga S -> alarm, bieg stop
    t.stall = True
    st = d.execute(Op.START)
    assert st.alarm == "S" and not st.running
    t.stall = False
    d.execute(Op.STOP)
    assert d.poll().alarm is None

    # 11. błąd komendy -> PumpCommandError z treścią
    class BadP11(FakeP11):
        def request(self, frame, **kw):
            return b"\nCommand error:\nUnknown command\n:\r"

    d2 = P11EliteDriver(BadP11())
    try:
        d2.execute(Op.START)
        raise AssertionError("powinno rzucić")
    except PumpCommandError as e:
        assert "Command error" in str(e)

    print("p11elite: OK")