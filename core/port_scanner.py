"""Skaner portów szeregowych (przejściówki USB->RS-232).

API:
    scan_ports(usb_only=True) -> list[PortInfo]   pełne metadane
    scan_port_names() -> list[str]                same nazwy (do combo)

PortInfo.serial_number jest kluczem do stabilnego mapowania pomp
między restartami (numer COM potrafi się zmienić, S/N przejściówki
nie). Uwaga: najtańsze przejściówki CH340 często NIE MAJĄ numeru
seryjnego — wtedy serial_number == "" i stabilne mapowanie po S/N
jest niemożliwe (sygnalizujemy to w UI etapu 12).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from serial.tools.list_ports import comports as _comports


@dataclass(frozen=True)
class PortInfo:
    device: str            # "COM3" / "/dev/ttyUSB0"
    description: str
    vid: int | None
    pid: int | None
    serial_number: str     # "" gdy przejściówka go nie ma
    hwid: str

    @property
    def label(self) -> str:
        """Tekst do combossa: 'COM3 — USB Serial Port (S/N: ...)'."""
        sn = f", S/N: {self.serial_number}" if self.serial_number else ""
        return f"{self.device} — {self.description}{sn}"


def _sort_key(p: PortInfo):
    """COM2 przed COM10; poza tym porządek alfabetyczny prefiksów."""
    m = re.search(r"^(.*?)(\d+)$", p.device)
    if m:
        return (m.group(1), int(m.group(2)), "")
    return (p.device, -1, "")


def _to_info(p) -> PortInfo:
    return PortInfo(
        device=p.device,
        description=(p.description or "").strip(),
        vid=p.vid,
        pid=p.pid,
        serial_number=(p.serial_number or "").strip(),
        hwid=p.hwid or "",
    )


def scan_ports(usb_only: bool = True, source=None) -> list[PortInfo]:
    """Wykryte porty; usb_only odrzuca wewnętrzne porty płyt (VID=None).

    source: wewnętrzny seam testowy — funkcja zwracająca porty
    (domyślnie pyserial comports). Testy wstrzykują fake BEZ
    monkeypatchowania modułu (python -m tworzy osobne obiekty
    __main__ i core.port_scanner — patch globala trafia w złą kopię).
    """
    src = _comports if source is None else source
    infos = [_to_info(p) for p in src()]
    if usb_only:
        infos = [i for i in infos if i.vid is not None]
    return sorted(infos, key=_sort_key)


def scan_port_names(usb_only: bool = True, source=None) -> list[str]:
    return [p.device for p in scan_ports(usb_only=usb_only, source=source)]


# ---------------------------------------------------------------- testy
if __name__ == "__main__":
    class FakePort:                     # imitacja ListPortInfo
        def __init__(self, device, desc="", vid=None, pid=None, sn=None,
                     hwid=""):
            self.device, self.description = device, desc
            self.vid, self.pid = vid, pid
            self.serial_number, self.hwid = sn, hwid

    fake = [
        FakePort("COM10", "USB Serial Port", vid=0x1A86, pid=0x7523,
                 sn=None, hwid="USB VID:PID=1A86:7523"),   # CH340, brak S/N
        FakePort("COM2", "USB Serial Port", vid=0x0403, pid=0x6001,
                 sn="A50285BI", hwid="USB VID:PID=0403:6001 SER=A50285BI"),
        FakePort("COM7", "", vid=None, hwid="ACPI\\PNP0501\\0"),  # wewn.
        FakePort("/dev/ttyUSB1", "FTDI", vid=0x0403, pid=0x6001, sn="FTXYZ",
                 hwid="USB VID:PID=0403:6001"),
        FakePort("COM2B", "FTDI", vid=0x0403, pid=0x6001, sn="Q1",
                 hwid="x"),                             # bez końcówki cyfrowej
    ]

    fake_src = lambda: fake

    ports = scan_ports(source=fake_src)

    # filtr vid=None (COM7) wyrzucony; porządek z _sort_key:
    # "/dev/ttyUSB" < "COM" (ASCII 0x2F < 0x43), "COM" < "COM2B", 2 < 10
    assert [p.device for p in ports] == \
        ["/dev/ttyUSB1", "COM2", "COM10", "COM2B"], ports

    assert ports[1].serial_number == "A50285BI"          # COM2, FTDI
    assert ports[1].label == "COM2 — USB Serial Port, S/N: A50285BI"

    ch340 = ports[2]                                    # COM10, CH340
    assert ch340.serial_number == ""
    assert ch340.label == "COM10 — USB Serial Port"     # bez segmentu S/N

    assert ports[3].label == "COM2B — FTDI, S/N: Q1"

    # usb_only=False: wewnętrzny COM7 wraca (vid None, ale hwid ma treść)
    all_ports = scan_ports(usb_only=False, source=fake_src)
    assert len(all_ports) == 5

    names = scan_port_names(source=fake_src)
    assert names == ["/dev/ttyUSB1", "COM2", "COM10", "COM2B"]

    # sanity: realny skan na tej maszynie (czysto informacyjnie)
    real_ports = scan_ports()
    print(f"port_scanner: OK  (w systemie: {len(real_ports)} portów USB)")
    for p in real_ports:
        print(f"  {p.label}")