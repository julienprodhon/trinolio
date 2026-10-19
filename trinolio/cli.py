"""trinolio command line."""

import time
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table

from trinolio import auth, files, metrics, push, store, streams, upload
from trinolio.trainings import fetch_trainings
from trinolio.wellness import read_archive

console = Console(highlight=False)

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
    on `nolio_id`, so re-running is idempotent, and only rows whose content moved count as
    changed. A stored workout inside the window that Nolio no longer returns was deleted there,
    and is dropped and listed; `--full` does the same over the whole history.

    Each listed workout's original file is downloaded into `data/files/` once, and the samples of
    every FIT not parsed yet go into `streams.parquet`. Both resume where an interrupted run
    stopped.

    It also reports workouts of the same sport that start within five minutes of each other and
    agree on duration and distance, the shape Nolio's own deduplication misses. They are only
    ever reported: a brick session looks identical from here, so merging is a judgement call for
    a human.
    """
    if full:
        since = None
    elif since is None:
        # A trailing window rather than only what is newer: RPE, feeling and descriptions get
        # filled in after the session, and Nolio recomputes load when FTP or weight change.
        stored = store.newest(store.TRAININGS, "date_start")
        since = (stored - timedelta(days=lookback)).strftime("%Y-%m-%d") if stored else None

    window = f"from {since}" if since else "full history"
    console.print(f"[bold]sync[/] [dim]{window}[/]")

    workouts = fetch_trainings(since)
    window_start = pl.col("date_start") >= date.fromisoformat(since) if since else pl.lit(True)
    trainings = store.upsert(
        store.TRAININGS,
        store.trainings_frame(workouts),
        key="nolio_id",
        sort="date_start",
        mirrored=window_start,
    )
    _report_upsert("trainings", trainings)
    for day, nolio_id, name in trainings.removed.select(
        "date_start", "nolio_id", "name"
    ).iter_rows():
        console.print(f"    [red]deleted in Nolio[/] {day}  {nolio_id}  {escape(name or '')}")
    files.forget(trainings.removed["nolio_id"])
    _download_files(workouts)
    _parse_streams()

    # Not mirrored: the per-type row cap truncates silently, so a missing metric may just be
    # one the response had no room for.
    points = metrics.fetch_metrics(since)
    _report_upsert(
        "metrics", store.upsert(store.METRICS, store.metrics_frame(points), key="id", sort="date")
    )

    capped = [name for name, n in Counter(p["type"] for p in points).items() if n >= metrics.LIMIT]
    if capped:
        console.print(
            f"  [yellow]warning[/] {', '.join(capped)} hit the {metrics.LIMIT} row cap,"
            " sync a shorter range"
        )


def _download_files(workouts: list[dict[str, Any]]) -> None:
    """Fetch the original file of every workout in this sync that has none on disk yet."""
    # Only the workouts this sync listed, since a `file_url` expires within the hour. Files for
    # older workouts come from a `--full` sync.
    pending = files.missing(workouts)
    fileless = sum(not workout.get("file_url") for workout in workouts)
    note = f"  [dim]{fileless} without a file in Nolio[/]" if fileless else ""
    if not pending:
        console.print(f"  {'files':<10} [dim]up to date[/]{note}")
        return

    done = 0
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client, _progress() as progress:
            task = progress.add_task("", total=len(pending))
            for workout in pending:
                if done:
                    time.sleep(files.INTERVAL)
                path = files.download(workout, client)
                done += 1
                progress.update(task, advance=1, description=path.name)
    except KeyboardInterrupt:
        console.print(f"  [yellow]stopped[/] at {done}/{len(pending)} files, rerun to resume")
        raise typer.Exit(130) from None
    except httpx.HTTPStatusError as error:
        # The links expire within the hour, so a long backfill can outlive the listing it read.
        console.print(
            f"  [red]stopped[/] at {done}/{len(pending)} files on HTTP"
            f" {error.response.status_code}, rerun to resume with fresh links"
        )
        raise typer.Exit(1) from error
    console.print(f"  {'files':<10} [green]+{done} new[/]{note}")


def _parse_streams() -> None:
    """Parse the samples of every downloaded FIT file not in the streams table yet."""
    workouts = pl.read_parquet(store.TRAININGS, columns=["nolio_id", "start_local", "duration"])
    existing = set(workouts["nolio_id"])
    pending = streams.pending(streams.parsed_ids(), workouts)
    parsed: list[pl.DataFrame] = []
    unlisted: set[str] = set()
    done = 0
    try:
        with _progress() as progress:
            task = progress.add_task("", total=len(pending))
            for path, workout in pending:
                samples, fields = streams.parse(path, workout)
                if samples.height:
                    parsed.append(samples)
                unlisted |= fields
                done += 1
                progress.update(task, advance=1, description=path.name)
    except KeyboardInterrupt:
        streams.save(parsed, existing)
        console.print(f"  [yellow]stopped[/] at {done}/{len(pending)} files, rerun to resume")
        raise typer.Exit(130) from None

    dropped = streams.save(parsed, existing)
    changes = [f"[green]+{len(parsed)} new[/]"] if parsed else []
    changes += [f"[red]-{dropped} removed[/]"] if dropped else []
    console.print(f"  {'streams':<10} {'  '.join(changes) or '[dim]up to date[/]'}")
    if unlisted:
        console.print(
            f"  [yellow]warning[/] fields outside the streams schema: {', '.join(sorted(unlisted))}"
        )


def _report_upsert(label: str, result: store.Upserted) -> None:
    parts = []
    if result.added:
        parts.append(f"[green]+{result.added} new[/]")
    if result.changed:
        parts.append(
            f"[yellow]~{result.changed} changed[/] [dim]{', '.join(result.changed_columns)}[/]"
        )
    if result.removed.height:
        parts.append(f"[red]-{result.removed.height} removed[/]")
    console.print(f"  {label:<10} {'  '.join(parts) or '[dim]up to date[/]'}")


@app.command()
def wellness() -> None:
    """Rebuild the local wellness table from the Garmin archive. No network, rerunnable."""
    frame = store.wellness_frame(read_archive())
    store.write(store.WELLNESS, frame)
    console.print(
        f"[bold]wellness[/] {frame.height} rows"
        f" [dim]{frame['date'].min()} to {frame['date'].max()}[/]"
    )
    _print_counts(frame.group_by("type").len().sort("type").iter_rows())


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
    console.print(
        f"[bold]push-metrics[/] {todo.height} values before {cutoff} [dim]{already} already sent[/]"
    )
    if not todo.height:
        return
    hours = todo.height * interval / 3600
    console.print(
        f"  [dim]{todo['date'].min()} to {todo['date'].max()},"
        f" about {hours:.1f}h at {interval}s each[/]"
    )
    if dry_run:
        _print_counts(todo.group_by("type").len().sort("type").iter_rows())
        return

    done = 0
    try:
        with _progress() as progress:
            task = progress.add_task("", total=todo.height)
            for row in todo.iter_rows(named=True):
                if done:
                    time.sleep(interval)
                nolio_id = push.send(row["type"], row["value"], row["date"])
                push.mark_sent(row["type"], row["date"], nolio_id)
                done += 1
                progress.update(task, advance=1, description=f"{row['type']} {row['date']}")
    except KeyboardInterrupt:
        console.print(f"[yellow]stopped[/] at {done}/{todo.height}, rerun to resume")
    except httpx.HTTPStatusError as error:
        _report_http_stop(error, done, todo.height)
        raise typer.Exit(1) from error


@app.command()
def athletes() -> None:
    """List the athletes this token manages, with the id the other commands take."""
    managed = upload.athletes()
    if not managed:
        console.print("[dim]no managed athletes on this account[/]")
        return
    table = Table("id", "name", "teams", box=None, header_style="dim")
    for athlete in managed:
        teams = ", ".join(team["name"] for team in athlete["teams"]) or "-"
        table.add_row(str(athlete["nolio_id"]), escape(athlete["name"]), escape(teams))
    console.print(table)


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
    console.print(
        f"[bold]push-trainings[/] {len(found)} activities"
        f" [dim]{len(found) - len(todo)} already uploaded[/]"
    )
    _report_unsupported(unsupported)
    if not todo:
        return
    if not overlap:
        _refuse_if_nolio_has_history(athlete_id)

    if limit:
        todo = todo[:limit]
    hours = len(todo) * interval / 3600
    console.print(f"  uploading {len(todo)} [dim]about {hours:.1f}h at {interval}s each[/]")
    if dry_run:
        _print_counts(sorted(Counter(activity["format"] for activity in todo).items()))
        return

    _trickle(todo, athlete_id, interval)


def _report_unsupported(unsupported: list[Path]) -> None:
    if not unsupported:
        return
    # Broken down rather than totalled: pointed at a whole export this also counts the wellness
    # JSON and the zips, and only the extension tells those from a real workout.
    formats = Counter(path.suffix.lower() for path in unsupported)
    listed = ", ".join(f"{n} {suffix}" for suffix, n in sorted(formats.items()))
    console.print(
        f"  [yellow]skipping {len(unsupported)}[/] the endpoint cannot take [dim]{listed}[/]"
    )
    if gpx := formats.get(".gpx"):
        console.print(
            f"    {gpx} of those are workouts: upload them by hand, the Nolio UI takes GPX"
        )


def _refuse_if_nolio_has_history(athlete_id: int | None) -> None:
    """Stop before uploading into a range Nolio already holds, which duplicates it for good."""
    held = upload.existing_range(athlete_id)
    if not held:
        return
    first, last = held
    whose = f"athlete {athlete_id}" if athlete_id else "your account"
    console.print(
        f"[bold red]refusing[/] Nolio already holds workouts for {whose}, {first} to {last}"
    )
    console.print(
        "  A connector-synced session has no id_partner, so uploading over that range duplicates"
        " it, and Nolio has no delete route. Scope the paths to the years before it, or pass"
        " --overlap.",
        style="dim",
    )
    raise typer.Exit(1)


def _trickle(todo: list[upload.Activity], athlete_id: int | None, interval: float) -> None:
    done, imported = 0, 0
    try:
        with _progress() as progress:
            task = progress.add_task("", total=len(todo))
            for activity in todo:
                if done:
                    time.sleep(interval)
                path = activity["path"]
                accepted = upload.send(
                    upload.read_activity(path),
                    activity["format"],
                    activity["id_partner"],
                    athlete_id,
                )
                upload.mark_uploaded(activity["id_partner"], path, athlete_id)
                done += 1
                imported += accepted
                progress.update(task, advance=1, description=escape(path.name))
    except KeyboardInterrupt:
        console.print(f"[yellow]stopped[/] at {done}/{len(todo)}, rerun to resume")
        return
    except httpx.HTTPStatusError as error:
        _report_http_stop(error, done, len(todo))
        raise typer.Exit(1) from error

    # Accepted means queued, not landed, so the count Nolio ends up with is the one that counts.
    console.print(f"  [green]{imported} accepted[/] [dim]{done - imported} Nolio already had[/]")
    console.print(
        "  [dim]run `trinolio sync --full` and reconcile the counts: uploads are processed async[/]"
    )


def _progress() -> Progress:
    return Progress(
        BarColumn(),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        TextColumn("[dim]{task.description}[/]"),
        console=console,
    )


def _report_http_stop(error: httpx.HTTPStatusError, done: int, total: int) -> None:
    status = error.response.status_code
    rate_limited = status == httpx.codes.TOO_MANY_REQUESTS
    reason = "rate limited, wait an hour" if rate_limited else escape(error.response.text[:200])
    console.print(f"[red]stopped[/] at {done}/{total} on HTTP {status}: {reason}")


def _print_counts(rows: Iterable[tuple[str, int]]) -> None:
    table = Table(box=None, show_header=False, padding=(0, 1, 0, 2))
    table.add_column()
    table.add_column(justify="right")
    for name, n in rows:
        table.add_row(name, str(n))
    console.print(table)


if __name__ == "__main__":
    app()
