"""Protokół szeregowy Harvard Apparatus Pump 11 Elite (manual 5420-002).

ASCII, linie CR-terminated, koniec odpowiedzi = linia promptu:
  ':' idle, '>' infuzja, '<' withdraw, '*' stall, 'T*' target osiągnięty.
Adres 0 (USB): bez prefiksu; adres != 0: linie danych mają prefiks
'NN:', prompt ma prefiks 'NN' bez dwukropka (manual, str. 51-52).

Parsowanie pobłażliwe: nieznane formaty -> None zamiast wyjątku;
driver decyduje, co to znaczy. Surowe dane traktowane jako niezaufane.
"""

from __future__ import annotations

import re

PROMPTS = (":", ">", "<", "*", "T*")

# jednostki rate GUI -> kody Pump 11 (manual: "m/m = milliliter/minutes")
GUI_RATE_TO_P11 = {"mL/h": "m/h", "mL/min": "m/m",
                   "µL/h": "u/h", "µL/min": "u/m"}
GUI_VOL_TO_P11 = {"mL": "m", "µL": "u"}
VOL_LETTER_TO_GUI = {"m": "mL", "u": "µL", "n": "nL", "p": "pL"}

_ADDR_DATA = re.compile(r"^\d{1,2}:")          # prefiks linii danych


def split_lines(data: bytes) -> list[str]:
    """Bajty odpowiedzi -> linie (bez pustych, bez CR/LF)."""
    text = data.decode("ascii", errors="replace")
    return [ln.strip() for ln in re.split(r"[\r\n]+", text) if ln.strip()]


def prompt_of(line: str) -> str | None:
    """Prompt z linii (z opcjonalnym prefiksem adresu) albo None."""
    for p in PROMPTS:
        rest = line[: -len(p)]
        if line == p or (line.endswith(p) and rest.isdigit()
                        and len(rest) <= 2):
            return p
    return None


def is_prompt_line(line: str) -> bool:
    return prompt_of(line) is not None


def strip_addr_data(line: str) -> str:
    """Usuń prefiks adresu z linii danych ('12:xxx' -> 'xxx')."""
    return _ADDR_DATA.sub("", line, count=1)


def response_complete(data: bytes) -> bool:
    """Warunek until dla transportu: ostatnia linia to prompt."""
    lines = split_lines(data)
    return bool(lines) and is_prompt_line(lines[-1])


def parse_status_line(line: str) -> dict | None:
    """Linia statusu: '<rate fL/s> <czas ms> <obj fL> <6 flag>'.

    Flagi (manual, str. 57): [0] kierunek+bieg (i/I/w/W, wielkie=bieg),
    [1] limit switch (Pump 11: zawsze '.'), [2] stall S/.,
    [3] trigger T/., [4] port kierunku, [5] target T/.
    """
    m = re.fullmatch(r"(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(.+)",
                     strip_addr_data(line))
    if not m:
        return None
    flags = re.findall(r"[iIwWST*.]", m[4])[:6]
    if len(flags) < 6:
        return None
    d = flags[0]
    return {
        "rate_fl_s": int(m[1]),
        "time_ms": int(m[2]),
        "volume_fl": int(m[3]),
        "direction": "WDR" if d.lower() == "w" else "INF",
        "running": d.isupper(),
        "stall": flags[2] == "S",
        "target": flags[5] == "T",
    }


def parse_rate(text: str) -> dict | None:
    """'# xl/xxx' -> {'value', 'ml_per_h'}; None gdy nie sparsowano.

    Akceptuje '26.020 ml/min', '100 u/h', 'n/min', 'n/s', 'µl/hr'...
    """
    t = text.replace("µ", "u").strip()
    m = re.fullmatch(r"([\d.]+)\s+(.+)", t)
    if not m:
        return None
    u = m[2].replace(" ", "")
    m2 = re.fullmatch(r"([munp])l?/([hms])(?:r|in|ec)?", u)
    if not m2:
        return None
    vol = {"m": 1.0, "u": 1e-3, "n": 1e-6, "p": 1e-9}[m2[1]]   # w mL
    per_h = {"h": 1.0, "m": 60.0, "s": 3600.0}[m2[2]]
    return {"value": float(m[1]), "ml_per_h": float(m[1]) * vol * per_h}


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    assert split_lines(b"\n11 Elite 2.0.0.0\r\n:\r") == \
        ["11 Elite 2.0.0.0", ":"]
    assert split_lines(b"") == []

    assert prompt_of(":") == ":" and prompt_of(">") == ">"
    assert prompt_of("T*") == "T*" and prompt_of("03:") == ":"
    assert prompt_of("12>") == ">"                     # adres bez ':'
    assert prompt_of("11 Elite 2.0.0.0") is None
    assert prompt_of("Command error:") is None

    assert response_complete(b"\n:\r")
    assert response_complete(b"\n11 Elite 2.0.0.0\r\n:\r")
    assert not response_complete(b"")
    assert not response_complete(b"\n11 Elite 2.0.0.0\r")

    s = parse_status_line("69444444444 1000 500000000000 I. . .i.")
    assert s and s["running"] and s["direction"] == "INF"
    assert not s["stall"] and not s["target"]
    s = parse_status_line("0 0 0 w. . .w.")
    assert s and not s["running"] and s["direction"] == "WDR"
    s = parse_status_line("0 0 0 i . S . i .")        # spacje między flagami
    assert s and s["stall"] and not s["running"]
    s = parse_status_line("12:0 0 0 i. . .i.")
    assert s and not s["running"] and s["direction"] == "INF"
    assert parse_status_line("garbage") is None
    assert parse_status_line("1 2 3") is None

    assert strip_addr_data("12:26.020 ml/hr") == "26.020 ml/hr"
    assert strip_addr_data("11 Elite 2.0.0.0") == "11 Elite 2.0.0.0"

    r = parse_rate("26.020 ml/min")
    assert abs(r["ml_per_h"] - 1561.2) < 1e-9
    r = parse_rate("100 u/h")
    assert abs(r["ml_per_h"] - 0.1) < 1e-12
    r = parse_rate("25.05 n/min")
    assert abs(r["ml_per_h"] - 1.503e-3) < 1e-9
    r = parse_rate("5.5 µl/s")
    assert abs(r["ml_per_h"] - 19.8) < 1e-9
    assert parse_rate("bla") is None
    assert parse_rate("12 m") is None

    print("protocol_p11: OK")