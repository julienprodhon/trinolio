"""Samples parsed out of FIT files: values as encoded, race legs cut apart, the stored table."""

import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl
from fitdecode.utils import compute_crc

from trinolio import store, streams

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)
START = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
SINGAPORE = timedelta(hours=8)

UINT8, UINT32 = 0x02, 0x86
# (global message number, [(field number, size, base type)])
RECORD = (20, [(253, 4, UINT32), (3, 1, UINT8), (73, 4, UINT32), (30, 1, UINT8), (39, 1, UINT8)])
SESSION = (18, [(253, 4, UINT32), (2, 4, UINT32), (8, 4, UINT32)])
ACTIVITY = (34, [(253, 4, UINT32), (5, 4, UINT32)])


def fit_time(moment: datetime) -> int:
    return int((moment - FIT_EPOCH).total_seconds())


def write_fit(path: Path, sessions: list[tuple[datetime, int]], unknown: bool = False) -> None:
    """A file holding one record a second over each (start, seconds) session."""
    record = (RECORD[0], [*RECORD[1], (90, 1, UINT8)] if unknown else RECORD[1])
    body = b""
    for local, (number, fields) in enumerate((record, SESSION, ACTIVITY)):
        layout = b"".join(struct.pack("<BBB", *field) for field in fields)
        body += struct.pack("<BBBHB", 0x40 | local, 0, 0, number, len(fields)) + layout
    for start, seconds in sessions:
        for second in range(seconds):
            # heart rate 150, 2.5 m/s, balance byte 128, vertical oscillation 80, unknown_90 7
            values = [fit_time(start) + second, 150, 2500, 128, 80] + ([7] if unknown else [])
            body += struct.pack("<BIBIBB" + "B" * unknown, 0, *values)
    for start, seconds in sessions:
        end = fit_time(start) + seconds
        body += struct.pack("<BIII", 1, end, fit_time(start), seconds * 1000)
    end = fit_time(sessions[-1][0]) + sessions[-1][1]
    body += struct.pack("<BII", 2, end, end + int(SINGAPORE.total_seconds()))
    header = struct.pack("<BBHI4sH", 14, 0x20, 2100, len(body), b".FIT", 0)
    path.write_bytes(header + body + struct.pack("<H", compute_crc(header + body)))


def workout(nolio_id: int, start: datetime, duration: int) -> streams.Workout:
    return streams.Workout(nolio_id, (start + SINGAPORE).replace(tzinfo=None), duration)


def test_samples_keep_the_values_the_file_encodes(tmp_path: Path) -> None:
    path = tmp_path / "1.fit"
    write_fit(path, [(START, 3)])

    samples, _ = streams.parse(path, workout(1, START, 3))

    assert samples.height == 3
    assert samples["nolio_id"].to_list() == [1, 1, 1]
    assert samples["timestamp"][0] == START
    assert samples["heart_rate"][0] == 150
    assert samples["enhanced_speed"][0] == 2.5
    # fitdecode reads this byte as "right"
    assert samples["left_right_balance"][0] == 128


def test_named_fields_outside_the_schema_are_reported_and_unknown_ones_skipped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "1.fit"
    write_fit(path, [(START, 2)], unknown=True)

    assert streams.parse(path, workout(1, START, 2)).unlisted == {"vertical_oscillation"}


def test_a_race_leg_gets_only_its_own_session(tmp_path: Path) -> None:
    path = tmp_path / "1.fit"
    bike = START + timedelta(seconds=10)
    write_fit(path, [(START, 10), (bike, 5), (bike + timedelta(seconds=5), 20)])

    # Nolio logs the leg a few seconds late
    samples, _ = streams.parse(path, workout(2, bike + timedelta(seconds=3), 5))

    assert samples["timestamp"].to_list() == [bike + timedelta(seconds=s) for s in range(5)]


def test_a_race_leg_matching_no_session_gets_no_samples(tmp_path: Path) -> None:
    path = tmp_path / "1.fit"
    write_fit(path, [(START, 10), (START + timedelta(seconds=10), 10)])

    assert streams.parse(path, workout(3, START + timedelta(hours=9), 0)).samples.is_empty()


def test_save_adds_parsed_samples_and_drops_workouts_gone_from_nolio() -> None:
    store.write(store.STREAMS, stored_samples(1, 2))

    dropped = streams.save([stored_samples(3)], existing_ids={1, 3})

    assert dropped == 1
    assert pl.read_parquet(store.STREAMS)["nolio_id"].to_list() == [1, 3]


def stored_samples(*nolio_ids: int) -> pl.DataFrame:
    rows = pl.DataFrame({"nolio_id": nolio_ids, "timestamp": [START] * len(nolio_ids)})
    return pl.concat([streams.SCHEMA.to_frame(), rows], how="diagonal_relaxed")
