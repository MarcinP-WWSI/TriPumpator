"""Transport szeregowy: port COM -> przejściówka USB->RS-232 -> pompa.

Warstwa celowo nie zna żadnego protokołu pompy: terminator, baud i
parametry ramki (8N1 itd.) podaje driver przy tworzeniu transportu.

Umowa z warstwą wyżej (driver):
    request(frame) -> bytes   pełna odpowiedź z terminatorem
    request(frame) -> None    brak odpowiedzi w czasie timeoutu
    request(frame) -> rzuca TransportError   port martwy (USB wypięty,
                                             zajęty przez inny proces)

Wołany wyłącznie z wątku roboczego pompy (blokujące I/O jest OK).
"""

from __future__ import annotations

import sys
import time

import serial


class TransportError(Exception):
    """Port szeregowy nieaktywny — wymaga reopen() lub decyzji użytkownika."""


class SerialTransport:
    """Jeden port COM. Jedna pompa. Blokujący request/response."""

    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        bytesize: int = serial.EIGHTBITS,
        parity: str = serial.PARITY_NONE,
        stopbits: float = serial.STOPBITS_ONE,
        terminator: bytes = b"\x03",   # AL-1000: ETX; model B/C: co innego
        timeout: float = 1.0,
        write_timeout: float = 1.0,
    ):
        self.port = port
        self.terminator = terminator
        self._timeout = timeout
        self._conf = dict(
            baudrate=baudrate, bytesize=bytesize, parity=parity,
            stopbits=stopbits, timeout=timeout, write_timeout=write_timeout,
        )
        self._ser = serial.Serial(port=port, **self._conf)

    # ---------------------------------------------------------------- API

    def request(self, frame: bytes, terminator: bytes | None = None) \
            -> bytes | None:
        """Wyślij ramkę, zbierz odpowiedź do terminatora (włącznie).

        None = cisza w ramach timeoutu (pompa zajęta / zły adres / brak
        pompy na magistrali). TransportError = port nieżywy.
        """
        term = terminator or self.terminator
        deadline = time.monotonic() + self._timeout

        try:
            # stare śmiecie z poprzedniej, niedokończonej wymiany — w kosz
            self._ser.reset_input_buffer()
            self._ser.write(frame)

            buf = bytearray()
            while term not in buf:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._ser.timeout = remaining
                chunk = self._ser.read(64)
                if not chunk:
                    return None
                buf += chunk
            return bytes(buf[: buf.index(term) + len(term)])

        except serial.SerialException as e:
            raise TransportError(
                f"port {self.port} nieaktywny: {e}") from e

    def reopen(self) -> None:
        """Spróbuj wskrzesić port (po wypięciu/wpięciu USB)."""
        try:
            self._ser.close()
        except serial.SerialException:
            pass
        try:
            self._ser = serial.Serial(port=self.port, **self._conf)
        except serial.SerialException as e:
            raise TransportError(f"reopen {self.port}: {e}") from e

    def close(self) -> None:
        try:
            self._ser.close()
        except serial.SerialException:
            pass

    def __enter__(self) -> "SerialTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ------------------------------------------------------------------ narzędzie
if __name__ == "__main__":
    # Test kabla/pompy w duchu "Getting Started With RS-232" z manuala:
    #   uv run core/transport/serial.py COM3        (adres 0, 19200)
    #   uv run core/transport/serial.py /dev/ttyUSB0 9600 1
    # wysyła VER pod wskazany adres i pokazuje surową + sparsowaną odpowiedź.
    from core.transport.protocol_al1000 import (
        AL1000CommError, build_command, parse_response,
    )
    import serial.tools.list_ports

    if len(sys.argv) < 2:
        print("porty COM w systemie:")
        for p in serial.tools.list_ports.comports():
            print(f"  {p.device}  {p.description}")
        print("użycie: serial_port.py <PORT> [BAUD] [ADRES]")
        sys.exit(0)

    port = sys.argv[1]
    baud = int(sys.argv[2]) if len(sys.argv) > 2 else 19200
    addr = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    frame = build_command(addr, "VER")
    print(f"-> {port} @{baud}: {frame!r}")
    try:
        with SerialTransport(port, baudrate=baud) as t:
            reply = t.request(frame)
    except (TransportError, serial.SerialException) as e:
        sys.exit(f"BŁĄD PORTU: {e}")

    if reply is None:
        sys.exit("brak odpowiedzi (sprawdź: adres, baud, kabel, zasilanie)")
    print(f"<- {reply!r}")
    try:
        r = parse_response(reply)
        print(f"   adres={r.address} status={r.status} ({r.status_text})"
              f"  data={r.data!r}")
    except AL1000CommError as e:
        sys.exit(f"odpowiedź nie przechodzi parsowania: {e}")