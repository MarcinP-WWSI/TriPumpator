"""Persystencja procedur Programatora: JSON, schemat wersjonowany.

Plik: {"version": 1, "steps": [ {"kind": ...}, ... ]}
Rodzaje kroków: command | loop_start | loop_end | condition.
Zapis atomowy (tmp+replace); odczyt strict: nieznany identyfikator
komendy/warunku/kind = błąd (plik "z przyszłości" nie wykona się
po cichu). Walidacji logicznej wobec caps NIE robi storage — robi
ją Programator przy Run (plik z innego stanowiska wczytuje się
do edycji, komunikaty wylecą z walidatora).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.proc.steps import (
    CommandStep, CondCmd, ConditionStep, LoopEndStep, LoopStartStep,
    ProcCmd, PROC_COMMANDS, CONDITIONS,
)

SCHEMA_VERSION = 1
PROC_DIR = Path(__file__).resolve().parent.parent.parent / "procedures"
PROC_SUFFIX = ".proc.json"

_KINDS = ("command", "loop_start", "loop_end", "condition")


class ProcStorageError(Exception):
    """Błąd zapisu/odczytu procedury (opisywalny dla GUI)."""


def save_procedure(steps: list, path: Path | str) -> None:
    """Zapisz procedurę (atomowo). Ścieżka bez .proc.json -> dopisana."""
    path = _with_suffix(Path(path))
    data = {"version": SCHEMA_VERSION, "steps": [_dump(s) for s in steps]}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       "utf-8")
        tmp.replace(path)
    except OSError as e:
        raise ProcStorageError(f"zapis {path}: {e}") from e


def load_procedure(path: Path | str) -> list:
    """Wczytaj procedurę; strict na kind/cmd/cond, wersja sprawdzana."""
    path = Path(path)
    if not path.exists() and not path.name.endswith(PROC_SUFFIX):
        path = _with_suffix(path)
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as e:
        raise ProcStorageError(f"brak pliku: {path}") from e
    except (OSError, ValueError) as e:
        raise ProcStorageError(f"odczyt {path}: {e}") from e
    if not isinstance(raw, dict) or "steps" not in raw:
        raise ProcStorageError(f"{path}: brak pola 'steps'")
    version = raw.get("version")
    if version != SCHEMA_VERSION:
        raise ProcStorageError(
            f"{path}: wersja schematu {version} != {SCHEMA_VERSION}")
    steps = []
    for i, item in enumerate(raw["steps"], start=1):
        try:
            steps.append(_load(item))
        except ProcStorageError as e:
            raise ProcStorageError(f"krok {i} w {path}: {e}") from e
    return steps


# ------------------------------------------------------------ (de)serializacja

def _dump(step) -> dict:
    match step:
        case LoopStartStep(count=c):
            return {"kind": "loop_start", "count": c}
        case LoopEndStep():
            return {"kind": "loop_end"}
        case ConditionStep(cond=cond, pump=p, args=a, timeout_s=ts,
                          on_timeout=ot):
            return {"kind": "condition", "cond": cond.name,
                    "pump": p, "args": a, "timeout_s": ts,
                    "on_timeout": ot}
        case CommandStep(cmd=cmd, pumps=p, args=a):
            return {"kind": "command", "cmd": cmd.name,
                    "pumps": list(p) if p is not None else None,
                    "args": a}
    raise ProcStorageError(f"nieznany typ kroku: {type(step).__name__}")


def _load(item: dict):
    if not isinstance(item, dict) or "kind" not in item:
        raise ProcStorageError("brak pola 'kind'")
    kind = item["kind"]
    if kind == "loop_start":
        return LoopStartStep(int(item["count"]))
    if kind == "loop_end":
        return LoopEndStep()
    if kind == "command":
        try:
            cmd = ProcCmd[item["cmd"]]
        except KeyError as e:
            raise ProcStorageError(
                f"nieznana komenda {item['cmd']!r}") from e
        pumps = item.get("pumps")
        return CommandStep(cmd, tuple(pumps) if pumps is not None
                           else None, dict(item.get("args") or {}))
    if kind == "condition":
        try:
            cond = CondCmd[item["cond"]]
        except KeyError as e:
            raise ProcStorageError(
                f"nieznany warunek {item['cond']!r}") from e
        return ConditionStep(
            cond, int(item["pump"]),
            dict(item.get("args") or {}),
            timeout_s=float(item.get("timeout_s", 60.0)),
            on_timeout=item.get("on_timeout", "error"))
    raise ProcStorageError(f"nieznany rodzaj kroku {kind!r}")

def _with_suffix(path: Path) -> Path:
    """Dopisz .proc.json, jeśli go (całego) brak — poprawnie dla
    dwuczłonowych sufiksów: demo.proc.json ma suffix '.json', ale
    NIE jest plikiem procedury z naszego sufiksem... a 'demo'/'noext'
    dostają pełny sufiks. Bez podwajania."""
    if path.name.endswith(PROC_SUFFIX):
        return path
    return path.with_name(path.name + PROC_SUFFIX)

# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    from core.proc.steps import CommandStep, CondCmd, ConditionStep, \
        LoopEndStep, LoopStartStep, ProcCmd, validate
    from core.pump.base import PumpCapabilities
    from core.pump.demo import DemoDriver

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # 1. round-trip pełnej palety typów kroków
        proc = [
            CommandStep(ProcCmd.SET_RATE, (1, 2),
                        {"value": 100.5, "unit": "mL/h"}),
            CommandStep(ProcCmd.START, None),
            LoopStartStep(3),
            CommandStep(ProcCmd.WAIT, None, {"seconds": 30}),
            ConditionStep(CondCmd.DISPENSED_GE, 1,
                          {"value": 5.0, "unit": "mL"},
                          timeout_s=120.0, on_timeout="continue"),
            LoopEndStep(),
            CommandStep(ProcCmd.STOP, (1,)),
            LoopEndStep(),               # nieparzysta? nie — patrz niżej
        ]
        # poprawmy strukturę: loop_end nadmiarowy -> usuwamy ostatni
        proc = proc[:-1]
        p = td / "demo.proc.json"
        save_procedure(proc, p)
        loaded = load_procedure(p)
        assert loaded == proc, (loaded, proc)

        # 2. sufiks dopisywany; zapis atomowy nie zostawia tmp
        p2 = td / "noext"
        save_procedure(proc, p2)
        assert p2.with_suffix(PROC_SUFFIX).exists()
        assert not list(td.glob("*.tmp"))

        # 3. wersja schematu: ręcznie podbity -> odrzucony
        raw = json.loads(p.read_text("utf-8"))
        raw["version"] = 99
        p3 = td / "future.proc.json"
        p3.write_text(json.dumps(raw), "utf-8")
        try:
            load_procedure(p3)
            raise AssertionError("powinno rzucić")
        except ProcStorageError as e:
            assert "wersja" in str(e)

        # 4. strict: nieznana komenda / warunek / kind
        for mutate, needle in (
            (lambda r: r["steps"][0].update(cmd="BOGUS"), "komenda"),
            (lambda r: r["steps"][4].update(cond="BOGUS"), "warunek"),
            (lambda r: r["steps"][0].update(kind="bogus"), "rodzaj"),
        ):
            raw = json.loads(p.read_text("utf-8"))
            mutate(raw)
            bad = td / "bad.proc.json"
            bad.write_text(json.dumps(raw), "utf-8")
            try:
                load_procedure(bad)
                raise AssertionError("powinno rzucić")
            except ProcStorageError as e:
                assert needle in str(e)

        # 5. defaults przy wczytaniu: brak timeout_s/on_timeout -> 60/error
        raw = json.loads(p.read_text("utf-8"))
        cond = raw["steps"][4]
        del cond["timeout_s"]; del cond["on_timeout"]
        p5 = td / "def.proc.json"
        p5.write_text(json.dumps(raw), "utf-8")
        loaded5 = load_procedure(p5)
        c = loaded5[4]
        assert isinstance(c, ConditionStep)
        assert c.timeout_s == 60.0 and c.on_timeout == "error"

        # 6. brak pliku / uszkodzony JSON / brak 'steps'
        try:
            load_procedure(td / "missing.proc.json")
            raise AssertionError()
        except ProcStorageError as e:
            assert "brak pliku" in str(e)
        (td / "broken.proc.json").write_text("{nie", "utf-8")
        try:
            load_procedure(td / "broken.proc.json")
            raise AssertionError()
        except ProcStorageError as e:
            assert "odczyt" in str(e)
        (td / "nosteps.proc.json").write_text('{"version": 1}', "utf-8")
        try:
            load_procedure(td / "nosteps.proc.json")
            raise AssertionError()
        except ProcStorageError as e:
            assert "'steps'" in str(e)

        # 7. wczytana procedura przechodzi walidację (spójność modeli)
        caps = {1: DemoDriver.capabilities, 2: DemoDriver.capabilities}
        assert validate(loaded, caps) == []

        # 8. zapis pustej i wczytanie -> pusta lista
        p8 = td / "empty.proc.json"
        save_procedure([], p8)
        assert load_procedure(p8) == []

        print("storage: OK")