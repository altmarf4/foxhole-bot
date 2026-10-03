STOCKPILE_LIFETIME_HOURS = 50
SHIP_LIFETIME_HOURS = 48

DEFAULT_ALERT_THRESHOLDS = [12.0, 6.0, 2.0, 1.0]
DEFAULT_STORAGE_TYPES = ["Storage Depot", "Seaport", "Aircraft Depot"]
DEFAULT_SHIP_TYPES = ["Destroyer", "Submarine", "Battleship", "Bluefin", "Bowhead", "Longhook", "Carrier"]

PRIORITIES = {
    "High": 0xE67E22,
    "Medium": 0xF1C40F,
    "Low": 0x2ECC71,
}
PRIORITY_ORDER = {name: index for index, name in enumerate(PRIORITIES)}

RARES_STATUS = [
    (0, "Empty", 0x95A5A6),
    (50, "Low", 0xE74C3C),
    (200, "Moderate", 0xF1C40F),
]
RARES_STOCKED = ("Stocked", 0x2ECC71)

TICKET_STATUSES = {
    "Open": 0x95A5A6,
    "Payment Pending": 0xF1C40F,
    "Paid": 0x2ECC71,
    "In Production": 0x3498DB,
    "Delivered": 0x1ABC9C,
    "Cancelled": 0xE74C3C,
}
TICKET_QUANTITY_MAX = 25

SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024
PANEL_TIMEOUT = 900


class Colour:
    INFO = 0x3498DB
    SUCCESS = 0x2ECC71
    WARNING = 0xF1C40F
    ORANGE = 0xE67E22
    DANGER = 0xE74C3C
    CRITICAL = 0x992D22
    MUTED = 0x95A5A6
    DARK = 0x2C2F33
    PRIVATE = 0x8E44AD
    GOLD = 0xF1C40F
