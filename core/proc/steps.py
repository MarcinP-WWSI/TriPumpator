"""Procedury Programatora: model kroków + walidacja. Bez GUI, bez Qt.

Procedura = lista kroków trzech rodzajów:
    CommandStep   — komenda na wybranych pompach (Op w głębi)
    LoopStartStep — początek pętli (count powtórzeń)
    LoopEndStep   — koniec pętli (paruje z najbliższym otwartym startem)

Struktura spłaszczona na wzór Pumping Program AL-1000 (LP:ST/LP:nn):
prosta walidacja parowania/zagnieżdżania, proste wykonanie przez
wskaźnik + stos liczników. Maks. zagnieżdżenie: MAX_LOOP_DEPTH.

Rejestr PROC_COMMANDS to otwarta struktura: nowa komenda = wpis
w rejestrze + handler w runnerze (core/proc/runner.py) + obsługa
w palecie GUI (ui/programator_window.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

from core.pump.base import PumpCapabilities, norm_unit

MAX_LOOP_DEPTH = 3
MIN_LOOP_COUNT = 1
MAX_LOOP_COUNT = 999
MIN_WAIT_S = 0.1
MAX_WAIT_S = 99999.0


class ProcCmd(Enum):
    """Komendy Programatora. Warstwa nad Op — odseparowana od zmian
    w API pomp; nowe komendy (np. warunkowe) dokłada się tutaj."""
    START = auto()
    STOP = auto()
    PURGE = auto()
    SET_RATE = auto()
    SET_DIRECTION = auto()
    SET_TARGET_VOLUME = auto()
    SET_TARGET_TIME = auto()
    SET_DIAMETER = auto()
    SET_FORCE = auto()
    CLEAR_DISPENSED = auto()
    WAIT = auto()            # pauza w procedurze; pumps ignorowane


@dataclass(frozen=True)
class CommandSpec:
    cmd: ProcCmd
    label: str               # tekst w palecie/listwie GUI
    args: tuple[str, ...]   # nazwy argumentów (kolejność = GUI)
    caps_attr: str | None   # wymagana capability (None = brak wymogu)
    needs_pumps: bool = True    # WAIT: False (pompy nieistotne)


PROC_COMMANDS: dict[ProcCmd, CommandSpec] = {
    ProcCmd.START: CommandSpec(ProcCmd.START, "Start pomp", (), None),
    ProcCmd.STOP: CommandSpec(ProcCmd.STOP, "Stop pomp", (), None),
    ProcCmd.PURGE: CommandSpec(
        ProcCmd.PURGE, "Purge", (), "purge"),
    ProcCmd.SET_RATE: CommandSpec(
        ProcCmd.SET_RATE, "Ustaw rate", ("value", "unit"), None),
    ProcCmd.SET_DIRECTION: CommandSpec(
        ProcCmd.SET_DIRECTION, "Ustaw kierunek", ("direction",),
        "directions"),
    ProcCmd.SET_TARGET_VOLUME: CommandSpec(
        ProcCmd.SET_TARGET_VOLUME, "Target: objętość", ("volume", "unit"),
        "target_volume"),
    ProcCmd.SET_TARGET_TIME: CommandSpec(
        ProcCmd.SET_TARGET_TIME, "Target: czas", ("seconds",),
        "target_time"),
    ProcCmd.SET_DIAMETER: CommandSpec(
        ProcCmd.SET_DIAMETER, "Ustaw średnicę", ("mm",),
        "diameter_setting"),
    ProcCmd.SET_FORCE: CommandSpec(
        ProcCmd.SET_FORCE, "Ustaw siłę", ("percent",), "force_setting"),
    ProcCmd.CLEAR_DISPENSED: CommandSpec(
        ProcCmd.CLEAR_DISPENSED, "Zeruj liczniki", (),
        "dispensed_query"),
    ProcCmd.WAIT: CommandSpec(
        ProcCmd.WAIT, "Czekaj", ("seconds",), None, needs_pumps=False),
}

class CondCmd(Enum):
    """Warunki bramek 'czekaj aż'. Odczytywane z PumpStatus (fizyka)."""
    STOPPED = auto()       # pompa zatrzymana (m.in. target osiągnięty)
    RUNNING = auto()       # pompa pracuje
    DISPENSED_GE = auto()  # dozowane >= value (wspólny mianownik: mL)
    ALARM = auto()         # alarm aktywny (np. stall)


@dataclass(frozen=True)
class ConditionSpec:
    cmd: CondCmd
    label: str
    args: tuple[str, ...] = ()


CONDITIONS: dict[CondCmd, ConditionSpec] = {
    CondCmd.STOPPED: ConditionSpec(CondCmd.STOPPED, "pompa zatrzymana"),
    CondCmd.RUNNING: ConditionSpec(CondCmd.RUNNING, "pompa pracuje"),
    CondCmd.DISPENSED_GE: ConditionSpec(
        CondCmd.DISPENSED_GE, "dozowane >=", ("value", "unit")),
    CondCmd.ALARM: ConditionSpec(CondCmd.ALARM, "alarm aktywny"),
}

ON_TIMEOUT = ("error", "continue")


@dataclass
class ConditionStep:
    cond: CondCmd
    pump: int                      # dokładnie JEDNA pompa (walidowane)
    args: dict = field(default_factory=dict)
    timeout_s: float = 60.0
    on_timeout: str = "error"      # co po przekroczeniu timeoutu

@dataclass
class CommandStep:
    cmd: ProcCmd
    pumps: tuple[int, ...] | None = None   # None = wszystkie pompy sesji
    args: dict = field(default_factory=dict)


@dataclass
class LoopStartStep:
    count: int = 2


@dataclass
class LoopEndStep:
    pass


Step = CommandStep | LoopStartStep | LoopEndStep | ConditionStep


def describe(step: Step) -> str:
    """Krótka etykieta kroku (listwa GUI, logi runnera)."""
    match step:
        case LoopStartStep(count=c):
            return f"PĘTLA ×{c}"
        case LoopEndStep():
            return "KONIEC PĘTLI"
        case ConditionStep(cond=c, pump=p, args=a, timeout_s=t):
            spec = CONDITIONS[c]
            body = ""
            if c is CondCmd.DISPENSED_GE:
                body = f" {a.get('value')} {a.get('unit')}"
            return (f"Czekaj aż {spec.label}{body} [pompa {p}] "
                    f"(max {t:g} s)")
        case CommandStep(cmd=cmd, pumps=p, args=a):
            spec = PROC_COMMANDS[cmd]
            body = ""
            if cmd is ProcCmd.SET_RATE:
                body = f"{a.get('value')} {a.get('unit')}"
            elif cmd is ProcCmd.SET_TARGET_VOLUME:
                body = f"{a.get('volume')} {a.get('unit')}"
            elif cmd is ProcCmd.SET_TARGET_TIME:
                body = f"{a.get('seconds')} s"
            elif cmd is ProcCmd.WAIT:
                body = f"{a.get('seconds')} s"
            elif cmd is ProcCmd.SET_DIAMETER:
                body = f"{a.get('mm')} mm"
            elif cmd is ProcCmd.SET_FORCE:
                body = f"{a.get('percent')}%"
            elif cmd is ProcCmd.SET_DIRECTION:
                body = str(a.get("direction"))
            parts = [spec.label]
            if body:
                parts[0] += f": {body}"
            if spec.needs_pumps:
                who = "wszystkie" if p is None else \
                    ",".join(str(x) for x in p)
                parts.append(f"[{who}]")
            return " ".join(parts)


def validate(steps: list[Step],
             caps: dict[int, PumpCapabilities]) -> list[str]:
    """Walidacja procedury przed uruchomieniem. Zwraca listę błędów
    (pusta = OK). caps: pid -> capabilities pomp bieżącej sesji."""
    errors: list[str] = []
    stack: list[tuple[int, LoopStartStep]] = []   # (indeks, krok)

    def selected(st: CommandStep) -> list[int]:
        return list(caps) if st.pumps is None else list(st.pumps)

    for i, step in enumerate(steps, start=1):
        pos = f"krok {i}"
        match step:
            case LoopStartStep(count=c):
                if len(stack) >= MAX_LOOP_DEPTH:
                    errors.append(
                        f"{pos}: pętle zagnieżdżone głębiej niż "
                        f"{MAX_LOOP_DEPTH}")
                if not MIN_LOOP_COUNT <= c <= MAX_LOOP_COUNT:
                    errors.append(
                        f"{pos}: liczba powtórzeń poza zakresem "
                        f"{MIN_LOOP_COUNT}-{MAX_LOOP_COUNT}: {c}")
                stack.append((i, step))

            case LoopEndStep():
                if not stack:
                    errors.append(
                        f"{pos}: koniec pętli bez początku")
                else:
                    stack.pop()
            case ConditionStep(cond=cond, pump=pid, args=a,
                               timeout_s=ts, on_timeout=ot):
                spec = CONDITIONS.get(cond)
                if spec is None:
                    errors.append(f"{pos}: nieznany warunek {cond}")
                    continue
                if pid not in caps:
                    errors.append(f"{pos}: pompa {pid} nie istnieje "
                                  f"w sesji")
                    continue
                if not MIN_WAIT_S <= ts <= MAX_WAIT_S:
                    errors.append(
                        f"{pos}: timeout poza zakresem "
                        f"{MIN_WAIT_S}-{MAX_WAIT_S:g} s: {ts}")
                if ot not in ON_TIMEOUT:
                    errors.append(
                        f"{pos}: on_timeout musi być jednym z "
                        f"{ON_TIMEOUT}: {ot!r}")
                if cond is CondCmd.DISPENSED_GE:
                    if not caps[pid].dispensed_query:
                        errors.append(
                            f"{pos}: pompa {pid} nie raportuje objętości")
                    for name in spec.args:
                        if name not in a:
                            errors.append(
                                f"{pos}: warunek bez argumentu '{name}'")
                    unit = norm_unit(a.get("unit", ""))
                    if "unit" in a and unit not in caps[pid].volume_units:
                        errors.append(
                            f"{pos}: jednostka {unit} spoza listy "
                            f"objętości modelu (pompa {pid})")
            case CommandStep(cmd=cmd):
                spec = PROC_COMMANDS.get(cmd)
                if spec is None:
                    errors.append(f"{pos}: nieznana komenda {cmd}")
                    continue
                if cmd is ProcCmd.WAIT:
                    secs = step.args.get("seconds")
                    if not isinstance(secs, (int, float)):
                        errors.append(f"{pos}: WAIT bez czasu")
                    elif not MIN_WAIT_S <= secs <= MAX_WAIT_S:
                        errors.append(
                            f"{pos}: WAIT poza zakresem "
                            f"{MIN_WAIT_S}-{MAX_WAIT_S:g} s: {secs}")
                    continue
                if spec.needs_pumps:
                    pids = selected(step)
                    if not pids:
                        errors.append(
                            f"{pos}: brak pomp do wykonania komendy")
                    for pid in pids:
                        if pid not in caps:
                            errors.append(
                                f"{pos}: pompa {pid} nie istnieje "
                                f"w sesji")
                        elif spec.caps_attr and \
                                not getattr(caps[pid], spec.caps_attr):
                            errors.append(
                                f"{pos}: pompa {pid} nie wspiera "
                                f"{spec.label.lower()}")
                # argumenty
                for name in spec.args:
                    if name not in step.args:
                        errors.append(
                            f"{pos}: {spec.label} bez argumentu "
                            f"'{name}'")
                if errors and errors[-1].startswith(f"{pos}:"):
                    continue                     # resztę pomiń dla kroku
                pid0 = selected(step)
                ref = caps[pid0[0]] if pid0 and pid0[0] in caps else None
                if cmd is ProcCmd.SET_RATE:
                    unit = norm_unit(step.args.get("unit", ""))
                    if ref and unit not in ref.rate_units:
                        errors.append(
                            f"{pos}: jednostka {unit} spoza listy "
                            f"modelu (pompa {pid0[0]})")
                elif cmd is ProcCmd.SET_TARGET_VOLUME:
                    unit = norm_unit(step.args.get("unit", ""))
                    if ref and unit not in ref.volume_units:
                        errors.append(
                            f"{pos}: jednostka {unit} spoza listy "
                            f"objętości modelu")
                elif cmd is ProcCmd.SET_DIRECTION:
                    d = step.args.get("direction")
                    if ref and d not in ref.directions:
                        errors.append(
                            f"{pos}: kierunek {d} niedostępny "
                            f"(pompa {pid0[0]})")
                elif cmd is ProcCmd.SET_DIAMETER:
                    mm = step.args.get("mm", 0)
                    if ref and not (ref.diameter_range[0]
                                    <= mm <= ref.diameter_range[1]):
                        errors.append(
                            f"{pos}: średnica {mm} poza zakresem "
                            f"{ref.diameter_range[0]}-"
                            f"{ref.diameter_range[1]} mm")
                elif cmd is ProcCmd.SET_FORCE:
                    pct = step.args.get("percent", 0)
                    if ref and not (ref.force_range[0]
                                    <= pct <= ref.force_range[1]):
                        errors.append(
                            f"{pos}: siła {pct}% poza zakresem "
                            f"{ref.force_range[0]:g}-"
                            f"{ref.force_range[1]:g}%")
                elif cmd is ProcCmd.SET_TARGET_TIME:
                    if step.args.get("seconds", 0) <= 0:
                        errors.append(f"{pos}: czas targetu musi być > 0")

    for _, step in stack:
        errors.append(f"pętla ×{step.count} bez końca")
    return errors


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    from core.pump.base import PumpCapabilities
    from core.pump.demo import DemoDriver

    demo = DemoDriver.capabilities
    al = PumpCapabilities(name="AL-like", rate_units=("mL/h",),
                          rate_min=0.1, rate_max=50.0,
                          directions=("INF", "WDR"),
                          volume_units=("mL", "µL"),
                          target_volume=True,
                          dispensed_query=True,  # <- AL-1000 ma DIS
                          diameter_setting=True,
                          diameter_range=(0.1, 50.0))
    caps = {1: demo, 2: al}

    # 1. poprawna procedura: setup -> 3× (start, wait, stop)
    proc = [
        CommandStep(ProcCmd.SET_DIAMETER, None, {"mm": 14.43}),
        CommandStep(ProcCmd.SET_FORCE, (1,), {"percent": 45}),
        CommandStep(ProcCmd.SET_RATE, None, {"value": 100, "unit": "mL/h"}),
        CommandStep(ProcCmd.SET_DIRECTION, (1,), {"direction": "WDR"}),
        CommandStep(ProcCmd.SET_TARGET_VOLUME, (2,),
                    {"volume": 5.0, "unit": "mL"}),
        LoopStartStep(3),
        CommandStep(ProcCmd.START),
        CommandStep(ProcCmd.WAIT, None, {"seconds": 30}),
        CommandStep(ProcCmd.STOP),
        CommandStep(ProcCmd.CLEAR_DISPENSED),
        LoopEndStep(),
    ]
    assert validate(proc, caps) == [], validate(proc, caps)

    # 2. opisy kroków
    assert describe(LoopStartStep(3)) == "PĘTLA ×3"
    assert describe(LoopEndStep()) == "KONIEC PĘTLI"
    assert describe(CommandStep(ProcCmd.SET_RATE, (1, 2),
                                {"value": 100, "unit": "mL/h"})) == \
        "Ustaw rate: 100 mL/h [1,2]"
    assert describe(CommandStep(ProcCmd.WAIT, None,
                                {"seconds": 30})) == "Czekaj: 30 s"

    # 3. błędy struktury pętli
    assert any("bez końca" in e for e in
               validate([LoopStartStep(2)], caps))
    assert any("bez początku" in e for e in
               validate([LoopEndStep()], caps))
    deep = [LoopStartStep(), LoopStartStep(), LoopStartStep(),
            LoopStartStep(), LoopEndStep(), LoopEndStep(),
            LoopEndStep(), LoopEndStep()]
    assert any("głębiej" in e for e in validate(deep, caps))
    assert any("zakresem" in e for e in
               validate([LoopStartStep(0)], caps))
    assert any("zakresem" in e for e in
               validate([LoopStartStep(1000)], caps))

    # 4. WAIT: zakres i brak czasu
    assert any("poza zakresem" in e for e in validate(
        [CommandStep(ProcCmd.WAIT, None, {"seconds": 0.01})], caps))
    assert any("bez czasu" in e for e in validate(
        [CommandStep(ProcCmd.WAIT, None, {})], caps))

    # 5. pompy: nieistniejący pid, brak capability
    assert any("nie istnieje" in e for e in validate(
        [CommandStep(ProcCmd.START, (9,))], caps))
    assert any("nie wspiera" in e for e in validate(
        [CommandStep(ProcCmd.SET_FORCE, (2,), {"percent": 40})], caps))
    assert any("nie wspiera" in e for e in validate(
        [CommandStep(ProcCmd.PURGE, (2,))], caps))

    # 6. argumenty: brakujący, złe jednostki/kierunek/zakresy
    assert any("bez argumentu" in e for e in validate(
        [CommandStep(ProcCmd.SET_RATE, None, {"value": 1})], caps))
    assert any("spoza listy" in e for e in validate(
        [CommandStep(ProcCmd.SET_RATE, (2,),
                     {"value": 1, "unit": "µL/min"})], caps))
    assert any("spoza listy" in e for e in validate(
        [CommandStep(ProcCmd.SET_TARGET_VOLUME, (2,),
                     {"volume": 1, "unit": "nL"})], caps))
    assert any("niedostępny" in e for e in validate(
        [CommandStep(ProcCmd.SET_DIRECTION, (2,),
                     {"direction": "XXX"})], caps))
    assert any("poza zakresem" in e and "mm" in e for e in validate(
        [CommandStep(ProcCmd.SET_DIAMETER, None, {"mm": 99})], caps))
    assert any("poza zakresem" in e and "%" in e for e in validate(
        [CommandStep(ProcCmd.SET_FORCE, (1,), {"percent": 200})], caps))
    assert any("musi być > 0" in e for e in validate(
        [CommandStep(ProcCmd.SET_TARGET_TIME, (1,), {"seconds": 0})],
        caps))

    # 7. selekcja: puste pidy przy needs_pumps
    assert any("brak pomp" in e for e in validate(
        [CommandStep(ProcCmd.START, ())], caps))

    # 8. jednostka μ greckie normalizowana (jak wszędzie w projekcie)
    ok = [CommandStep(ProcCmd.SET_RATE, (1,),
                      {"value": 500, "unit": "μL/min"})]   # μ U+03BC
    assert validate(ok, caps) == []

    # 9. warunki: opisy, walidacja, bramka STOPPED jako "IF target"
    #    (bez self-importu! klasy są już w zasięgu tego modułu)
    assert describe(ConditionStep(CondCmd.STOPPED, 1)) == \
           "Czekaj aż pompa zatrzymana [pompa 1] (max 60 s)"
    assert describe(ConditionStep(CondCmd.DISPENSED_GE, 2,
                                  {"value": 5, "unit": "mL"},
                                  timeout_s=120.0)) == \
           "Czekaj aż dozowane >= 5 mL [pompa 2] (max 120 s)"

    # procedura "IF target": start, czekaj aż zatrzymana, dopiero dalej
    proc_if = [
        CommandStep(ProcCmd.SET_TARGET_VOLUME, (1,),
                    {"volume": 5.0, "unit": "mL"}),
        CommandStep(ProcCmd.START, (1,)),
        ConditionStep(CondCmd.STOPPED, 1, timeout_s=3600.0),
        CommandStep(ProcCmd.START, (2,)),
    ]
    assert validate(proc_if, caps) == [], validate(proc_if, caps)

    # błędy: pompa nie istnieje / timeout / on_timeout
    assert any("nie istnieje" in e for e in validate(
        [ConditionStep(CondCmd.STOPPED, 9)], caps))
    assert any("timeout poza zakresem" in e for e in validate(
        [ConditionStep(CondCmd.STOPPED, 1, timeout_s=0.01)], caps))
    assert any("on_timeout" in e for e in validate(
        [ConditionStep(CondCmd.STOPPED, 1, on_timeout="skip")], caps))

    # błędy: brak argumentu, zła jednostka
    assert any("bez argumentu" in e for e in validate(
        [ConditionStep(CondCmd.DISPENSED_GE, 1, {"value": 5})], caps))
    assert any("spoza listy" in e for e in validate(
        [ConditionStep(CondCmd.DISPENSED_GE, 1,
                       {"value": 5, "unit": "nL"})], caps))

    # negatywny "nie raportuje": pompa celowo BEZ dispensed_query
    # (al-like ma je od dawna — stara asercja na pid 2 testowała lukę,
    #  która nie istnieje)
    caps_bare = PumpCapabilities(name="Bare", rate_units=("mL/h",),
                                 rate_min=0.1, rate_max=50.0)
    caps3 = dict(caps)
    caps3[3] = caps_bare
    assert any("nie raportuje" in e for e in validate(
        [ConditionStep(CondCmd.DISPENSED_GE, 3,
                       {"value": 5, "unit": "mL"})], caps3)), \
        "Bare nie ma dispensed_query — walidator ma to łapać"

    # RUNNING/ALARM bez argumentów: przechodzą czysto
    assert validate([ConditionStep(CondCmd.RUNNING, 1),
                     ConditionStep(CondCmd.ALARM, 2)], caps) == []


    print("steps: OK")