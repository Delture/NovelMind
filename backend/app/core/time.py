"""UTC timestamp helpers shared by persistence code."""

from datetime import datetime, timezone


def utc_now() -> datetime:
    """Return naive UTC for SQLite while avoiding deprecated utcnow()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
