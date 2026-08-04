"""Pushing archive wellness back into Nolio, one value per request.

`/update/metric/` takes exactly one metric per POST, so thousands of archive values against a
~200/hour limit make this a trickle job measured in days.
"""

import json
from datetime import date

import httpx
import polars as pl

from trinolio import store
from trinolio.auth import BASE_URL, token

METRIC_IDS = {"sleep": 1, "weight": 2, "vo2max": 8, "hrrest": 9}
DEFAULT_TYPES = ("hrrest", "sleep", "weight")

PUSHED = store.DATA / "pushed.jsonl"

# The dev tier allows ~200 requests/hour, kept a little under.
INTERVAL = 20.0

SENT = pl.Schema({"type": pl.String, "date": pl.String, "nolio_id": pl.Int64})


def sent() -> pl.DataFrame:
    """The (type, date) pairs already accepted by Nolio, from the append-only log."""
    # JSONL rather than parquet: the log is appended to after every single POST and has to
    # survive the run being killed, and rewriting a parquet each time would be both slow and a
    # window in which the file is corrupt.
    lines = PUSHED.read_text().splitlines() if PUSHED.exists() else []
    rows = [json.loads(line) for line in lines if line.strip()]
    return pl.DataFrame(rows, schema=SENT, strict=False).with_columns(
        pl.col("date").str.to_date("%Y-%m-%d")
    )


def nolio_start() -> date | None:
    """Where Nolio's own wellness begins, which is where the backfill has to stop.

    `/update/metric/` upserts, so pushing past it would overwrite live values with archive ones.
    """
    if not store.METRICS.exists():
        return None
    earliest = pl.read_parquet(store.METRICS, columns=["date"])["date"].min()
    return earliest if isinstance(earliest, date) else None


def pending(types: list[str], until: date | None) -> pl.DataFrame:
    """Archive values still owed to Nolio, oldest first."""
    rows = pl.read_parquet(store.WELLNESS).filter(
        pl.col("type").is_in(types),
        # Nolio rejects a non-positive `new_value` with a 400.
        pl.col("value") > 0,
    )
    if until:
        rows = rows.filter(pl.col("date") < until)
    return rows.join(sent(), on=["type", "date"], how="anti").sort("date", "type")


def send(metric_type: str, value: float, day: date) -> int:
    """Send one value. Returns the id of the Nolio metric it created or updated."""
    response = httpx.post(
        f"{BASE_URL}/update/metric/",
        headers={"Authorization": f"Bearer {token()}"},
        json={
            "metric_id": METRIC_IDS[metric_type],
            "new_value": value,
            "date_start": day.strftime("%Y-%m-%d"),
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["metric_id"]


def mark_sent(metric_type: str, day: date, nolio_id: int) -> None:
    entry = {"type": metric_type, "date": str(day), "nolio_id": nolio_id}
    with PUSHED.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
