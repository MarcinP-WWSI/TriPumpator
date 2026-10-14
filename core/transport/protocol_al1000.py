"""Protokół RS-232 WPI AL-1000 — Basic mode (manual, str. 31).

Master-slave: pompa odpowiada wyłącznie na komendę. Gramy tylko w
Basic mode (SAF 0); Safe mode (CRC16 + timeout + auto-alarmy) celowo
nie jest tu obsłużony — patrz "RS-232 Protocol: Safe Mode".

Kontrakt modułu:
    build_command(address, command) -> bytes      komenda do pompy
    parse_response(frame) -> AL1000Response       odpowiedź pompy
"""

from __future__ import annotations

from dataclasses import dataclass

# Bajty sterujące ramki (manual: "RS-232 Protocol: Basic Mode")
STX, ETX, CR = 0x02, 0x03, 0x0D

ADDRESS_MAX = 99  # <address> => 0..99

# <prompt> => I | W | S | P | T | U (manual: składnia wspólna Basic/Safe)
STATUS = {
    "I": "infuzja",
    "W": "withdraw",
    "S": "program zatrzymany",
    "P": "program wstrzymany (pauza)",
    "T": "pauza czasowa",
    "U": "czeka na trigger",
}

# <alarm type> => R | S | T | E | O (manual: "Command Errors and Alarms")
ALARMS = {
    "R": "reset zasilania",
    "S": "STALL silnika",
    "T": "timeout komunikacji (Safe mode)",
    "E": "błąd programu pompowania",
    "O": "numer fazy poza zakresem",
}

# <error> => pusty | NA | OOR | COM | IGN (manual: "Command Errors")
COMMAND_ERRORS = {
    "": "komenda nierozpoznana",
    "NA": "komenda nieaktualna w tym stanie pompy",
    "OOR": "wartość poza zakresem",
    "COM": "uszkodzony pakiet",
    "IGN": "zignorowano (start nowej fazy programu)",
}


class AL1000CommError(Exception):
    """Ramka lub odpowiedź niezgodna z protokołem AL-1000."""


@dataclass(frozen=True)
class AL1000Response:
    """Sparsowana odpowiedź: STX <adres><status>[<data>] ETX."""

    address: int
    status: str              # klucz z STATUS albo "A" (ramka alarmowa)
    data: str                # surowe <data> (za adresem i statusem)
    alarm: str | None = None # kod alarmu ("S", "R", ...) gdy status == "A"
    error: str | None = None # kod błędu ("OOR", "NA", "", ...) gdy data "?..."

    @property
    def is_alarm(self) -> bool:
        return self.alarm is not None

    @property
    def is_error(self) -> bool:
        return self.error is not None

    @property
    def status_text(self) -> str:
        if self.is_alarm:
            return f"ALARM: {ALARMS.get(self.alarm, self.alarm)}"
        return STATUS[self.status]

    @property
    def error_text(self) -> str | None:
        if not self.is_error:
            return None
        return COMMAND_ERRORS.get(self.error, self.error)


def build_command(address: int, command: str = "") -> bytes:
    """Ramka komendy Basic mode: <adres><KOMENDA><CR>.

    Pusta komenda = zapytanie o status ("A packet without a command
    is interpreted as a status query"). Pompa sama usuwa spacje i
    uppercasuje — robimy to i tak, żeby być przewidywalnym.
    """
    if not 0 <= address <= ADDRESS_MAX:
        raise AL1000CommError(
            f"adres poza zakresem 0..{ADDRESS_MAX}: {address}")
    cmd = command.strip().upper()
    try:
        return f"{address}{cmd}".encode("ascii") + bytes([CR])
    except UnicodeEncodeError as e:
        raise AL1000CommError(
            f"komenda zawiera znaki spoza ASCII: {command!r}") from e


def parse_response(frame: bytes) -> AL1000Response:
    """Sparsuj odpowiedź pompy; rzuca AL1000CommError przy złej strukturze.

    Zasada: struktura ścisła (STX/ETX, adres, znany znak statusu),
    payload pobłażliwy (nieznany kod błędu/alarmu przechodzi jako
    surowy kod z tekstem zastępczym).
    """
    if len(frame) < 4 or frame[0] != STX or frame[-1] != ETX:
        raise AL1000CommError(f"zła ramka: {frame!r}")

    payload = frame[1:-1].decode("ascii", errors="replace")

    # <address> => <n> [<n>] — jedna lub dwie cyfry
    i = 0
    while i < 2 and i < len(payload) and payload[i].isdigit():
        i += 1
    if i == 0 or i >= len(payload):
        raise AL1000CommError(f"brak adresu lub statusu: {payload!r}")

    address, status, data = int(payload[:i]), payload[i], payload[i + 1:]

    alarm = None
    error = None
    if status == "A":
        # <alarm> => A ? <alarm type>
        raw = data[1:] if data.startswith("?") else data
        alarm = raw[:1] or "?"
    elif status not in STATUS:
        raise AL1000CommError(f"nieznany znak statusu: {payload!r}")
    elif data.startswith("?"):
        # <command error> => ? <error>
        error = data[1:].strip()

    return AL1000Response(address, status, data, alarm=alarm, error=error)


if __name__ == "__main__":
    r = parse_response(b"\x021I\x03")
    assert (r.address, r.status) == (1, "I")
    assert not r.is_alarm and not r.is_error

    r = parse_response(b"\x021SNEAL1000V2.2\x03")
    assert r.data == "NEAL1000V2.2" and r.status == "S"

    r = parse_response(b"\x021A?S\x03")
    assert r.is_alarm and r.alarm == "S" and "STALL" in r.status_text

    r = parse_response(b"\x021S?OOR\x03")
    assert r.is_error and r.error == "OOR" and r.error_text

    r = parse_response(b"\x020S?\x03")
    assert r.is_error and r.error == ""      # samo "?" = nierozpoznana

    assert build_command(1, "run") == b"1RUN\r"
    assert build_command(0) == b"0\r"

    for bad in (b"", b"\x021I", b"1I\x03", b"\x02XI\x03"):
        try:
            parse_response(bad)
            raise AssertionError(f"powinno rzucić: {bad!r}")
        except AL1000CommError:
            pass

    print("protocol_al1000: OK")