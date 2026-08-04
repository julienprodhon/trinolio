"""The Garmin archive's wellness JSON, read into the long shape Nolio metrics use.

Nolio's wellness starts the day a connector is linked, so every earlier night and morning
exists only in the athlete's own export, read here from `data-archive/wellness/`. Types are
named with Nolio's own vocabulary (`hrrest`, `paradoxicalsleep`…) so this table and
`metrics.parquet` union without a mapping in between.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict

from trinolio.store import WellnessRow


class Reading(TypedDict):
    """One archive value, before the unit and provenance every stored row carries."""

    date: str
    type: str
    value: float


def find_archive() -> Path:
    """$TRINOLIO_ARCHIVE, else the export sitting next to a source checkout, else the cwd's."""
    # Same import-time read as `store.find_data`. The archive lives outside the package by
    # definition, so an installed copy has nothing sensible to guess and falls back to the cwd.
    if override := os.environ.get("TRINOLIO_ARCHIVE"):
        return Path(override)
    checkout = Path(__file__).parent.parent
    root = checkout.parent if (checkout / "pyproject.toml").is_file() else Path.cwd()
    return root / "data-archive" / "wellness"


ARCHIVE = find_archive()

UNITS = {
    "hrrest": "bpm",
    "sleep": "seconds",
    "deepsleep": "seconds",
    "lightsleep": "seconds",
    "paradoxicalsleep": "seconds",
    "awaketime": "seconds",
    "scoresommeil": None,
    "vo2max": "ml/min/kg",
    "weight": "kg",
    "calories": "kCal",
    "caloriesactive": "kCal",
    "caloriesaurepos": "kCal",
    "numberofsteps": None,
}

DAILY_SUMMARY_FIELDS = {
    "restingHeartRate": "hrrest",
    "totalKilocalories": "calories",
    "activeKilocalories": "caloriesactive",
    "bmrKilocalories": "caloriesaurepos",
    "totalSteps": "numberofsteps",
}

SLEEP_STAGE_FIELDS = {
    "deepSleepSeconds": "deepsleep",
    "lightSleepSeconds": "lightsleep",
    "remSleepSeconds": "paradoxicalsleep",
    "awakeSleepSeconds": "awaketime",
}

# Nolio's `sleep` is time asleep, so the awake portion of the window is left out, and older
# exports carry no REM, hence a sum over whichever stages the night actually has.
ASLEEP_FIELDS = ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")


def _records(folder: str, pattern: str = "*.json") -> Iterator[dict[str, Any]]:
    for path in sorted((ARCHIVE / folder).glob(pattern)):
        yield from json.loads(path.read_text())


def _daily_summary() -> Iterator[Reading]:
    for record in _records("daily-summary"):
        for field, name in DAILY_SUMMARY_FIELDS.items():
            if record.get(field):
                yield {"date": record["calendarDate"], "type": name, "value": record[field]}


def _sleep() -> Iterator[Reading]:
    for record in _records("sleep"):
        date = record["calendarDate"]
        for field, name in SLEEP_STAGE_FIELDS.items():
            if record.get(field) is not None:
                yield {"date": date, "type": name, "value": record[field]}
        if asleep := sum(record.get(field) or 0 for field in ASLEEP_FIELDS):
            yield {"date": date, "type": "sleep", "value": asleep}
        if score := (record.get("sleepScores") or {}).get("overallScore"):
            yield {"date": date, "type": "scoresommeil", "value": score}


def _vo2max() -> Iterator[Reading]:
    for record in _records("vo2max"):
        if record.get("vo2MaxValue"):
            yield {"date": record["calendarDate"], "type": "vo2max", "value": record["vo2MaxValue"]}


def _weight() -> Iterator[Reading]:
    """Garmin stores weight in grams, and only on the days it was entered by hand."""
    for record in _records("biometrics", "*_userBioMetrics.json"):
        weight = (record.get("weight") or {}).get("weight")
        if weight:
            date = record["metaData"]["calendarDate"][:10]
            yield {"date": date, "type": "weight", "value": weight / 1000}


def read_archive() -> list[WellnessRow]:
    """Every wellness value in the archive, as long rows keyed by Nolio's type names."""
    # Garmin's export splits each folder into ~100-day files overlapping on their boundary
    # date, so a day can arrive twice. `store.wellness_frame` is what deduplicates.
    readings = [*_daily_summary(), *_sleep(), *_vo2max(), *_weight()]
    return [
        {**reading, "unit": UNITS[reading["type"]], "source": "garmin-archive"}
        for reading in readings
    ]
