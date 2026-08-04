"""Uploading an athlete's pre-Nolio history, one file per request.

`/upload/file/` takes one base64 encoded FIT or TCX per POST against a ~200/hour limit, so a few
years of history is a trickle job measured in hours, the same shape as `push`. Nolio matches
uploads on `id_partner` and never on their contents, so the key here is a hash of the file: a
re-export of the same activity produces the same key and is refused rather than duplicated.

That key is also the whole of Nolio's duplicate protection, which is why `overlap` exists. A
session already synced by the athlete's own connector carries no `id_partner`, so nothing stops
this from uploading a second copy of it, and there is no delete route to undo one.
"""

import base64
import gzip
import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict

import httpx
import polars as pl

from trinolio import store
from trinolio.auth import BASE_URL, get, token
from trinolio.trainings import fetch_trainings

UPLOADED = store.DATA / "uploaded.jsonl"

# The same ~200/hour developer tier the metric backfill is paced against, kept a little under.
INTERVAL = 20.0

# What `/upload/file/` accepts, and nothing else. GPX is reported for manual upload through the
# Nolio UI, which does take it.
FORMATS = {".fit": "fit", ".tcx": "tcx"}

# The wiki calls `id_partner` a string but its own example is a six digit integer, and documents
# no maximum length. Truncated rather than sent as a full digest so there is room to shrink it
# if Nolio turns out to have a limit; 32 hex chars is far past any collision risk at this scale.
KEY_LENGTH = 32

# A re-upload is refused with a 400 whose body says so, which is a success for a resumed run.
ALREADY_IMPORTED = "already imported"

SENT = pl.Schema({"id_partner": pl.String, "path": pl.String, "athlete_id": pl.Int64})


class Activity(TypedDict):
    """One uploadable file, hashed during the scan so the bytes need not be held in memory."""

    path: Path
    format: str
    id_partner: str


def athletes(wants_coach: bool = False) -> list[dict[str, Any]]:
    """The athletes this token manages: `nolio_id`, `name`, and the teams they are managed in."""
    return get("get/athletes", wants_coach=wants_coach)


def activity_format(path: Path) -> str | None:
    """`fit`, `tcx`, or None for what the endpoint will not take. Sees through a `.gz`."""
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if suffixes and suffixes[-1] == ".gz":
        suffixes.pop()
    return FORMATS.get(suffixes[-1]) if suffixes else None


def read_activity(path: Path) -> bytes:
    """The activity's own bytes, gunzipped where the export stored it compressed."""
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.suffix.lower() == ".gz" else raw


def id_partner(payload: bytes) -> str:
    """The dedup key Nolio matches on, derived from the file rather than its name.

    Strava names an export by its own activity id and Garmin by a different one, so a name-based
    key would upload the same session twice once an athlete re-exports from the other source.
    """
    return hashlib.sha256(payload).hexdigest()[:KEY_LENGTH]


def scan(paths: Iterable[Path]) -> tuple[list[Activity], list[Path]]:
    """Every uploadable activity under `paths`, and the files no endpoint will take.

    Hashes as it walks, so a run holds one payload in memory at a time rather than the ~500 MB
    a few years of FITs comes to.
    """
    found: list[Activity] = []
    unsupported: list[Path] = []
    for root in paths:
        candidates = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in candidates:
            if not path.is_file():
                continue
            kind = activity_format(path)
            if kind is None:
                unsupported.append(path)
                continue
            found.append(
                {"path": path, "format": kind, "id_partner": id_partner(read_activity(path))}
            )
    return found, unsupported


def uploaded() -> pl.DataFrame:
    """The keys Nolio has already answered for, from the append-only log."""
    # JSONL for the same reason as `push.PUSHED`: written after every POST, so it has to survive
    # the run being killed mid-write.
    lines = UPLOADED.read_text().splitlines() if UPLOADED.exists() else []
    rows = [json.loads(line) for line in lines if line.strip()]
    return pl.DataFrame(rows, schema=SENT, strict=False)


def pending(found: Iterable[Activity]) -> list[Activity]:
    """The activities still owed to Nolio, oldest path first."""
    done = set(uploaded()["id_partner"].to_list())
    return [activity for activity in found if activity["id_partner"] not in done]


def existing_range(athlete_id: int | None = None) -> tuple[date, date] | None:
    """The span of workouts Nolio already holds for this athlete, None if it holds none.

    Uploading across this range is what creates duplicates, since a connector-synced session has
    no `id_partner` for Nolio to match a fresh upload against.
    """
    days = [
        datetime.strptime(workout["date_start"], "%Y-%m-%d").date()
        for workout in fetch_trainings(athlete_id=athlete_id)
        if workout.get("date_start")
    ]
    return (min(days), max(days)) if days else None


def send(payload: bytes, kind: str, key: str, athlete_id: int | None = None) -> bool:
    """Upload one activity. True if Nolio took it, False if it already had that key."""
    body: dict[str, Any] = {
        "id_partner": key,
        "format": kind,
        "data": base64.b64encode(payload).decode(),
    }
    if athlete_id:
        body["athlete_id"] = athlete_id
    # Generous against `push`'s 30s: base64 inflates a FIT by a third, so these are the largest
    # bodies trinolio sends by two orders of magnitude.
    response = httpx.post(
        f"{BASE_URL}/upload/file/",
        headers={"Authorization": f"Bearer {token()}"},
        json=body,
        timeout=120,
    )
    if (
        response.status_code == httpx.codes.BAD_REQUEST
        and ALREADY_IMPORTED in response.text.lower()
    ):
        return False
    response.raise_for_status()
    return True


def mark_uploaded(key: str, path: Path, athlete_id: int | None) -> None:
    UPLOADED.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id_partner": key, "path": str(path), "athlete_id": athlete_id}
    with UPLOADED.open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
