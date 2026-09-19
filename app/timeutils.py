"""timeutils components."""

from datetime import datetime, timezone
from typing import Optional


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now()).isoformat()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
