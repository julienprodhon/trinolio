# trinolio

Tooling over the [Nolio](https://www.nolio.io) API: fetch your training data, keep it locally, analyse it yourself. Unofficial and unaffiliated with Nolio.

Nolio aggregates from Garmin, Coros, Suunto, Polar, Zwift, Strava, Oura, Whoop and more, and normalises across them, so reading from Nolio alone gives device-agnostic data.

Reading is the whole point. The exceptions are the two backfills, which exist because a Nolio account starts the day it is opened and an athlete's history starts years earlier: `push-metrics` uploads wellness from a local Garmin export, and `push-trainings` uploads the activity files themselves. Both are for onboarding, not for ongoing use, and neither writes a workout you have already recorded.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/), for development
- A Nolio account and a developer app from https://www.nolio.io/developers/, which gives you a `client_id` and `client_secret`.

## Setup

Not on PyPI yet, so install from a checkout:

```sh
uv sync          # in this directory, for development
uv pip install . # or into an environment of your own
```

Create a `.env` with your credentials. It can live in this directory or in any parent, which is where the rotating tokens get written too:

```
NOLIO_CLIENT_ID=...
NOLIO_CLIENT_SECRET=...
NOLIO_REDIRECT_URI=...
```

Then authorise once:

```sh
trinolio auth-url        # open the URL, approve, copy the code back
trinolio exchange CODE
```

## Getting started

```sh
trinolio sync            # pull workouts and wellness metrics into data/
```

```python
from trinolio import data

runs = data.trainings().filter(discipline="run")
print(runs["duration"].sum() / 3600, "hours running")
```

Always read through `trinolio.data` rather than the parquet directly. Nolio writes 0, never null, for what it did not record, and that is the common case rather than the exception. On an archive where most rides have no power meter, raw `avg_watt.mean()` reads 3.4 W against a true 112 W.

## Commands

`trinolio <command>`, or `python -m trinolio <command>` if you would rather not rely on the console script being on your `PATH`. Each one explains itself with `--help`.

| Command | |
| --- | --- |
| `auth-url`, `exchange`, `token` | Authorise once, then let it refresh itself. |
| `sync` | Pull workouts and wellness metrics into the local parquet files. |
| `athletes` | List the athletes you coach, with their ids. |
| `wellness` | Rebuild `wellness.parquet` from a Garmin export. |
| `push-metrics` | Trickle archive wellness up into Nolio. |
| `push-trainings` | Upload an athlete's pre-Nolio history from their export. |

## Onboarding an athlete who trained before Nolio

Their Garmin or Strava export holds the years Nolio never saw. `push-trainings` walks it and
uploads each FIT or TCX, one per request, pacing itself against the hourly limit and logging
what Nolio answered for so an interrupted run resumes:

```sh
trinolio athletes                                          # find their id
trinolio push-trainings --athlete-id 123 --dry-run export/ # what would go up
trinolio push-trainings --athlete-id 123 export/           # hours, resumable
```

Read `--help` before the real run. Nolio matches uploads on a key trinolio derives from the file,
so re-running is safe, but that key does not exist on the sessions their own watch already
synced: uploading across a range Nolio holds duplicates it, and there is no delete route. The
command refuses that case unless you pass `--overlap`. GPX is reported and skipped, since the API
takes only FIT and TCX even though the web UI accepts GPX by hand.

## Reading the data

```python
data.trainings()   # one row per workout, unrecorded values as null
data.wellness()    # your export and Nolio's connector as one series
data.zones()       # time in zone, long: nolio_id, date_start, sport_id, channel, min, max, duration, zone
```

Each has a docstring covering what it decodes and what to watch out for. Units are Nolio's own, unconverted: `distance` in kilometres, `elevation_*` in metres, `duration` in seconds. `date_start` and `hour_start` are your local wall clock rather than UTC.

The module holds facts about how Nolio encodes things, not analysis. A weekly load or a fitness curve is a question, and questions belong in a notebook. `trinolio.store` exposes the raw paths and schemas if you want the file untouched.

## Notes

[NOTES.md](https://github.com/julienprodhon/trinolio/blob/main/NOTES.md) has the reference material: where trinolio looks for your files, the rate limits, and what the API actually does where that differs from the official wiki.

## Contributing

See [CONTRIBUTING.md](https://github.com/julienprodhon/trinolio/blob/main/CONTRIBUTING.md).

## Versioning

CalVer, `YY.M`, so `26.8` is August 2026 and a patch inside that month is `26.8.1`. The version
says when this was last checked against Nolio's live API, which is the thing most likely to break
it, and says nothing about whether an upgrade breaks your code. The reading API is small and I do
not plan to churn it, but treat a change in the month as something to read the release notes for
rather than as a compatibility promise.

## License

Apache License 2.0, see [LICENSE](https://github.com/julienprodhon/trinolio/blob/main/LICENSE).
