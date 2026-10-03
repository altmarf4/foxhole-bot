import time


def now() -> int:
    return int(time.time())


def relative(ts: int) -> str:
    return f"<t:{int(ts)}:R>"


def full(ts: int) -> str:
    return f"<t:{int(ts)}:f>"


def short_date(ts: int) -> str:
    return f"<t:{int(ts)}:d>"


def hours_to_seconds(hours: float) -> int:
    return int(round(hours * 3600))


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_hours(hours: float) -> str:
    if float(hours).is_integer():
        return f"{int(hours)}h"
    return f"{hours:g}h"
