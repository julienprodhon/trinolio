# Notes

Reference material that would only weigh the [README](README.md) down: where trinolio puts its
files, and what Nolio's API actually does. Most of the second half was found by probing the live
API, and some of it corrects the
[official wiki](https://github.com/NolioApp/NolioAPI-Documentation/wiki). Where the two disagree,
what is written here is what the code was built against.

## Where files are looked for

Three environment variables, all optional, all read at import rather than from the `.env`, since
the `.env` is itself one of the things being located:

| Variable | Default | What it points at |
| --- | --- | --- |
| `TRINOLIO_ENV` | nearest `.env` walking up from the cwd | Credentials and the rotating tokens. |
| `TRINOLIO_DATA` | `data/` beside a source checkout, else `./data` | The parquet warehouse. |
| `TRINOLIO_ARCHIVE` | `data-archive/wellness/` beside a checkout, else `./data-archive/wellness` | A Garmin wellness export, read by `wellness`. |

The checkout defaults are what keep a `pip install`ed copy from writing its warehouse into
`site-packages`, and they hold from any working directory, so a notebook elsewhere in the tree
finds the same files.

## Rate limits

The developer tier allows about 200 requests per hour and 2000 per day, with `/get/records/`
capped separately at 20 per minute. Reading is comfortable inside that: `sync --full` pages
through a whole history in single-digit requests and an incremental `sync` costs 2. Query the
parquet afterwards rather than refetching.

Writing is not. `/update/metric/` takes one value per request and `/upload/file/` one activity,
so both backfills are bounded by the hourly limit and nothing else, which is why `push-metrics`
and `push-trainings` are trickle jobs with resume logs. Seven years of training files is on the
order of two thousand requests, so plan for half a day rather than an evening.

There is no sandbox. Nolio suggests testing against production with a dedicated account. A
personal developer app is capped at 5 synchronized athletes; lifting that means emailing
contact@nolio.io to switch to a multi-user application.

## What the API actually returns

Zero, never null, for anything not recorded, and that is the common case rather than the
exception. `rpe` and `feeling` are 0 on every workout you did not rate, the power columns on
every ride without a meter, `load_coggan` wherever it could not be computed, and `ftp`, `rftp`,
`critical_power`, `wbal` and `max_hr_user` on every row of the archive this was built against.
`load_foster` is exactly `rpe` times minutes, so it is 0 on precisely the rows where RPE is.
`trinolio.data` nulls them; on an archive of mostly meterless rides, a raw `avg_watt.mean()`
read 3.4 W against a true 112 W.

The same field arrives as an int for one workout and a float for the next (`elevation_gain`,
`load_foster`, `avg_watt`), which is why `store` pins schemas rather than letting polars infer
them. Inferred, one sync is not appendable to the last.

`sport` is localized to the account language while `sport_id` is stable. Never key on the
string. The only list of ids in the 47 wiki pages is the table in `Training-Object.md`, and no
endpoint enumerates them, so `SPORTS` is a transcription of that page and the English names have
no authority beyond it. Two consequences: id 75 (Ski alpin) is absent even from the wiki and is
in the map by inference from the localized string, and id 18 is "Virtual ride" in the wiki while
the app serves "Vélo - Home Trainer", so a plain non-virtual turbo session lands there too.

`date_end` is empty on every workout, so its parse format has to be given explicitly rather than
inferred. `file_url` on a workout is a CDN link that expires within the hour, so `sync` drops it
instead of storing a column that would churn every row on every sync while never being usable.

`/get/user/meta/` is the only usable metrics listing endpoint, since `/get/metric/` needs an id.
One request returns every type at once, and it serves 14 where the wiki documents 4: `sleep`,
`lightsleep`, `deepsleep`, `paradoxicalsleep`, `awaketime`, `scoresommeil`, `hrrest`, `vo2max`,
`weight`, `calories`, `caloriesactive`, `caloriesaurepos`, `numberofsteps`, `garminbodybattery`.
Its `limit` applies per type and truncates silently at 300, so `sync` warns when a type comes
back at the cap.

Wellness only exists from the day a connector was linked. Anything earlier has to come from an
export, which is what `wellness` and `push-metrics` are for.

`/get/hrv/rmssd/` returns empty when the athlete's watch does not report RMSSD to Nolio. The
endpoint works; it is a per-device gap, not a dead route. Treat HRV as an optional cross-check
that some athletes have and others do not, never as a required input.

## Writing metrics

`/update/metric/` is the one write path trinolio uses. Verified against the live API by probing
with a non-existent id, so nothing could be written:

- Exactly one metric per POST. Bare arrays and parallel arrays return a 500 Django error page,
  `{"metrics": [...]}` and `{"data": [...]}` return `metric_id is missing`, and only the flat
  single form returns `Metric doesn't exist`. There is no batch path and no CSV route.
- Both JSON and form encoding are accepted.
- It genuinely upserts on (metric_id, date): re-pushing the same day returns the same row id
  rather than creating a second, so re-running costs nothing but requests.
- The response echoes `metric_id` as the id of the stored row, not the type id that was sent.
- Written values get `source: null`, indistinguishable from manual entry. Metrics carry no
  `id_partner`, so afterwards a backfill can only be identified by date range.
- Only 4 of the 14 types have a documented numeric id and can be written: `sleep` (1), `weight`
  (2), `vo2max` (8), `hrrest` (9). No id is discoverable for the rest, since `/get/metric/`
  returns a localized type name ("Poids") rather than an id.
- There is no delete route. A wrong value can be overwritten via the API, or removed by hand in
  the Nolio UI.
- `new_value` must be finite and positive; weight is capped at 200 and body fat at 100.

Everything else Nolio can write stays unused: completed and planned workouts, structured
workouts, notes, training messages, and mark as viewed.

## Uploading activities

`/upload/file/` is the second write path, used by `push-trainings` to put an athlete's history
into Nolio from the day before they had an account. Unlike everything above, none of this has
been probed: it is the wiki's contract as written, and the wiki has been wrong before. Treat a
first run as the probe, and correct this section from what it returns.

- One base64 encoded file per POST. `id_partner`, `format` and `data` are required, `format` is
  `fit` or `tcx`, and `title`, `comment` and `athlete_id` are optional.
- GPX is not accepted, despite the Nolio web UI taking it by hand. An export that has no FIT for
  an activity, which is what Strava serves for a phone-recorded run, cannot go up this way.
- `202 Accepted` means queued for processing, not imported. Success here is not a workout in
  Nolio, so a run has to be reconciled against a later `sync` rather than trusted.
- `400` with "Training already imported" is how a repeat `id_partner` is refused. `push-trainings`
  treats it as done rather than as an error, since that is exactly what a resumed run hits.
- `athlete_id` uploads on behalf of a managed athlete, whose ids come from `/get/athletes/`.
  That endpoint returns `nolio_id`, `name` and the `teams` the athlete is managed through, and
  lists an athlete once no matter how many of those teams they are in.

The consequence that shapes the command: `id_partner` is the only thing Nolio matches an upload
against, and a session its own connector synced has none. So the duplicate protection covers
re-running the uploader and covers nothing else. Uploading a range Nolio already holds duplicates
it, and there is no delete route, which is why `push-trainings` refuses to run against an athlete
with existing workouts unless told to.

## Zones

The `zones` payload carries only `min`, `max` and `duration` per bucket. There is no zone index,
which is why `data.ladder()` derives the ordinal by dense-ranking each channel's distinct floors
rather than declaring one. Boundaries are the athlete's own settings, mutable and owned by them,
so a constant in the library would freeze one account's configuration into it.

Two shapes to know, since the numbering cannot assume either: heart rate buckets meet exactly
(164 closes one and opens the next) while watts leave a unit between them (99 then 100). The
overlap guard is therefore strictly `min < previous max`, which passes both. It raises rather
than reports, because a ladder redefined mid-history would renumber every workout on both sides
of the change, and an ordinal nobody can trust is worse than no ordinal.

The consequence for any query spanning years: every bucket was measured against whatever the
zone settings were at the time, and Nolio does not recompute history when they change. Time in
zone is not comparable across a settings change, and there is no way to detect one from the
stored buckets alone.

## Streams

`/get/training/streams/` is not fetched by `sync`. Probed on five workouts spanning the history,
which is what any future stream support should be built on:

- Streams exist for the whole history, including a 2019 workout and a GPX backfill. No 204s.
- Channels are `heartrate`, `watts`, `cadence`, `torque`, `pace`, `altitude`, `distance`, `time`,
  plus `file_url` and `custom_laps`. No GPS coordinates, no temperature, no running dynamics:
  those only exist in the original FIT.
- The wiki's claim that all arrays share one length and index is false. One swim returned 1701
  `heartrate` and `time` samples against 1680 for `cadence`, `pace` and `distance`. Do not zip
  blindly.
- Channels vary per workout. A 155 minute power ride had 9348 `watts` samples and zero
  `heartrate`. Anything assuming HR is always present will have holes.
- Sampling is irregular: 0, 5, 6, 7 seconds on one run, 0, 1, 8, 10 on another. Time in zone has
  to integrate over the deltas, and counting samples is wrong by a different amount per workout.
- `stream_pace` is speed in m/s despite the name, and is populated on every workout probed,
  swimming included (0.735 reads as 2:16/100m). Nolio's own buckets only ever contain
  `heartrate` and `watts`, so pace zones are reachable this way and only this way.
- Swim `stream_distance` is per lap (25.0 repeated, the pool length), not cumulative. Run and
  ride are cumulative and consistent with their speed traces.
- Payloads run 26 KB to 519 KB at roughly 1 Hz. Against ~2250 recorded hours that is on the
  order of 8M samples, perhaps 30 to 60 MB as parquet, two orders of magnitude past the current
  warehouse. It needs a storage shape decision before a full backfill, not after.
- One request per workout at ~200/hour puts a full history in the order of ten hours of
  trickling, the same shape as `push-metrics`.

## Other things Nolio does

Duplicate uploads are deduplicated by source priority, wearables (Garmin, Suunto, Coros) beating
apps (Strava), but only once Nolio matches two uploads as the same session by start time and
duration. The known failure mode is an indoor or virtual ride recorded twice with slightly
different start times, where neither copy is recognized as the other's duplicate. That is what
`sync` reports and deliberately does not merge.

Webhooks exist for real events, planned events and metrics. The payload carries `notif_type`,
`object_type`, `object_id`, `user_id` and `livemode`, and is verified with a static `X-Nolio-Key`
header rather than an HMAC signature. trinolio does not use them; they need a public URL.
