"""The per-second samples in each workout's original file, one row per FIT `record` message.

Values are what fitdecode reads, scale and offset applied and nothing else undone: positions in
semicircles, running cadence per leg, `speed` and `enhanced_speed` side by side, Zwift's 0 for a
missing heart rate. Garmin's undocumented `unknown_*` fields are skipped.
"""

import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, cast

import fitdecode
import polars as pl

from trinolio import store

# Every named `record` field found across the 2019-2026 history, in FIT's own names and units
SCHEMA = pl.Schema(
    {
        "nolio_id": pl.Int64,
        "timestamp": pl.Datetime("us", "UTC"),
        "position_lat": pl.Int32,
        "position_long": pl.Int32,
        "distance": pl.Float64,
        "speed": pl.Float64,
        "enhanced_speed": pl.Float64,
        "speed_1s": pl.List(pl.Float64),
        "altitude": pl.Float64,
        "enhanced_altitude": pl.Float64,
        "heart_rate": pl.Int16,
        "cadence": pl.Int16,
        "fractional_cadence": pl.Float64,
        "cycle_length16": pl.Float64,
        "cycles": pl.Int16,
        "total_cycles": pl.Int32,
        "power": pl.Int16,
        "accumulated_power": pl.Int32,
        "target_power": pl.Int16,
        "left_right_balance": pl.Int16,
        "left_torque_effectiveness": pl.Float64,
        "right_torque_effectiveness": pl.Float64,
        "left_pedal_smoothness": pl.Float64,
        "right_pedal_smoothness": pl.Float64,
        "resistance": pl.Int16,
        "temperature": pl.Int16,
        "enhanced_respiration_rate": pl.Float64,
    }
)

# Nolio logs a race leg a few seconds after the watch starts the matching session
LEG_TOLERANCE = timedelta(seconds=60)


class Workout(NamedTuple):
    nolio_id: int
    start_local: datetime | None
    duration: int | None


class Session(NamedTuple):
    start: datetime
    end: datetime
    timer: float


class Parsed(NamedTuple):
    samples: pl.DataFrame
    unlisted: set[str]


def value(field: fitdecode.types.FieldData, dtype: pl.DataType) -> Any:
    # fitdecode names some raw values, `left_right_balance` 128 reads "right": keep the number
    raw = field.raw_value if isinstance(field.value, str) else field.value
    # Zwift writes `enhanced_speed` and `enhanced_altitude` twice in one record
    if isinstance(raw, tuple) and not isinstance(dtype, pl.List):
        return raw[0]
    return raw


def leg(sessions: list[Session], offset: timedelta | None, workout: Workout) -> Session | None:
    """The session of a multisport file that is this workout, matched on start and duration."""
    start = workout.start_local
    if offset is None or start is None:
        return None
    late = {s: abs((s.start + offset).replace(tzinfo=None) - start) for s in sessions}
    near = [s for s in sessions if late[s] <= LEG_TOLERANCE]
    if not near:
        return None
    return min(near, key=lambda s: late[s].total_seconds() + abs(s.timer - (workout.duration or 0)))


def parse(path: Path, workout: Workout) -> Parsed:
    """The workout's samples, and the named fields it carries that `SCHEMA` does not."""
    rows: list[dict[str, Any]] = []
    sessions: list[Session] = []
    offset: timedelta | None = None
    unlisted: set[str] = set()
    with warnings.catch_warnings(), fitdecode.FitReader(path) as fit:
        # fitdecode warns on every field and message its profile does not name
        warnings.simplefilter("ignore")
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name == "record":
                row = {}
                for field in frame.fields:
                    if field.name in SCHEMA:
                        row[field.name] = value(field, SCHEMA[field.name])
                    elif field.value is not None and not field.name.startswith("unknown_"):
                        unlisted.add(field.name)
                rows.append(row)
            elif frame.name == "session":
                # fitdecode is untyped, so the FIT profile's types are restated here
                sessions.append(
                    Session(
                        cast("datetime", frame.get_value("start_time")),
                        cast("datetime", frame.get_value("timestamp")),
                        cast("float", frame.get_value("total_timer_time")),
                    )
                )
            elif frame.name == "activity":
                local = frame.get_value("local_timestamp", fallback=None)
                if local is not None:
                    offset = cast("datetime", local) - cast(
                        "datetime", frame.get_value("timestamp")
                    )

    samples = pl.DataFrame(rows, schema=SCHEMA, strict=False).with_columns(
        nolio_id=pl.lit(workout.nolio_id, pl.Int64)
    )
    # Nolio serves a multisport race's whole file for each of its legs
    if len(sessions) > 1:
        session = leg(sessions, offset, workout)
        samples = (
            samples.filter(pl.col("timestamp").is_between(session.start, session.end, "left"))
            if session
            else samples.clear()
        )
    return Parsed(samples, unlisted)


def parsed_ids() -> set[int]:
    if not store.STREAMS.exists():
        return set()
    return set(pl.read_parquet(store.STREAMS, columns=["nolio_id"])["nolio_id"].unique())


def pending(parsed: set[int], workouts: pl.DataFrame) -> list[tuple[Path, Workout]]:
    """The FIT files on disk whose samples are not stored yet, with the workout each belongs to."""
    known = {row[0]: Workout(*row) for row in workouts.select(Workout._fields).iter_rows()}
    return [
        (path, known[int(path.stem)])
        for path in sorted(store.FILES.glob("*.fit"))
        if int(path.stem) not in parsed and int(path.stem) in known
    ]


def save(parsed: list[pl.DataFrame], existing_ids: set[int]) -> int:
    """Add the parsed samples, drop those of workouts gone from Nolio, write if either happened.

    Returns how many workouts were dropped.
    """
    gone = parsed_ids() - existing_ids
    if parsed or gone:
        # The whole table is loaded only here: the id column is enough to know what is pending
        stored = pl.read_parquet(store.STREAMS) if store.STREAMS.exists() else SCHEMA.to_frame()
        kept = stored.filter(pl.col("nolio_id").is_in(gone).not_())
        store.write(store.STREAMS, pl.concat([kept, *parsed]).sort("nolio_id", "timestamp"))
    return len(gone)
