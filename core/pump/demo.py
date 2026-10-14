import time
from .base import BasePumpDriver, Op, PumpStatus, PumpCapabilities


class DemoDriver(BasePumpDriver):
    """Generyczna pompa do developmentu — niezależny klon dla każdego portu."""
    capabilities = PumpCapabilities(
        name="Pompa demo", rate_units=("mL/h", "mL/min"),
        directions=("INF", "WDR"), target_volume=True, dispensed_query=True,
        set_rate_while_running=True)

    def __init__(self, transport):
        super().__init__(transport)
        self.rate, self.unit = 100.0, "mL/h"
        self.running, self.direction = False, "INF"
        self.dispensed, self._t = 0.0, time.monotonic()

    def query_version(self):
        return "DEMO-1.0"

    def _tick(self):
        now = time.monotonic()
        if self.running:
            self.dispensed += self.rate / 3600.0 * (now - self._t)
        self._t = now

    def execute(self, op: Op, **kw) -> PumpStatus:
        self._tick()
        match op:
            case Op.START:   self.running = True
            case Op.STOP:    self.running = False
            case Op.SET_RATE: self.rate, self.unit = kw["value"], kw["unit"]
            case Op.SET_DIRECTION: self.direction = kw["direction"]
            case Op.CLEAR_DISPENSED: self.dispensed = 0.0
            case Op.PURGE:   self.running = True
        return PumpStatus(running=self.running, direction=self.direction,
                          rate=self.rate, rate_unit=self.unit,
                          dispensed=self.dispensed)

    def poll(self):
        return self.execute(Op.POLL)