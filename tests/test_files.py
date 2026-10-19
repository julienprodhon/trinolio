"""Original activity files: what a download is saved as."""

import gzip

from trinolio import files

FIT = b"\x0e\x20\x00\x00\x00\x00\x00\x00.FIT" + b"\x00" * 20


def test_a_gzipped_fit_is_unpacked_whatever_the_url_said() -> None:
    assert files.decoded(gzip.compress(FIT)) == (FIT, ".fit")


def test_a_gpx_is_told_apart_by_its_root_element() -> None:
    assert files.decoded(b'<?xml version="1.0"?><gpx version="1.1">')[1] == ".gpx"
