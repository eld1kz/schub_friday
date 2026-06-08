import logging


logger = logging.getLogger("habit_intelligence")


def metric(event: str, **fields) -> None:
    """Structured log helper. Callers must not pass raw coordinates or message text."""
    safe = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("[habit] event=%s %s", event, safe)
