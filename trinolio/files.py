"""The original activity file behind each workout, kept on disk as Nolio stores it.

Every workout `/get/training/` returns carries a `file_url`, a link to the file the device or
the upload produced that expires within the hour. It points at Nolio's file server rather than
the API, so downloading costs no API requests. Each file is written once, named by `nolio_id`,
and never modified afterwards.
"""

import gzip
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import httpx

from trinolio import store

# Nolio's file server publishes no limit, so stay polite rather than find one.
INTERVAL = 0.25


def decoded(content: bytes) -> tuple[bytes, str]:
    """The file's bytes and the suffix its contents call for, gunzipped if Nolio compressed it."""
    # Sniffed rather than read off the URL: the same workout has been served as `.fit` on one
    # listing and `.gzip` on the next.
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
    head = content[:512]
    if head[8:12] == b".FIT":
        return content, ".fit"
    if b"<gpx" in head:
        return content, ".gpx"
    return content, ".bin"


def stored_ids() -> set[int]:
    if not store.FILES.exists():
        return set()
    return {int(path.stem) for path in store.FILES.iterdir() if path.stem.isdigit()}


def missing(workouts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The workouts that have a file in Nolio and none on disk yet."""
    have = stored_ids()
    return [w for w in workouts if w.get("file_url") and w["nolio_id"] not in have]


def download(workout: dict[str, Any], client: httpx.Client) -> Path:
    """Save one workout's file. A shared client keeps the connection open, which halves the time."""
    response = client.get(workout["file_url"])
    response.raise_for_status()
    content, suffix = decoded(response.content)
    path = store.FILES / f"{workout['nolio_id']}{suffix}"
    store.FILES.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    return path


def forget(nolio_ids: Iterable[int]) -> None:
    """Delete the files of workouts that no longer exist in Nolio."""
    for nolio_id in nolio_ids:
        for path in store.FILES.glob(f"{nolio_id}.*"):
            path.unlink()
