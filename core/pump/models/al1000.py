import re
from core.pump.base import BasePumpDriver, Op, PumpStatus, PumpCapabilities
from core.transport.protocol_al1000 import build_command, parse_response

UNIT_TO_AL = {"mL/h": "MH", "mL/min": "MM", "µL/h": "UH", "µL/min": "UM"}


class AL1000Driver(BasePumpDriver):
    capabilities = PumpCapabilities(
        name="WPI AL-1000",
        rate_units=("mL/h", "mL/min", "µL/h", "µL/min"),
        directions=("INF", "WDR"),
        target_volume=True,
        dispensed_query=True,
        purge=True,
        diameter_setting=True,
        set_rate_while_running=True,
    )

    ADDR = 0  # jedna pompa na własny port — adres sieciowy nieistotny, stały 0

    def _req(self, text: str = "") -> dict:
        reply = self.transport.request(build_command(self.ADDR, text))
        if reply is None:
            raise TimeoutError(f"AL-1000 ({self.ADDR}): brak odpowiedzi")
        return parse_response(reply)

    def query_version(self) -> str:
        return self._req("VER")["data"]     # np. "NEAL1000V2.2"

    def execute(self, op: Op, **kw) -> PumpStatus:
        match op:
            case Op.START:             p = self._req("RUN")
            case Op.STOP:              p = self._req("STP")
            case Op.PURGE:             p = self._req("PUR")
            case Op.SET_RATE:          p = self._req(f"RAT {kw['value']:g} "
                                                     f"{UNIT_TO_AL[kw['unit']]}")
            case Op.SET_DIRECTION:     p = self._req(f"DIR {kw['direction']}")
            case Op.SET_TARGET_VOLUME: p = self._req(f"VOL {kw['volume']:g}")
            case Op.SET_DIAMETER:      p = self._req(f"DIA {kw['mm']:g}")
            case Op.CLEAR_DISPENSED:   p = self._req("CLD INF")
            case Op.POLL:              p = self._req("DIS")
            case _:                    raise ValueError(op)
        return self._to_status(p)

    def poll(self) -> PumpStatus | None:
        try:
            return self.execute(Op.POLL)
        except TimeoutError:
            return None

    def _to_status(self, p: dict) -> PumpStatus:
        st = PumpStatus(raw=p["data"])
        st.running = p["status"] in ("I", "W")
        st.direction = "WDR" if p["status"] == "W" else "INF"
        if "alarm" in p:
            st.alarm = p["alarm"]          # A?S, A?R itd.
        # odpowiedź DIS: "I<obj.inf>W<obj.wdr>ML"
        if m := re.match(r"I([\d.]+)W([\d.]+)(ML|UL)", p["data"]):
            st.dispensed = float(m[1]) + float(m[2])
        return st