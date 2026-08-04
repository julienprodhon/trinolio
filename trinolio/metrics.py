"""Fetching wellness metrics from Nolio.

`/get/metric/` needs a metric id, so `/get/user/meta/` is the only usable listing endpoint:
one request returns every type at once, grouped by type. The types are whatever the connectors
happen to send (Garmin currently sends 14, only 4 of them documented), so they are discovered
here rather than declared.
"""

from trinolio.auth import get
from trinolio.store import MetricRow

# `limit` applies per metric type, and the endpoint truncates silently once it is hit.
LIMIT = 300


def fetch_metrics(since: str | None = None) -> list[MetricRow]:
    """Metrics as long rows, flattened out of Nolio's per-type grouping."""
    params: dict[str, str | int] = {"limit": LIMIT}
    if since:
        params["from"] = since
    grouped = get("get/user/meta", **params)
    return [
        {
            "id": point["id"],
            "date": point["date"],
            "hour": point["hour"],
            "type": name,
            "value": point["value"],
            "unit": bucket["unit"] or None,
            "source": point["source"],
        }
        for name, bucket in grouped.items()
        for point in bucket["data"]
    ]
