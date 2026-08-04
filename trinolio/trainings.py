"""Fetching workouts from Nolio."""

from datetime import datetime, timedelta
from typing import Any

from trinolio.auth import get


def fetch_trainings(
    since: str | None = None, athlete_id: int | None = None
) -> list[dict[str, Any]]:
    """Every workout, or everything from `since` on. `athlete_id` reads a managed athlete's."""
    # There is no cursor and no total, so the `to` bound walks backwards a page at a time.
    # Nolio's range is inclusive at both ends, so consecutive pages overlap by a date: workouts
    # are keyed by id, and a page holding nothing new is the end of the history.
    by_id: dict[int, dict[str, Any]] = {}
    to_date = None
    while True:
        params: dict[str, str | int] = {"limit": 300}
        if since:
            params["from"] = since
        if athlete_id:
            params["athlete_id"] = athlete_id
        if to_date:
            params["to"] = to_date
        batch = get("get/training", **params)
        if not batch:
            break
        unseen = [record for record in batch if record["nolio_id"] not in by_id]
        if not unseen:
            break
        by_id.update({record["nolio_id"]: record for record in unseen})
        oldest = min(record["date_start"] for record in batch)
        next_to = (datetime.strptime(oldest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        # A page of 300 workouts sharing one date would otherwise ask for it forever.
        if next_to == to_date:
            break
        to_date = next_to
    return list(by_id.values())
