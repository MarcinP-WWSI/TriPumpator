STX, ETX, CR = 0x02, 0x03, 0x0D

STATUS = {"I": "infuzja", "W": "withdraw", "S": "stop", "P": "pauza",
          "T": "pauza_czasowa", "U": "czeka_na_trigger"}
ALARMS = {"R": "reset zasilania", "S": "STALL silnika",
          "T": "timeout komunikacji", "E": "blad programu",
          "O": "faza poza zakresem"}


class AL1000CommError(Exception):
    pass


def build_command(address: int, command: str = "") -> bytes:
    """Basic mode: <adres><komenda><CR>. Pusta komenda = query statusu."""
    cmd = command.strip().upper()
    return f"{address}{cmd}".encode("ascii") + bytes([CR])


def parse_response(frame: bytes) -> dict:
    """STX <adres><status>[dane] ETX -> dict z adresem, statusem i danymi."""
    if len(frame) < 4 or frame[0] != STX or frame[-1] != ETX:
        raise AL1000CommError(f"zla ramka: {frame!r}")
    payload = frame[1:-1].decode("ascii", errors="replace")

    i = 0
    while i < 2 and i < len(payload) and payload[i].isdigit():
        i += 1
    if i == 0 or i >= len(payload):
        raise AL1000CommError(f"zla odpowiedz: {payload!r}")

    parsed = {"address": int(payload[:i]), "status": payload[i],
              "data": payload[i + 1:]}
    if parsed["status"] == "A":
        parsed["alarm"] = ALARMS.get(parsed["data"].lstrip("?")[:1],
                                     parsed["data"])
    elif parsed["data"].startswith("?"):
        parsed["error"] = parsed["data"]      # np. "?OOR"
    return parsed