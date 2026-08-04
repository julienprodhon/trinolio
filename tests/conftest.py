"""Redirect the warehouse at a temporary directory, for every test without exception.

`store.DATA` resolves at import to the `data/` beside the checkout. Left alone, a test
calling `store.write` would overwrite the real trainings table, and `push` appends to a log a
live backfill may be writing to. `autouse` so that cannot happen by forgetting it.
"""

from pathlib import Path

import pytest

from trinolio import push, store, upload, wellness


@pytest.fixture(autouse=True)
def warehouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("TRAININGS", "METRICS", "WELLNESS"):
        monkeypatch.setattr(store, name, tmp_path / f"{name.lower()}.parquet")
    monkeypatch.setattr(push, "PUSHED", tmp_path / "pushed.jsonl")
    monkeypatch.setattr(upload, "UPLOADED", tmp_path / "uploaded.jsonl")
    # Read-only, but pointed away all the same: a test asserting on row counts would
    # otherwise pass or fail depending on whose Garmin export sits next to the checkout.
    monkeypatch.setattr(wellness, "ARCHIVE", tmp_path / "archive")
    return tmp_path
