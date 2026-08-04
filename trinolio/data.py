"""The stored parquet read back with Nolio's encodings undone.

Facts about how Nolio encodes things, never analysis: a weekly load or a fitness curve is a
question, and questions belong in a notebook.
"""

import json
from typing import Any

import polars as pl

from trinolio import store

# Nolio writes 0, never null, for what it did not record. `duration`, `distance` and
# `elevation_*` are deliberately absent: 0 is a real measurement on a treadmill run or a gym
# session, so nulling them would invent missing data.
ZERO_MEANS_ABSENT = (
    "rpe",
    "feeling",
    "kilojoules",
    "avg_watt",
    "max_watt",
    "np",
    "load_foster",
    "load_coggan",
    "rest_hr_user",
    "max_hr_user",
    "ftp",
    "rftp",
    "weight",
    "critical_power",
    "wbal",
)

# Transcribed from the wiki's `Training-Object.md`, since no endpoint enumerates sports. 75 is
# absent even from there and read off the localized `sport`, so it is the one entry backed by
# inference rather than documentation.
SPORTS = {
    2: "Running",
    3: "XC ski - Classic",
    4: "XC ski - Skating",
    5: "Roller ski - Classic",
    6: "Roller ski - Skating",
    7: "Ski Mountaineering",
    8: "Climbing",
    10: "Bodybuilding",
    12: "Other",
    14: "Road cycling",
    15: "Mountain cycling",
    16: "Hiking",
    18: "Virtual ride",
    19: "Swimming",
    20: "Strength",
    21: "Stretching",
    24: "Treadmill",
    26: "Kayaking - Sea",
    27: "Kayaking - River",
    28: "Elliptical trainer",
    29: "Walking sticks",
    30: "Yoga",
    31: "Canoe - Sea",
    32: "Canoe - River",
    33: "Rowing",
    34: "Orienteering race",
    35: "Track cycling",
    36: "CX cycling",
    37: "Squash",
    38: "Biathlon",
    45: "Walking",
    51: "Stand up paddle",
    52: "Trail running",
    53: "OCR running",
    59: "Tennis",
    75: "Alpine ski",
}

# `other` is a volume bucket and not a discard: tennis and hiking each outweigh swimming in
# hours. Splitting it further is a question, and questions are a `sport_en` filter at the call
# site. Sports never logged are mapped anyway, so the first one lands in a total.
DISCIPLINES = {
    (2, "Running"): "run",
    (24, "Treadmill"): "run",
    (52, "Trail running"): "run",
    (53, "OCR running"): "run",
    (14, "Road cycling"): "bike",
    (15, "Mountain cycling"): "bike",
    (18, "Virtual ride"): "bike",
    (35, "Track cycling"): "bike",
    (36, "CX cycling"): "bike",
    (19, "Swimming"): "swim",
    (3, "XC ski - Classic"): "other",
    (4, "XC ski - Skating"): "other",
    (5, "Roller ski - Classic"): "other",
    (6, "Roller ski - Skating"): "other",
    (7, "Ski Mountaineering"): "other",
    (8, "Climbing"): "other",
    (10, "Bodybuilding"): "other",
    (12, "Other"): "other",
    (16, "Hiking"): "other",
    (20, "Strength"): "other",
    (21, "Stretching"): "other",
    (26, "Kayaking - Sea"): "other",
    (27, "Kayaking - River"): "other",
    (28, "Elliptical trainer"): "other",
    (29, "Walking sticks"): "other",
    (30, "Yoga"): "other",
    (31, "Canoe - Sea"): "other",
    (32, "Canoe - River"): "other",
    (33, "Rowing"): "other",
    (34, "Orienteering race"): "other",
    (37, "Squash"): "other",
    (38, "Biathlon"): "other",
    (45, "Walking"): "other",
    (51, "Stand up paddle"): "other",
    (59, "Tennis"): "other",
    (75, "Alpine ski"): "other",
}

# Guards two hand-typed tables against each other: a sport added to one and not the other
# would read as a null `discipline` and be mistaken for an unknown API id. It cannot see a
# rename upstream, since Nolio never sends an English name.
if set(DISCIPLINES) != set(SPORTS.items()):
    raise RuntimeError(
        f"DISCIPLINES and SPORTS disagree on {set(DISCIPLINES) ^ set(SPORTS.items())}"
    )

DISCIPLINE_BY_ID = {sport_id: discipline for (sport_id, _), discipline in DISCIPLINES.items()}

# Nolio's type strings are magic numbers in string form: `paradoxicalsleep` is REM,
# `caloriesaurepos` resting calories, `scoresommeil` the sleep score, `hrrest` resting HR.
METRIC_NAMES = {
    "awaketime": "awake_time",
    "calories": "calories",
    "caloriesactive": "active_calories",
    "caloriesaurepos": "resting_calories",
    "deepsleep": "deep_sleep",
    "garminbodybattery": "body_battery",
    "hrrest": "resting_hr",
    "lightsleep": "light_sleep",
    "numberofsteps": "steps",
    "paradoxicalsleep": "rem_sleep",
    "scoresommeil": "sleep_score",
    "sleep": "sleep",
    "vo2max": "vo2max",
    "weight": "weight",
}

ZONE_ROWS = pl.Schema(
    {
        "nolio_id": pl.Int64,
        "date_start": pl.Date,
        "sport_id": pl.Int64,
        "channel": pl.String,
        "min": pl.Float64,
        "max": pl.Float64,
        "duration": pl.Int64,
    }
)


def trainings() -> pl.DataFrame:
    """Every stored workout: unrecorded values nulled, `sport_en` and `discipline` added.

    Group on `sport_en` or `discipline` (`run`, `bike`, `swim`, `other`) rather than the
    localized `sport`. Both are null for an id neither map knows, so an unrecognised sport shows
    up as unclassified instead of landing in the wrong total.

    `weight` is renamed `weight_profile`, because it is the profile weight stamped onto every
    workout: one value with no variance, which averages into a weight trend that was never
    measured. The measured series is `wellness()` type `weight`.

    `distance` is noise for sports that do not travel, so a global sum wants a `discipline`
    filter first.
    """
    return (
        pl.read_parquet(store.TRAININGS)
        .with_columns(
            *(pl.col(column).replace(0, None) for column in ZERO_MEANS_ABSENT),
            sport_en=pl.col("sport_id").replace_strict(
                SPORTS, default=None, return_dtype=pl.String
            ),
            discipline=pl.col("sport_id").replace_strict(
                DISCIPLINE_BY_ID, default=None, return_dtype=pl.String
            ),
        )
        .rename({"weight": "weight_profile"})
    )


def wellness() -> pl.DataFrame:
    """The archive and Nolio's connector as one series, plus `metric` and `sleep_complete`.

    The connector wins wherever the two overlap, `source` keeping provenance readable. `metric`
    is the readable name of `type`, which stays because it is what round-trips to the API.

    `sleep_complete` is false on nights whose `sleep` total is missing REM, and on those nights
    the total is deep plus light exactly, so a trend crossing them steps for no physiological
    reason. Units are as stored, named by the `unit` column.
    """
    live = (
        pl.read_parquet(store.METRICS)
        .sort("date", "type", "id")
        # Nolio serves the same day twice under two ids. Only the values tell a duplicate from
        # a genuine second measurement, so two rows that disagree are both kept.
        .unique(subset=["date", "type", "value"], keep="first", maintain_order=True)
    )
    # The connector wins wherever the two overlap: it is the live feed, the archive is frozen.
    archive = pl.read_parquet(store.WELLNESS).join(
        live.select("date", "type"), on=["date", "type"], how="anti"
    )
    rows = pl.concat([live, archive], how="diagonal").sort("date", "type")
    # Per night rather than against the date REM first appears: Garmin also misses it on
    # scattered nights since, which would otherwise be called complete. Imploded because
    # `is_in` against a bare Series of the same dtype is ambiguous, and polars stopped guessing.
    rem_nights = rows.filter(pl.col("type") == "paradoxicalsleep").get_column("date").implode()
    return rows.with_columns(
        metric=pl.col("type").replace_strict(METRIC_NAMES, default=None, return_dtype=pl.String),
        sleep_complete=pl.when(pl.col("type") == "sleep").then(pl.col("date").is_in(rem_nights)),
    )


def ladder(buckets: pl.DataFrame) -> pl.DataFrame:
    """The distinct buckets of each channel, numbered 1..n by ascending floor."""
    # Derived rather than declared: the boundaries are the athlete's own zone settings, so a
    # constant here would freeze one account's configuration into the library.
    numbered = (
        buckets.select("channel", "min", "max")
        .unique()
        .sort("channel", "min")
        .with_columns(zone=pl.col("min").rank("dense").over("channel").cast(pl.Int64))
    )
    # Strictly less, because heart rate buckets meet exactly (164 closes one and opens the
    # next) while watts leave a unit between them (99 then 100).
    overlapping = numbered.filter(pl.col("min") < pl.col("max").shift().over("channel"))
    if overlapping.height:
        raise RuntimeError(
            "zone boundaries overlap, so the ladder was redefined mid-history and cannot be "
            f"numbered as one: {overlapping.select('channel', 'min', 'max').rows()}"
        )
    return numbered


def zones() -> pl.DataFrame:
    """Time in zone as long rows, one per workout, channel and bucket, numbered by `zone`.

    `zone` is derived by `ladder()`, which raises if the boundaries overlap.

    Know what it counts before trusting it across years: each bucket was measured against
    whatever the athlete's zone settings were at the time, and Nolio does not recompute history
    when they change.
    """
    columns = ["nolio_id", "date_start", "sport_id", "zones"]
    stored = pl.read_parquet(store.TRAININGS, columns=columns)
    # Parsed in Python rather than with `str.json_decode`, which needs one dtype for the whole
    # column: the channels and the bucket count both vary per workout, so a fixed struct would
    # quietly drop whatever it was not told about.
    rows: list[dict[str, Any]] = [
        {
            "nolio_id": nolio_id,
            "date_start": date_start,
            "sport_id": sport_id,
            "channel": channel,
            **bucket,
        }
        for nolio_id, date_start, sport_id, raw in stored.iter_rows()
        for channel, buckets in json.loads(raw).items()
        for bucket in buckets
    ]
    parsed = pl.DataFrame(rows, schema=ZONE_ROWS, strict=False)
    return parsed.join(ladder(parsed), on=["channel", "min", "max"], how="left").sort(
        "date_start", "channel", "min"
    )
