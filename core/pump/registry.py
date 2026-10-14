from core.pump.models.al1000 import AL1000Driver
from core.pump.demo import DemoDriver

REGISTRY = {
    "WPI AL-1000": AL1000Driver,
    "Pompa demo (symulator)": DemoDriver,
    # "Model B": BDriver,      <- dopiszesz, gdy poznasz protokół
    # "Model C": CDriver,
}