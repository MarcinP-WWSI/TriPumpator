"""Driver WPI AL-1000 — Basic mode (manual WPI, str. 30–39).

Jedna pompa na własny port: adres sieciowy stały (0), bo to jedyny
mieszkaniec magistrali. Cache kierunku/limity utrzymywane po stronie
drivera, bo pompa raportuje kierunek tylko w statusach I/W.

Objętości: VOL na pompie jest w JEDNOSTKACH POMPY (ustalanych
średnicą strzykawki, nadpisywanych komendą VOL UL/ML). GUI może
podać wartość w mL lub µL — driver przelicza na jednostki pompy
przy SET i raportuje target w jednostkach pompy (sync VOL przy
identyfikacji i po każdej zmianie DIA).
"""

from __future__ import annotations

import re

from core.pump.base import (
    BasePumpDriver, Op, PumpCapabilities, PumpCommandError, PumpStatus,
    norm_unit,
)
from core.transport.protocol_al1000 import build_command, parse_response

# jednostki GUI <-> kody AL-1000 (manual: RAT, <rate units>)
UNIT_TO_AL = {"µL/min": "UM", "mL/min": "MM", "µL/h": "UH", "mL/h": "MH"}

# Appendix A: max [mL/h] ≈ 1.778·d², min [µL/h] ≈ 0.0331·d²
MAX_RATE_COEF = 1.778     # mL/h na mm²
MIN_RATE_COEF = 0.0331    # µL/h na mm²


def _fmt(v: float) -> str:
    """Liczka pod <float> AL-1000: max 4 cyfry, max 3 po kropce."""
    if v < 0:
        raise PumpCommandError(f"wartość ujemna: {v}")
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    whole, _, frac = s.partition(".")
    if len(whole) + len(frac) > 4:
        raise PumpCommandError(
            f"wartość {v} nie mieści się w formacie pompy (max 4 cyfry)")
    return s


class AL1000Driver(BasePumpDriver):
    capabilities = PumpCapabilities(
        name="WPI AL-1000",
        rate_units=("mL/h", "mL/min", "µL/h", "µL/min"),
        rate_min=0.0,
        rate_max=float("inf"),      # limity dynamiczne — patrz validate_rate
        directions=("INF", "WDR"),
        volume_units=("mL", "µL"),
        target_volume=True,
        dispensed_query=True,
        purge=True,
        diameter_setting=True,
        diameter_range=(0.1, 50.0),
        set_rate_while_running=True,
    )

    ADDR = 0  # jedna pompa na port — adres sieciowy bez znaczenia

    # Basic mode, ustawienia fabryczne (manual: 8N1, domyślnie 19200,
    # odpowiedź domknięta ETX)
    TRANSPORT_CONF = {
        "baudrate": 19200,
        "bytesize": 8,        # serial.EIGHTBITS
        "parity": "N",        # serial.PARITY_NONE
        "stopbits": 1,        # serial.STOPBITS_ONE
        "terminator": b"\x03",
    }

    def __init__(self, transport):
        super().__init__(transport)
        self._direction: str | None = None
        self._diameter: float | None = None
        self._rate: float | None = None
        self._rate_unit: str | None = None
        self._last_status: str = "S"
        self._alarm: str | None = None
        self._target_volume: float | None = None   # wartość w _vol_unit
        self._vol_unit: str = "mL"                 # jednostki obj. pompy

    # ------------------------------------------------------------ rdzeń

    def _req(self, text: str = ""):
        """Wymiana ramka→odpowiedź; klasyfikacja błędu/alarmu."""
        reply = self.transport.request(build_command(self.ADDR, text))
        if reply is None:
            raise TimeoutError(f"AL-1000: brak odpowiedzi na {text!r}")
        resp = parse_response(reply)
        if resp.is_alarm:
            # odpowiedź alarmowa = komenda NIE została wykonana;
            # alarm potwierdzi następna udana wymiana (manual)
            self._alarm = resp.alarm
            return resp
        if resp.is_error:
            code = resp.error or "?"
            raise PumpCommandError(
                f"{text!r}: [{code}] "
                f"{resp.error_text or 'komenda odrzucona'}")
        self._alarm = None
        self._last_status = resp.status
        return resp

    def query_version(self) -> str:
        resp = self._req("VER")
        if resp.is_alarm:
            # po resecie zasilania pompa wysyła A?R zamiast odpowiedzi —
            # potwierdzamy (pusta komenda) i ponawiamy raz
            self._req("")
            resp = self._req("VER")
            if resp.is_alarm:
                raise PumpCommandError(
                    f"pompa w stanie alarmu: {resp.status_text}")
        self._sync_direction()
        self._sync_setup()          # DIA + VOL -> cache (GUI i przeliczenia)
        return resp.data

    def execute(self, op: Op, **kw) -> PumpStatus:
        match op:
            case Op.START:   resp = self._req("RUN")
            case Op.STOP:    resp = self._req("STP")
            case Op.PURGE:   resp = self._req("PUR")
            case Op.SET_RATE:
                unit = norm_unit(kw.get("unit", "mL/h"))
                if unit not in UNIT_TO_AL:
                    raise PumpCommandError(f"nieznana jednostka: {unit}")
                v = _fmt(float(kw["value"]))
                # w pauzie zwykły RAT zresetowałby program do fazy 1
                # (manual: RAT C nie czyści pauzy)
                rat = "RAT C" if self._last_status in "IWPT" else "RAT"
                resp = self._req(f"{rat} {v} {UNIT_TO_AL[unit]}")
            case Op.SET_DIRECTION:
                d = kw["direction"]
                if d not in ("INF", "WDR", "REV"):
                    raise PumpCommandError(f"kierunek: {d}")
                resp = self._req(f"DIR {d}")
            case Op.SET_TARGET_VOLUME:
                unit = norm_unit(kw.get("unit", self._vol_unit))
                if unit not in ("mL", "µL"):
                    raise PumpCommandError(f"jednostka objętości: {unit}")
                v = float(kw["volume"])
                v_ml = v / 1000.0 if unit == "µL" else v
                v_pump = v_ml * 1000.0 if self._vol_unit == "µL" else v_ml
                resp = self._req(f"VOL {_fmt(v_pump)}")
            case Op.SET_DIAMETER:      resp = self._req(f"DIA {_fmt(kw['mm'])}")
            case Op.CLEAR_DISPENSED:
                resp = self._req("CLD INF")
                resp = self._req("CLD WDR")
            case Op.POLL:              resp = self._req("DIS")
            case _:                   raise ValueError(op)

        if not resp.is_alarm:     # wykonane — cache PRZED statusem
            match op:
                case Op.SET_RATE:
                    self._rate, self._rate_unit = kw["value"], unit
                case Op.SET_DIRECTION:
                    if kw["direction"] in ("INF", "WDR"):
                        self._direction = kw["direction"]
                case Op.SET_TARGET_VOLUME:
                    # 0 = praca ciągła; cache w jednostkach pompy
                    self._target_volume = v_pump if v_pump > 0 else None
                case Op.SET_DIAMETER:
                    # uwaga: DIA zmienia jednostki objętości i kasuje
                    # liczniki (manual) — odśwież cache VOL z pompy
                    self._diameter = kw["mm"]
                    self._sync_vol()
        return self._to_status(resp)

    def poll(self) -> PumpStatus | None:
        try:
            return self.execute(Op.POLL)
        except TimeoutError:
            return None

    # ------------------------------------------------------ walidacja

    def validate_rate(self, value: float, unit: str) -> str | None:
        unit = norm_unit(unit)
        if unit not in self.capabilities.rate_units:
            return f"nieobsługiwana jednostka: {unit}"
        if self._diameter is None:
            return None            # średnica nieznana — pompa rozstrzygnie
        v = self.to_base_unit(value, unit, "mL/h")
        hi = MAX_RATE_COEF * self._diameter ** 2
        lo = MIN_RATE_COEF * self._diameter ** 2 / 1000.0   # µL/h -> mL/h
        if not lo <= v <= hi:
            return (f"rate {value:g} {unit} poza zakresem "
                    f"dla strzykawki Ø{self._diameter:g} mm: "
                    f"{lo:.3g}–{hi:.4g} mL/h")
        return None

    # ------------------------------------------------------- pomocnicze

    def _sync_direction(self):
        resp = self._req("DIR")
        if not resp.is_alarm and resp.data in ("INF", "WDR", "STK"):
            self._direction = resp.data

    def _sync_setup(self) -> None:
        """DIA + VOL przy identyfikacji — cache dla GUI i przeliczeń."""
        resp = self._req("DIA")
        if not resp.is_alarm:
            try:
                self._diameter = float(resp.data.strip())
            except ValueError:
                pass
        self._sync_vol()

    def _sync_vol(self) -> None:
        """Best-effort odczyt VOL; porażka nie może ubić operacji."""
        try:
            resp = self._req("VOL")
        except (TimeoutError, PumpCommandError):
            return
        if not resp.is_alarm:
            self._read_vol(resp.data)

    def _read_vol(self, data: str) -> None:
        m = re.fullmatch(r"([\d.]+)(ML|UL)?", data.strip().upper())
        if not m:
            return                  # format nieznany — cache nietknięty
        v = float(m[1])
        self._target_volume = v if v > 0 else None   # 0 = praca ciągła
        if m[2]:
            self._vol_unit = "µL" if m[2] == "UL" else "mL"

    def _to_status(self, resp) -> PumpStatus:
        st = PumpStatus(raw=resp.data)

        if resp.is_alarm:
            st.alarm = resp.alarm
            st.running = False
            return st

        st.running = resp.status in ("I", "W")
        if resp.status == "W":
            st.direction = self._direction = "WDR"
        elif resp.status == "I":
            st.direction = self._direction = "INF"
        else:
            st.direction = self._direction

        st.rate, st.rate_unit = self._rate, self._rate_unit
        st.target_volume = self._target_volume
        st.target_volume_unit = self._vol_unit
        st.diameter = self._diameter

        # DIS: "I<inf>W<wdr>ML|UL" — pokazujemy licznik bieżącego kierunku
        if m := re.fullmatch(r"I([\d.]+)W([\d.]+)(ML|UL)", resp.data):
            inf, wdr, u = float(m[1]), float(m[2]), m[3]
            st.dispensed_unit = "µL" if u == "UL" else "mL"
            st.dispensed = wdr if st.direction == "WDR" else inf
        return st


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    from core.transport.protocol_al1000 import STX, ETX

    class _FakeTransport:
        """Minimalny AL-1000 do testów drivera w izolacji."""
        def __init__(self):
            self.sent, self.running = [], False
            self.direction = "INF"
            self.diameter = 14.43
            self.target = 10.5          # mL (fake raportuje zawsze ML)
            self.disp = {"I": 0.500, "W": 0.200}
            self.alarm_next = False

        def request(self, frame):
            text = frame.decode("ascii").strip()
            assert text.startswith("0"), f"zły adres: {text!r}"
            cmd = text[1:]
            self.sent.append(cmd)

            def r(data="", status="S"):
                return (bytes([STX]) + f"0{status}{data}".encode()
                        + bytes([ETX]))

            st = "I" if self.running else "S"
            if self.alarm_next:
                self.alarm_next = False
                return r("?S", status="A")
            if cmd == "":        return r(status=st)
            if cmd == "VER":     return r("NEAL1000V2.2")
            if cmd == "DIR":     return r(self.direction)
            if cmd == "DIS":      return r(f"I{self.disp['I']:.3f}"
                                           f"W{self.disp['W']:.3f}ML", st)
            if cmd.startswith("DIA"):
                if len(cmd) > 3:            # set: "DIA 14.43"
                    self.diameter = float(cmd.split()[1])
                    return r(status=st)
                return r(f"{self.diameter:g}", st)      # query
            if cmd.startswith("VOL"):
                if len(cmd) > 3:            # set: "VOL 0.5"
                    self.target = float(cmd.split()[1])
                    return r(status=st)
                return r(f"{self.target:g}ML", st)     # query
            if cmd in ("RUN", "PUR"):
                self.running = True
                return r(status="I")
            if cmd == "STP":
                self.running = False
                return r(status="S")
            if cmd == "CLD INF": self.disp["I"] = 0.0
            if cmd == "CLD WDR": self.disp["W"] = 0.0
            return r(status=st)             # RAT itd. — akceptacja

    t = _FakeTransport()
    d = AL1000Driver(t)

    # 1. identyfikacja: VER + sync DIR/DIA/VOL (cache dla GUI)
    assert d.query_version() == "NEAL1000V2.2"
    assert t.sent == ["VER", "DIR", "DIA", "VOL"]
    assert d._direction == "INF"
    assert d._diameter == 14.43
    assert d._target_volume == 10.5 and d._vol_unit == "mL"

    # 2. stop -> zwykły RAT; w pracy -> RAT C (bez resetu programu)
    d.execute(Op.SET_RATE, value=250, unit="mL/h")
    assert t.sent[-1] == "RAT 250 MH"
    d.execute(Op.START)
    assert t.sent[-1] == "RUN"
    d.execute(Op.SET_RATE, value=125.5, unit="μL/min")   # μ greckie -> norm
    assert t.sent[-1] == "RAT C 125.5 UM"

    # 3. format liczb: max 4 cyfry, max 3 po kropce
    assert _fmt(0.033) == "0.033" and _fmt(1000) == "1000"
    for bad in (12345, 9999.9, -1):
        try:
            _fmt(bad)
            raise AssertionError(f"powinno rzucić: {bad}")
        except PumpCommandError:
            pass

    # 4. DIA set: cache średnicy + odświeżenie VOL z pompy
    d.execute(Op.SET_DIAMETER, mm=14.43)
    assert t.sent[-2] == "DIA 14.43" and t.sent[-1] == "VOL"
    assert d._diameter == 14.43 and d._target_volume == 10.5

    # 5. target w µL: 500 µL -> 0.5 mL -> "VOL 0.5" (pompa w mL)
    st = d.execute(Op.SET_TARGET_VOLUME, volume=500, unit="µL")
    assert t.sent[-1] == "VOL 0.5"
    assert st.target_volume == 0.5 and st.target_volume_unit == "mL"
    assert d._target_volume == 0.5
    d.execute(Op.SET_TARGET_VOLUME, volume=12.5)     # mL domyślnie
    assert t.sent[-1] == "VOL 12.5"
    d.execute(Op.SET_TARGET_VOLUME, volume=0)        # 0 = praca ciągła
    assert t.sent[-1] == "VOL 0" and d._target_volume is None

    # 6. limity wg Appendix A — średnica z identify (DIA query)
    assert d.validate_rate(370.0, "mL/h") is None     # Ø14.43: ~370.2
    assert d.validate_rate(380.0, "mL/h") is not None
    assert d.validate_rate(2.0, "mL/min") is None     # 120 mL/h — OK
    assert d.validate_rate(0.001, "mL/h") is not None  # poniżej min

    # 7. poll: status biegu + dispensed + średnica z cache
    st = d.poll()
    assert st.running and st.direction == "INF"
    assert st.dispensed == 0.500 and st.dispensed_unit == "mL"
    assert st.diameter == 14.43

    # 8. alarm blokuje komendę; następna wymiana potwierdza alarm
    t.alarm_next = True
    st = d.execute(Op.STOP)
    assert st.alarm == "S" and st.running is False    # STP nie wykonany
    assert t.running                                 # fake dalej pompuje
    st = d.poll()
    assert st.alarm is None and st.running           # DIS potwierdził

    # 9. prawdziwy stop + czyszczenie obu liczników
    d.execute(Op.STOP)
    assert t.sent[-1] == "STP" and not t.running
    d.execute(Op.CLEAR_DISPENSED)
    assert t.sent[-2:] == ["CLD INF", "CLD WDR"]
    st = d.poll()
    assert not st.running and st.dispensed == 0.0

    # 10. błąd komendy -> wyjątek z kodem
    class _BadTransport(_FakeTransport):
        def request(self, frame):
            return bytes([STX]) + b"0S?NA" + bytes([ETX])

    # 11. deklaratywne niewspieranie: target czasu i siły (manual AL-1000
    #     nie definiuje zadnego odpowiednika TTIME/FORCE w RS-232)
    assert AL1000Driver.capabilities.target_time is False
    assert AL1000Driver.capabilities.force_setting is False

    d2 = AL1000Driver(_BadTransport())
    try:
        d2.execute(Op.START)
        raise AssertionError("powinno rzucić PumpCommandError")
    except PumpCommandError as e:
        assert "NA" in str(e)

    print("al1000: OK")