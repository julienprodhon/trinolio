"""Scanning an export and knowing what has already gone up. No network: `upload.send` and
`upload.existing_range` are the only parts that call out."""

import gzip
import json
from pathlib import Path

import pytest

from trinolio import upload


def write_activity(path: Path, payload: bytes = b"FIT bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(payload) if path.suffix == ".gz" else payload)
    return path


def log_uploaded(keys: list[str]) -> None:
    lines = [json.dumps({"id_partner": key, "path": "x", "athlete_id": None}) for key in keys]
    upload.UPLOADED.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("4900473883.fit.gz", "fit"),
        ("395218037266.fit", "fit"),
        ("ride.tcx.gz", "tcx"),
        ("ride.tcx", "tcx"),
        ("6067102599.gpx", None),
        ("activities.csv", None),
        ("noextension", None),
    ],
)
def test_the_format_is_read_through_any_compression(name: str, expected: str | None) -> None:
    """Strava stores its originals gzipped and Garmin does not, so both spellings of the same
    format have to resolve to the one word the endpoint wants."""
    assert upload.activity_format(Path(name)) == expected


def test_a_gzipped_export_is_decompressed_before_it_is_sent(tmp_path: Path) -> None:
    """Nolio is sent the activity, not the envelope Strava wrapped it in."""
    path = write_activity(tmp_path / "ride.fit.gz", b"the actual FIT")

    assert upload.read_activity(path) == b"the actual FIT"


def test_an_uncompressed_export_is_sent_as_it_lies(tmp_path: Path) -> None:
    path = write_activity(tmp_path / "ride.fit", b"the actual FIT")

    assert upload.read_activity(path) == b"the actual FIT"


def test_the_key_is_the_activity_rather_than_the_name_it_was_exported_under() -> None:
    """Strava names a file by its own activity id and Garmin by another, so the same session
    re-exported from the other source would upload twice under a name-based key."""
    assert upload.id_partner(b"one ride") == upload.id_partner(b"one ride")


def test_different_activities_get_different_keys() -> None:
    """Nolio matches uploads on this and nothing else, so a collision silently drops a workout."""
    assert upload.id_partner(b"one ride") != upload.id_partner(b"another ride")


def test_a_gzipped_and_a_plain_copy_of_one_activity_share_a_key(tmp_path: Path) -> None:
    """The 2026 files are raw FIT and the rest are gzipped, so an athlete's archive can hold
    both spellings of one session. Hashing after decompression is what makes them one."""
    compressed = write_activity(tmp_path / "a.fit.gz", b"same ride")
    plain = write_activity(tmp_path / "b.fit", b"same ride")

    left = upload.id_partner(upload.read_activity(compressed))
    right = upload.id_partner(upload.read_activity(plain))
    assert left == right


def test_scanning_walks_a_directory_tree(tmp_path: Path) -> None:
    """An export is one directory per year, so the paths given are roots and not files."""
    write_activity(tmp_path / "activities-2019" / "a.fit.gz")
    write_activity(tmp_path / "activities-2020" / "b.fit.gz")

    found, _ = upload.scan([tmp_path])
    assert len(found) == 2


def test_scanning_separates_what_the_endpoint_cannot_take(tmp_path: Path) -> None:
    """GPX is reported rather than skipped in silence: it is a real workout that has to be
    uploaded by hand, and an athlete's history should not quietly come up short."""
    write_activity(tmp_path / "a.fit.gz")
    write_activity(tmp_path / "b.gpx", b"<gpx/>")

    found, unsupported = upload.scan([tmp_path])
    assert [activity["path"].name for activity in found] == ["a.fit.gz"]
    assert [path.name for path in unsupported] == ["b.gpx"]


def test_scanning_takes_a_single_file_as_well_as_a_directory(tmp_path: Path) -> None:
    path = write_activity(tmp_path / "a.fit")

    found, _ = upload.scan([path])
    assert len(found) == 1


def test_scanning_hashes_each_activity_it_finds(tmp_path: Path) -> None:
    """The key is what the resume log matches on, so it is computed during the walk rather
    than at send time, when the bytes are long gone."""
    write_activity(tmp_path / "a.fit", b"one ride")

    found, _ = upload.scan([tmp_path])
    assert found[0]["id_partner"] == upload.id_partner(b"one ride")


def test_pending_is_everything_before_the_first_run(tmp_path: Path) -> None:
    write_activity(tmp_path / "a.fit", b"one ride")
    write_activity(tmp_path / "b.fit", b"another ride")

    found, _ = upload.scan([tmp_path])
    assert len(upload.pending(found)) == 2


def test_pending_skips_what_the_log_already_records(tmp_path: Path) -> None:
    """The log is the resume point, appended after every answered POST so a run killed mid
    upload costs nothing but the request in flight."""
    write_activity(tmp_path / "a.fit", b"one ride")
    write_activity(tmp_path / "b.fit", b"another ride")
    log_uploaded([upload.id_partner(b"one ride")])

    found, _ = upload.scan([tmp_path])
    todo = upload.pending(found)
    assert [activity["path"].name for activity in todo] == ["b.fit"]


def test_a_moved_export_does_not_upload_twice(tmp_path: Path) -> None:
    """The log keys on the hash and not the path, so re-exporting into a new directory, or
    reorganising the archive, resumes rather than starting over."""
    write_activity(tmp_path / "old" / "4900473883.fit.gz", b"one ride")
    found, _ = upload.scan([tmp_path / "old"])
    log_uploaded([found[0]["id_partner"]])

    write_activity(tmp_path / "new" / "totally-different-name.fit", b"one ride")
    moved, _ = upload.scan([tmp_path / "new"])
    assert upload.pending(moved) == []


def test_uploaded_is_empty_rather_than_missing_before_the_first_run() -> None:
    """`pending` reads it on the very first run, when there is no log yet."""
    assert upload.uploaded().height == 0


def test_a_logged_upload_reads_back(tmp_path: Path) -> None:
    """The round trip is the whole resume mechanism, and it runs after every single POST."""
    upload.mark_uploaded("abc123", tmp_path / "a.fit", athlete_id=42)

    assert upload.uploaded().row(0, named=True) == {
        "id_partner": "abc123",
        "path": str(tmp_path / "a.fit"),
        "athlete_id": 42,
    }


def test_the_log_appends_rather_than_replaces(tmp_path: Path) -> None:
    """It is opened per file. Truncating would re-upload the whole history on the next run."""
    upload.mark_uploaded("abc", tmp_path / "a.fit", athlete_id=None)
    upload.mark_uploaded("def", tmp_path / "b.fit", athlete_id=None)

    assert upload.uploaded().height == 2


def test_a_blank_line_in_the_log_is_survivable(tmp_path: Path) -> None:
    """A run killed mid-write is the case the log exists to handle, so reading it back cannot
    be the thing that fails."""
    upload.mark_uploaded("abc", tmp_path / "a.fit", athlete_id=None)
    with upload.UPLOADED.open("a") as handle:
        handle.write("\n")

    assert upload.uploaded().height == 1


def test_every_supported_format_is_one_the_endpoint_documents() -> None:
    """`format` is rejected outright as anything but these two, so a third would fail on the
    first request of an hours-long run."""
    assert set(upload.FORMATS.values()) == {"fit", "tcx"}
