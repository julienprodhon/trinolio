"""The local parquet files, and the schemas that keep them stable across syncs.

Nolio sends the same field as an int for one workout and a float for the next
(`elevation_gain`, `load_foster`, `avg_watt`…), so dtypes are pinned here instead of
inferred per batch, which would otherwise make each sync unappendable to the last.
Missing keys become nulls, so a field Nolio adds or drops does not break a sync.
"""

import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

import polars as pl


def find_data() -> Path:
    """$TRINOLIO_DATA, else `data/` beside a source checkout, else `data/` under the cwd."""
    # Read at import like `auth.ENV`, so it is a real environment variable and not a `.env`
    # key: `load_env` runs too late to influence it. The checkout probe is what keeps an
    # installed copy from writing its warehouse into site-packages.
    if override := os.environ.get("TRINOLIO_DATA"):
        return Path(override)
    checkout = Path(__file__).parent.parent
    if (checkout / "pyproject.toml").is_file():
        return checkout / "data"
    return Path.cwd() / "data"


DATA = find_data()
TRAININGS = DATA / "trainings.parquet"
METRICS = DATA / "metrics.parquet"
WELLNESS = DATA / "wellness.parquet"
# One original activity file per workout, named by `nolio_id`
FILES = DATA / "files"

# Wide enough for two uploads of one ride disagree by, short enough that a brick falls outside
DUPLICATE_WINDOW = timedelta(minutes=5)

# Two recordings of one session agree on duration and distance to within 2%. Different sessions
# sharing a rewritten start time are 19% apart or more, so 5% sits in an empty gap.
DUPLICATE_TOLERANCE = 0.05

# Dates arrive as strings and are parsed once the frame is built. `file_url` is deliberately
# absent: it is a CDN link that expires within the hour
TRAININGS_RAW = pl.Schema(
    {
        "nolio_id": pl.Int64,
        "name": pl.String,
        "sport": pl.String,
        "sport_id": pl.Int64,
        "is_competition": pl.Boolean,
        "date_start": pl.String,
        "date_end": pl.String,
        "hour_start": pl.String,
        "duration": pl.Int64,
        "distance": pl.Float64,
        "elevation_gain": pl.Float64,
        "elevation_loss": pl.Float64,
        "rpe": pl.Int64,
        "feeling": pl.Int64,
        "description": pl.String,
        "kilojoules": pl.Float64,
        "avg_watt": pl.Float64,
        "max_watt": pl.Float64,
        "np": pl.Float64,
        "load_foster": pl.Float64,
        "load_coggan": pl.Float64,
        "rest_hr_user": pl.Int64,
        "max_hr_user": pl.Int64,
        "ftp": pl.Float64,
        "rftp": pl.Float64,
        "weight": pl.Float64,
        "critical_power": pl.Float64,
        "wbal": pl.Float64,
        "start_latitude": pl.Float64,
        "start_longitude": pl.Float64,
        "start_city": pl.String,
        "zones": pl.String,
    }
)

METRICS_RAW = pl.Schema(
    {
        "id": pl.Int64,
        "date": pl.String,
        "hour": pl.String,
        "type": pl.String,
        "value": pl.Float64,
        "unit": pl.String,
        "source": pl.String,
    }
)


class MetricRow(TypedDict):
    """A wellness point as `metrics.fetch_metrics` builds it."""

    id: int
    date: str
    hour: str | None
    type: str
    value: float
    unit: str | None
    source: str | None


# The metrics schema minus Nolio's `id` and `hour`
WELLNESS_RAW = pl.Schema(
    {
        "date": pl.String,
        "type": pl.String,
        "value": pl.Float64,
        "unit": pl.String,
        "source": pl.String,
    }
)


class WellnessRow(TypedDict):
    """An archive value as `wellness.read_archive` builds it."""

    date: str
    type: str
    value: float
    unit: str | None
    source: str


def trainings_frame(records: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """Workouts as stored: API units kept, `zones` as JSON, one derived `start_local`."""
    rows: list[dict[str, Any]] = []
    for record in records:
        row = {key: record.get(key) for key in TRAININGS_RAW}
        # Nolio orders the channels differently from one call to the next
        row["zones"] = json.dumps(record.get("zones") or {}, sort_keys=True)
        rows.append(row)
    return pl.DataFrame(rows, schema=TRAININGS_RAW, strict=False).with_columns(
        pl.col("date_start").str.to_date("%Y-%m-%d"),
        # Empty on every workout so far, so the format cannot be inferred, only given.
        pl.col("date_end").str.to_date("%Y-%m-%d", strict=False),
        # Local wall clock, not UTC. Null when Nolio has no hour for the workout.
        start_local=pl.concat_str("date_start", "hour_start", separator=" ").str.to_datetime(
            "%Y-%m-%d %H:%M:%S", strict=False
        ),
    )


def metrics_frame(records: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(records, schema=METRICS_RAW, strict=False).with_columns(
        pl.col("date").str.to_date("%Y-%m-%d")
    )


def wellness_frame(records: Sequence[Mapping[str, Any]]) -> pl.DataFrame:
    """One value per (date, type), the higher of the two where a day arrives twice."""
    # Garmin's export files overlap on their boundary dates, so most of those duplicates are
    # identical and the disagreements are only ever vo2max.
    return (
        pl.DataFrame(records, schema=WELLNESS_RAW, strict=False)
        .with_columns(pl.col("date").str.to_date("%Y-%m-%d"))
        .group_by("date", "type")
        .agg(pl.col("value").max(), pl.col("unit").first(), pl.col("source").first())
        .sort("date", "type")
    )


def newest(path: Path, column: str) -> date | None:
    """The latest value of `column` already stored, or None if there is no file yet."""
    if not path.exists():
        return None
    latest = pl.read_parquet(path, columns=[column])[column].max()
    return latest if isinstance(latest, date) else None


def write(path: Path, frame: pl.DataFrame) -> None:
    """Write then rename, so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    frame.write_parquet(temporary, compression="zstd")
    temporary.replace(path)


def agrees_within(column: str, previous: str, tolerance: float) -> pl.Expr:
    """Whether the two columns are within `tolerance` of each other, relatively.

    True when either is null, since a pair that cannot be compared still wants a human, and
    when both are zero, which is how Nolio stores distance for the sports that have none.
    """
    larger = pl.max_horizontal(column, previous)
    return ((pl.col(column) - pl.col(previous)).abs() / larger <= tolerance).fill_null(True) | (
        larger == 0
    )


def duplicate_candidates(
    path: Path, window: timedelta = DUPLICATE_WINDOW, tolerance: float = DUPLICATE_TOLERANCE
) -> pl.DataFrame:
    """Consecutive workouts of one sport starting within `window` of each other and agreeing
    on duration and distance to within `tolerance`, for review.

    Reported, never merged: a brick session and the legs of a multisport race have that same
    shape, so telling them from a double upload needs a human.

    The start time alone is not enough. Nolio rewrites it on a hand upload, hour included, so
    everything imported that way clusters on times it never had and unrelated sessions collide.
    Duration and distance come from the file and survive, which is what separates them.
    """
    # A start-time window rather than (date, duration, sport), which cannot see the known miss
    # by construction, an indoor ride recorded twice a minute apart, and finds nothing at all
    # on this archive.
    columns = ["nolio_id", "name", "sport_id", "start_local", "duration", "distance"]
    stored = (
        pl.read_parquet(path, columns=columns)
        .filter(pl.col("start_local").is_not_null())
        .sort("sport_id", "start_local")
    )
    # Pairwise on the sorted neighbour rather than a self-join: duplicates are adjacent in time
    # by definition, and a run of three reports as two overlapping pairs.
    return (
        stored.with_columns(
            previous_id=pl.col("nolio_id").shift().over("sport_id"),
            previous_name=pl.col("name").shift().over("sport_id"),
            previous_start=pl.col("start_local").shift().over("sport_id"),
            previous_duration=pl.col("duration").shift().over("sport_id"),
            previous_distance=pl.col("distance").shift().over("sport_id"),
        )
        .filter(
            pl.col("start_local") - pl.col("previous_start") < window,
            agrees_within("duration", "previous_duration", tolerance),
            agrees_within("distance", "previous_distance", tolerance),
        )
        .select(
            "sport_id",
            "previous_id",
            "nolio_id",
            "previous_start",
            "start_local",
            "previous_duration",
            "duration",
            "previous_name",
            "name",
        )
    )


class Upserted(NamedTuple):
    added: int
    changed: int
    unchanged: int
    changed_columns: list[str]
    removed: pl.DataFrame


def differing_columns(stored: pl.DataFrame, fresh: pl.DataFrame, key: str) -> list[str]:
    """The columns where at least one stored row disagrees with its refetched version."""
    paired = stored.join(fresh, on=key, suffix="_fresh")
    return [
        column
        for column in stored.columns
        if column != key and not paired[column].eq_missing(paired[f"{column}_fresh"]).all()
    ]


def upsert(
    path: Path, fresh: pl.DataFrame, key: str, sort: str, mirrored: pl.Expr | None = None
) -> Upserted:
    """Replace stored rows that were refetched, keep the rest, write only if anything moved.

    `mirrored` selects the stored rows the fetch was complete for: any of them it did not
    return was deleted upstream, and is dropped.
    """
    if not path.exists():
        write(path, fresh.sort(sort))
        return Upserted(fresh.height, 0, 0, [], fresh.clear())

    stored = pl.read_parquet(path)
    refetched = stored.join(fresh.select(key), on=key, how="semi")
    unchanged = refetched.join(fresh, on=fresh.columns, how="semi", nulls_equal=True)
    changed = refetched.join(unchanged.select(key), on=key, how="anti")
    removed = (
        stored.filter(mirrored).join(fresh.select(key), on=key, how="anti")
        if mirrored is not None
        else stored.clear()
    )
    added = fresh.height - refetched.height

    if added or changed.height or removed.height:
        kept = stored.join(pl.concat([fresh.select(key), removed.select(key)]), on=key, how="anti")
        write(path, pl.concat([kept, fresh], how="vertical").sort(sort))
    return Upserted(
        added, changed.height, unchanged.height, differing_columns(changed, fresh, key), removed
    )
