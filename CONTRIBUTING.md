# Contributing

## Setup

```sh
uv sync
```

## The gate

Everything below is expected to pass before a change lands.

```sh
uv run ruff format
uv run ruff check --fix
uv run ty check
uv run pytest
```

Coverage is optional and not a target to chase:

```sh
uv run coverage run -m pytest && uv run coverage report
```

## Releasing

CalVer, `YY.M`, with a patch segment for a second release inside one month. Do not zero-pad:
PEP 440 normalises numerically, so `26.08` and `26.8` are the same version, while `26.10` sorts
after `26.9` correctly because the comparison is on integers.

Bump `version` in `pyproject.toml`, then tag and push:

```sh
git tag -a v26.9 -m "..."
git push origin v26.9
```

The tag is what triggers `.github/workflows/publish.yml`, which builds once and then does three
things in order: check the tag against the packaged version, upload to PyPI, and cut a GitHub
release carrying the same sdist and wheel. The version check runs before any upload because a
version on PyPI is permanent: it can be yanked, never replaced, and never re-uploaded under the
same number. The release comes last so it can never advertise a version PyPI rejected.

Publishing uses PyPI trusted publishing, so there is no API token anywhere in this repo. It needs
a one-time setup on PyPI: add a pending publisher for the `trinolio` project pointing at this
repository, the workflow filename `publish.yml`, and the environment name `pypi`. The
`environment: pypi` line in the workflow has to match whatever is registered there.

## Tests

The suite touches no network. An autouse fixture in `tests/conftest.py` repoints the warehouse paths, the push log and the archive directory at a tmpdir, so a test can neither read your real data nor overwrite it, and cannot pass or fail depending on whose Garmin export happens to sit next to the checkout. Keep it that way: a test that needs a socket is a test that will be skipped, then deleted.

What is covered is the code where a bug is a wrong number rather than a traceback: the schemas and merge in `store`, every encoding `data` undoes, the archive parser in `wellness`, the pagination walk in `trainings`, and the resume logs in `push` and `upload`. `cli` and `auth` are deliberately uncovered, being wrappers around calls that need a socket. Covering them means mocking httpx, which mostly tests the mock. `upload.send` and `upload.existing_range` are uncovered for that reason too, which leaves the "already imported" branch resting on the wiki rather than on a test: correct it from what a real run returns.

## Layout

| Module | Responsibility |
| --- | --- |
| `auth` | OAuth: authorize, exchange, refresh, and the shared `get`. |
| `trainings`, `metrics` | Fetching, including the pagination walk Nolio gives no cursor for. |
| `store` | The parquet files, the pinned schemas, the upsert, the duplicate report. |
| `wellness` | The Garmin export parsed into Nolio's vocabulary. |
| `data` | The stored parquet read back with Nolio's encodings undone. |
| `push` | Backfilling the wellness history Nolio never had. |
| `upload` | Backfilling the activity files, for an athlete who trained before their account. |
| `cli` | typer wiring. The only module allowed to print. |

Command help text lives in the command docstrings, which typer prints under `--help`, and the public reading API documents itself in the `data` docstrings. Both are the README's overflow: keep an explanation next to the thing it explains rather than growing the front page.

## Conventions

[NOTES.md](NOTES.md) records what Nolio's API actually does, including where it contradicts the official wiki. Most of the rules below are a reaction to something in there, so read it before changing how anything is fetched or decoded, and add to it when probing turns up something new. A finding that only survives in a commit message is a finding the next person will rediscover the hard way.

Abstract when a need appears, not before. No layer, dependency or test scaffold goes in ahead of the code that requires it.

Comments explain why, never what. The code says what it does, so a comment restating it is a second thing to keep in sync.

`data` holds facts about how Nolio encodes things, never analysis. A grouping is data and belongs there; a question is a query and belongs in a notebook. If you find yourself adding an argument that narrows a result to one athlete, one year or one sport, that is a question.

Schemas are pinned in `store`, never inferred. Nolio sends the same field as an int for one workout and a float for the next, so inference makes one sync unappendable to the last.

Nolio writes 0 for what it did not record. Anything new that reads a numeric column should go through `data`, which nulls them, rather than `read_parquet`.

Report anomalies, do not silently repair them. Duplicate workouts are surfaced for review rather than merged, because a brick session is indistinguishable from a double upload without a human. The exception is a derived value that would be wrong rather than merely suspect: an overlapping zone ladder raises, because an ordinal nobody can trust is worse than no ordinal.

A write that cannot be undone gets a third answer: refuse and explain. `push-trainings` stops rather than uploading into a range Nolio already holds, because Nolio has no delete route and the duplicates would be the athlete's problem forever. Reporting is right when a human can still act on it; refusing is right when the damage lands before they can.
