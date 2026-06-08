from habit_config import CONFIG


def cleanup_location_retention(repo, user_id: str | None = None) -> None:
    repo.cleanup_retention(user_id, CONFIG.location_retention_days)
