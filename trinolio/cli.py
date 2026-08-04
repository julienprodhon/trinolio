"""trinolio command line."""

import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import typer

from trinolio import auth, metrics, push, store, upload
from trinolio.trainings import fetch_trainings
from trinolio.wellness import read_archive

# Markdown mode so the help text reflows: rich otherwise keeps the docstring's own line breaks.
app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="markdown")


@app.command("auth-url")
def auth_url() -> None:
    """Print the consent URL, then pass the returned code to `exchange`."""
    print(auth.auth_url())


@app.command()
def exchange(code: str) -> None:
    """Trade the authorization code for tokens. The code expires in 10 minutes.

    The access token lasts 24h and refreshes itself. The refresh token rotates on every use and
    the previous one dies immediately, so the `.env` is the source of truth: do not hand-edit it
    while a command is running. If a refresh is ever rejected, redo `auth-url` and `exchange`.
    """
    print(auth.exchange(code))


@app.command()
def token() -> None:
    """Print a valid access token."""
    print(auth.token())


@app.command()
def sync(
    full: bool = typer.Option(False, "--full", help="Refetch the whole history."),
    since: str | None = typer.Option(None, help="Fetch from this date, YYYY-MM-DD."),
    lookback: int = typer.Option(
        30, help="Days before the newest stored workout to refetch, so edits land."
    ),
) -> None:
    """Pull workouts and wellness metrics into the local parquet files.

    Refetches a trailing window rather than only what is new, because RPE and descriptions get
    filled in after the session and Nolio recomputes load when FTP or weight change. Rows upsert
    on `nolio_id`, so re-running is idempotent; a workout deleted in Nolio only disappears on
    `--full`.

    It also reports workouts of the same sport starting within five minutes of each other, the
    shape Nolio's own deduplication misses. They are only ever reported: a brick session looks
    identical from here, so merging is a judgement call for a human.
    """
    if full:
        since = None
    elif since is None:
        # A trailing window rather than only what is newer: RPE, feeling and descriptions get
        # filled in after the session, and Nolio recomputes load when FTP or weight change.
        stored = store.newest(store.TRAININGS, "date_start")
        since = (stored - timedelta(days=lookback)).strftime("%Y-%m-%d") if stored else None

    window = f"from {since}" if since else "full history"
    print(f"syncing {window}")

    workouts = fetch_trainings(since)
    added, updated = store.upsert(
        store.TRAININGS, store.trainings_frame(workouts), key="nolio_id", sort="date_start"
    )
    print(f"  trainings  {added:>4} new  {updated:>4} updated  ->  {store.TRAININGS}")

    points = metrics.fetch_metrics(since)
    added, updated = store.upsert(store.METRICS, store.metrics_frame(points), key="id", sort="date")
    print(f"  metrics    {added:>4} new  {updated:>4} updated  ->  {store.METRICS}")

    capped = [name for name, n in Counter(p["type"] for p in points).items() if n >= metrics.LIMIT]
    if capped:
        print(
            f"  warning: {', '.join(capped)} hit the {metrics.LIMIT} row cap, sync a shorter range"
        )

    # Over the whole table rather than the synced window: a duplicate arrives when its second
    # copy does, which can be any number of syncs after the first.
    pairs = store.duplicate_candidates(store.TRAININGS)
    if pairs.height:
        print(
            f"  warning: {pairs.height} pairs start within {store.DUPLICATE_WINDOW} of each other"
        )
        for row in pairs.iter_rows(named=True):
            first, second = row["previous_start"], row["start_local"]
            print(
                f"    {first:%Y-%m-%d %H:%M:%S} +{(second - first).seconds:>4}s  "
                f"{row['previous_id']} ({row['previous_duration']}s) "
                f"vs {row['nolio_id']} ({row['duration']}s)  {row['name']}"
            )
        print("    review them by hand: bricks and race legs look the same from here")


@app.command()
def wellness() -> None:
    """Rebuild the local wellness table from the Garmin archive. No network, rerunnable."""
    frame = store.wellness_frame(read_archive())
    store.write(store.WELLNESS, frame)
    print(f"wellness  {frame.height} rows  {frame['date'].min()} to {frame['date'].max()}")
    for metric_type, rows in frame.group_by("type").len().sort("type").iter_rows():
        print(f"  {metric_type:<18} {rows:>5}")
    print(f"  ->  {store.WELLNESS}")


@app.command("push-metrics")
def push_metrics(
    types: str = typer.Option(
        ",".join(push.DEFAULT_TYPES), help="Comma separated. Only writable types are accepted."
    ),
    limit: int | None = typer.Option(None, help="Stop after this many, otherwise run until done."),
    interval: float = typer.Option(push.INTERVAL, help="Seconds between requests."),
    until: str | None = typer.Option(None, help="Stop before this date, YYYY-MM-DD."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be sent, send none."),
) -> None:
    """Trickle archive wellness into Nolio. Safe to interrupt: the next run resumes.

    The API takes one value per request, so a few years of history is a job measured in days. It
    paces itself and logs every success to `pushed.jsonl`, which the next run anti-joins to pick
    up where this one stopped.

    It stops before the earliest date Nolio already holds, so a live connector value is never
    overwritten with an archive one. Check `--dry-run` before a long run: Nolio has no delete
    route, so a wrong value can only be overwritten or removed by hand in the UI.
    """
    wanted = [name.strip() for name in types.split(",") if name.strip()]
    if unknown := [name for name in wanted if name not in push.METRIC_IDS]:
        raise typer.BadParameter(
            f"{', '.join(unknown)} has no Nolio metric id. Writable: {', '.join(push.METRIC_IDS)}"
        )

    cutoff = datetime.strptime(until, "%Y-%m-%d").date() if until else push.nolio_start()
    todo = push.pending(wanted, cutoff)
    if limit:
        todo = todo.head(limit)

    already = push.sent().height
    print(f"pushing {todo.height} values before {cutoff}, {already} already sent")
    if not todo.height:
        return
    hours = todo.height * interval / 3600
    print(f"  {todo['date'].min()} to {todo['date'].max()}, about {hours:.1f}h at {interval}s each")
    if dry_run:
        for metric_type, rows in todo.group_by("type").len().sort("type").iter_rows():
            print(f"  {metric_type:<18} {rows:>5}")
        return

    done = 0
    started = time.monotonic()
    try:
        for row in todo.iter_rows(named=True):
            if done:
                time.sleep(interval)
            nolio_id = push.send(row["type"], row["value"], row["date"])
            push.mark_sent(row["type"], row["date"], nolio_id)
            done += 1
            if done % 25 == 0 or done == todo.height:
                left = (todo.height - done) * (time.monotonic() - started) / done / 3600
                print(f"  {done}/{todo.height}  {row['type']} {row['date']}  ~{left:.1f}h left")
    except KeyboardInterrupt:
        print(f"\nstopped at {done}/{todo.height}, rerun to resume")
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        rate_limited = status == httpx.codes.TOO_MANY_REQUESTS
        reason = "rate limited, wait an hour" if rate_limited else error.response.text[:200]
        print(f"\nstopped at {done}/{todo.height} on HTTP {status}: {reason}")
        raise typer.Exit(1) from error


@app.command()
def athletes() -> None:
    """List the athletes this token manages, with the id the other commands take."""
    managed = upload.athletes()
    if not managed:
        print("no managed athletes on this account")
        return
    for athlete in managed:
        teams = ", ".join(team["name"] for team in athlete["teams"]) or "-"
        print(f"  {athlete['nolio_id']:>8}  {athlete['name']:<30} {teams}")


@app.command("push-trainings")
def push_trainings(
    paths: list[Path] = typer.Argument(help="Activity files, or directories to walk."),
    athlete_id: int | None = typer.Option(None, help="Upload for a managed athlete, not yourself."),
    limit: int | None = typer.Option(None, help="Stop after this many, otherwise run until done."),
    interval: float = typer.Option(upload.INTERVAL, help="Seconds between requests."),
    overlap: bool = typer.Option(
        False, "--overlap", help="Upload even where Nolio already holds workouts. Read the docs."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report what would be sent, send none."),
) -> None:
    """Upload an athlete's pre-Nolio history from their export. Safe to interrupt: it resumes.

    One FIT or TCX per request against a ~200/hour limit, so a few years is a job measured in
    hours. Every answered file is logged to `uploaded.jsonl`, which the next run skips, and the
    key is a hash of the file, so a re-export of the same activity is refused by Nolio rather
    than uploaded twice.

    It refuses to run when Nolio already holds workouts for the athlete, because that protection
    does not extend to sessions their own connector synced: those carry no `id_partner`, so
    uploading over them creates duplicates Nolio has no route to delete. Scope the paths to the
    years before their history starts, or pass `--overlap` having understood that.

    GPX is reported and skipped. The endpoint takes neither it nor anything else, though the
    Nolio web UI does accept GPX by hand.
    """
    found, unsupported = upload.scan(paths)
    todo = upload.pending(found)
    print(f"{len(found)} activities found, {len(found) - len(todo)} already uploaded")
    _report_unsupported(unsupported)
    if not todo:
        return
    if not overlap:
        _refuse_if_nolio_has_history(athlete_id)

    if limit:
        todo = todo[:limit]
    print(f"  uploading {len(todo)}, about {len(todo) * interval / 3600:.1f}h at {interval}s each")
    if dry_run:
        for kind, n in sorted(Counter(activity["format"] for activity in todo).items()):
            print(f"  {kind:<6} {n:>5}")
        return

    _trickle(todo, athlete_id, interval)


def _report_unsupported(unsupported: list[Path]) -> None:
    if not unsupported:
        return
    # Broken down rather than totalled: pointed at a whole export this also counts the wellness
    # JSON and the zips, and only the extension tells those from a real workout.
    formats = Counter(path.suffix.lower() for path in unsupported)
    listed = ", ".join(f"{n} {suffix}" for suffix, n in sorted(formats.items()))
    print(f"  skipping {len(unsupported)} the endpoint cannot take ({listed})")
    if gpx := formats.get(".gpx"):
        print(f"    {gpx} of those are workouts: upload them by hand, the Nolio UI takes GPX")


def _refuse_if_nolio_has_history(athlete_id: int | None) -> None:
    """Stop before uploading into a range Nolio already holds, which duplicates it for good."""
    held = upload.existing_range(athlete_id)
    if not held:
        return
    first, last = held
    whose = f"athlete {athlete_id}" if athlete_id else "your account"
    print(f"\nrefusing: Nolio already holds workouts for {whose}, {first} to {last}")
    print("  a connector-synced session has no id_partner, so uploading over that range")
    print("  duplicates it, and Nolio has no delete route. Scope the paths to the years")
    print("  before it, or pass --overlap.")
    raise typer.Exit(1)


def _trickle(todo: list[upload.Activity], athlete_id: int | None, interval: float) -> None:
    done, imported = 0, 0
    started = time.monotonic()
    try:
        for activity in todo:
            if done:
                time.sleep(interval)
            path = activity["path"]
            accepted = upload.send(
                upload.read_activity(path), activity["format"], activity["id_partner"], athlete_id
            )
            upload.mark_uploaded(activity["id_partner"], path, athlete_id)
            done += 1
            imported += accepted
            if done % 25 == 0 or done == len(todo):
                left = (len(todo) - done) * (time.monotonic() - started) / done / 3600
                print(f"  {done}/{len(todo)}  {path.name}  ~{left:.1f}h left")
    except KeyboardInterrupt:
        print(f"\nstopped at {done}/{len(todo)}, rerun to resume")
        return
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        rate_limited = status == httpx.codes.TOO_MANY_REQUESTS
        reason = "rate limited, wait an hour" if rate_limited else error.response.text[:200]
        print(f"\nstopped at {done}/{len(todo)} on HTTP {status}: {reason}")
        raise typer.Exit(1) from error

    # Accepted means queued, not landed, so the count Nolio ends up with is the one that counts.
    print(f"  {imported} accepted, {done - imported} Nolio already had")
    print("  run `trinolio sync --full` and reconcile the counts: uploads are processed async")


if __name__ == "__main__":
    app()
