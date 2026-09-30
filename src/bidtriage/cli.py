"""`bidtriage` command line."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, help="ITB triage for Ferry Electric")


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run("bidtriage.web.app:app", host=host, port=port, reload=reload)


def _extractor(fake_dir: Path | None):
    from bidtriage.core.config import get_settings

    if fake_dir is not None:
        from bidtriage.extraction.fake import FakeExtractor

        return FakeExtractor(fake_dir)
    from bidtriage.extraction.claude import ClaudeExtractor

    s = get_settings()
    return ClaudeExtractor(model=s.extraction_model, effort=s.extraction_effort)


@app.command()
def worker(
    fake_fixtures: Path | None = typer.Option(
        None, help="Use fixture-backed extractor instead of Claude"
    ),
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from bidtriage.worker.handlers import Context
    from bidtriage.worker.main import run_forever

    run_forever(Context(_extractor(fake_fixtures)))


@app.command("db-create")
def db_create() -> None:
    """Create tables directly (dev only; production uses alembic)."""
    from bidtriage.core.db import get_engine
    from bidtriage.core.models import Base

    Base.metadata.create_all(get_engine())
    typer.echo("tables created")


@app.command("seed-gcs")
def seed_gcs_cmd() -> None:
    from bidtriage.core.db import session_scope
    from bidtriage.gcs.seed import seed_gcs

    with session_scope() as s:
        n = seed_gcs(s)
    typer.echo(f"seeded {n} new GCs")


@app.command("add-user")
def add_user(email: str, name: str, role: str = "estimator") -> None:
    from bidtriage.core.db import session_scope
    from bidtriage.core.models import User

    with session_scope() as s:
        s.add(User(email=email, name=name, role=role))
    typer.echo(f"added {email} ({role})")


@app.command("add-source")
def add_source(
    kind: str = typer.Argument(..., help="graph | imap | file | manual"),
    name: str = typer.Option(..., help="Display name, e.g. 'Estimating mailbox'"),
    mailbox: str = typer.Option("", help="Mailbox address; recorded as the recipient path"),
    config_json: str = typer.Option(
        "{}",
        help='Credentials, e.g. \'{"tenant_id":"...","client_id":"...","client_secret":"..."}\'',
    ),
    backfill_days: int = typer.Option(90, help="History window walked on first connection"),
) -> None:
    """Register a mail source (SPEC-01 F1). Credentials are encrypted with SECRET_KEY."""
    from bidtriage.core.config import get_settings
    from bidtriage.core.db import session_scope
    from bidtriage.core.models import Source
    from bidtriage.worker.sources import KINDS, encode_config

    if kind not in KINDS:
        typer.echo(f"kind must be one of {', '.join(KINDS)}")
        raise typer.Exit(1)
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as e:
        typer.echo(f"--config-json is not valid JSON: {e}")
        raise typer.Exit(1) from e
    settings = get_settings()
    with session_scope() as s:
        src = Source(
            kind=kind,
            name=name,
            mailbox=mailbox,
            config_enc=encode_config(config, settings.secret_key) if config else "",
            backfill_days=backfill_days,
            paused=kind == "manual",
        )
        s.add(src)
        s.flush()
        typer.echo(f"added source {src.id} ({kind}) {name}")


@app.command("poll-sources")
def poll_sources(
    fake_fixtures: Path | None = typer.Option(None, help="Fixture-backed extractor"),
) -> None:
    """Poll every active source once and report what each poll saw (SPEC-01 F7)."""
    from sqlalchemy import select

    from bidtriage.core.db import session_scope
    from bidtriage.core.models import Source
    from bidtriage.worker import ingest_job
    from bidtriage.worker.handlers import Context

    ctx = Context(_extractor(fake_fixtures) if fake_fixtures else None)
    with session_scope() as s:
        sources = s.scalars(select(Source).where(Source.paused.is_(False))).all()
        if not sources:
            typer.echo("no active sources; add one with `bidtriage add-source`")
            raise typer.Exit(1)
        failed = 0
        for src in sources:
            # One unreachable mailbox must not stop the others: the worker polls each source as its
            # own job, and this command should behave the same way.
            try:
                impl = ctx.source_impl(src)
                summary = ingest_job.run_poll(s, src, impl, ctx)
            except Exception as e:  # noqa: BLE001 - reported per source, then carry on
                failed += 1
                typer.secho(f"{src.name}: poll failed: {e}", fg=typer.colors.RED)
                continue
            typer.echo(
                f"{src.name}: seen={summary.seen} new={summary.new} "
                f"duplicates={summary.duplicates} errors={len(summary.errors)}"
            )
            while not src.backfill_done and not src.backfill_stuck:
                back = ingest_job.run_backfill(s, src, impl, ctx)
                typer.echo(f"{src.name}: backfill batch seen={back.seen} new={back.new}")
                if not back.seen and not back.more_available:
                    break
            if src.backfill_stuck:
                typer.echo(f"{src.name}: backfill stuck — {src.backfill_last_error}")
        if failed:
            typer.echo(f"\n{failed} of {len(sources)} source(s) could not be polled")
            raise typer.Exit(1)


@app.command("source-health")
def source_health_cmd() -> None:
    """Print each source's health using the SPEC-01 F8 thresholds."""
    from bidtriage.core.db import session_scope
    from bidtriage.worker import ingest_job
    from bidtriage.worker.handlers import Context

    with session_scope() as s:
        rows = ingest_job.check_sources(s, Context(None))
        for src, health in rows:
            typer.echo(f"{src.name}: {health.status} ({health.detail})")
        if not rows:
            typer.echo("no sources registered")


@app.command("ingest-metrics")
def ingest_metrics(window_hours: int = 24) -> None:
    """Print the SPEC-01 ingestion metrics over a trailing window."""
    from bidtriage.core.db import session_scope
    from bidtriage.worker.ingest_job import ingestion_metrics

    with session_scope() as s:
        m = ingestion_metrics(s, window_hours=window_hours)
    rate = "n/a" if m.poll_success_rate is None else f"{m.poll_success_rate:.0%}"
    typer.echo(f"window: last {m.window_hours}h")
    typer.echo(f"polls: {m.polls} ({m.polls_failed} with errors), success rate {rate}")
    typer.echo(f"messages: {m.messages_new} new, {m.duplicates_linked} duplicates linked")
    typer.echo(
        f"attachments: {m.attachments_extracted} extracted, {m.attachments_failed} not extracted"
    )
    typer.echo(f"messages skipped (given up on): {m.messages_skipped}")
    if m.clock_skew:
        typer.echo(f"clock skew: {m.clock_skew} message(s) created before they were received")
    typer.echo(f"ingestion lag: p50 {m.lag_p50_seconds}s p95 {m.lag_p95_seconds}s")


@app.command("ingest-skips")
def ingest_skips(source: str | None = None) -> None:
    """List messages ingestion gave up on, so the loss is never silent (SPEC-10 F1)."""
    from bidtriage.core.db import session_scope
    from bidtriage.worker.ingest_job import pending_skips

    with session_scope() as s:
        rows = pending_skips(s, source_id=source)
        for r in rows:
            typer.echo(
                f"{r.source_id[:8]} {r.provider_message_id}: {r.attempts} attempts, "
                f"last {r.last_attempt_at:%Y-%m-%d %H:%M} — {(r.last_error or '')[:120]}"
            )
        typer.echo(f"{len(rows)} message(s) skipped and unreviewed")


@app.command("ingest-dir")
def ingest_dir(
    directory: Path,
    fake: bool = typer.Option(False, help="Use <name>.expected.json fixtures instead of Claude"),
) -> None:
    """Ingest every .eml in a directory synchronously through the whole pipeline (dev/demo)."""
    from sqlalchemy import select

    from bidtriage.core.blobs import get_blob_store
    from bidtriage.core.config import get_settings
    from bidtriage.core.db import session_scope
    from bidtriage.core.models import Source
    from bidtriage.ingestion.msg import parse_upload
    from bidtriage.worker import pipeline

    extractor = _extractor(directory if fake else None)
    settings = get_settings()
    blobs = get_blob_store()
    with session_scope() as s:
        src = s.scalar(select(Source).where(Source.kind == "file", Source.name == str(directory)))
        if src is None:
            # Nothing polls this source afterwards, so it is paused: an unpolled source would
            # otherwise read as `down` on the health page and trigger an alert (SPEC-01 F8).
            src = Source(
                kind="file",
                name=str(directory),
                status="active",
                backfill_done=True,
                paused=True,
            )
            s.add(src)
            s.flush()
        paths = sorted(p for pat in ("*.eml", "*.msg") for p in directory.glob(pat))
        parsed_all = [(p, parse_upload(p.name, p.read_bytes())) for p in paths]
        parsed_all.sort(
            key=lambda t: (t[1].sent_at is None, t[1].sent_at or datetime.min.replace(tzinfo=UTC))
        )
        for path, parsed in parsed_all:
            msg, is_new = pipeline.ingest_parsed(
                s,
                source_id=src.id,
                provider_message_id=path.name,
                parsed=parsed,
                recipient_path=src.mailbox or str(directory),
                ocr=settings.ingest_ocr,
                blobs=blobs,
            )
            if not is_new:
                typer.echo(f"{path.name}: duplicate of {msg.id}")
                continue
            ext = pipeline.extract_message(s, msg, extractor, external_ref=path.stem)
            if ext is None:
                typer.echo(f"{path.name}: skipped (not bid)")
                continue
            from bidtriage.extraction.schema import EXTRACTABLE_KINDS, Kind

            if Kind(msg.kind or "not_bid") not in EXTRACTABLE_KINDS:
                typer.echo(f"{path.name}: {msg.kind}")
                continue
            opp, decision = pipeline.resolve_message(s, msg, ext)
            res = pipeline.score_opportunity(s, opp, home=(settings.home_lat, settings.home_lon))
            typer.echo(
                f"{path.name}: {msg.kind} -> {decision} opp={opp.id[:8]} score={res.score} {res.band}"
            )


@app.command("digest-preview")
def digest_preview(
    out: Path = Path("var/digest-preview.html"), recipient: str | None = None
) -> None:
    """Build (do not send) the digest and write the HTML to a file."""
    from sqlalchemy import select

    from bidtriage.core.db import session_scope
    from bidtriage.core.models import User
    from bidtriage.worker.digest_job import build_and_send
    from bidtriage.worker.handlers import Context

    out.parent.mkdir(parents=True, exist_ok=True)
    with session_scope() as s:
        user = (
            s.scalar(select(User).where(User.email == recipient))
            if recipient
            else s.scalars(select(User)).first()
        )
        if user is None:
            user = User(email="preview@example.com", name="Preview", role="chief")
            s.add(user)
            s.flush()
        digests = build_and_send(
            s,
            Context(None),
            date=None,
            recipient_id=user.id,
            send=False,
        )
        out.write_text(digests[0].html, encoding="utf-8")
    typer.echo(f"wrote {out}")


@app.command("eval-extraction")
def eval_extraction(
    fixtures: Path,
    offline: bool = typer.Option(
        False, help="Only validate fixtures and post-processing; no API calls"
    ),
) -> None:
    """Run every fixture through the extractor and compare to .expected.json (SPEC-02 metrics)."""
    from bidtriage.extraction.fake import FakeExtractor
    from bidtriage.extraction.protocol import ExtractionInput
    from bidtriage.extraction.schema import LLMExtraction
    from bidtriage.ingestion.eml import parse_eml

    paths = sorted(fixtures.glob("*.eml"))
    if not paths:
        typer.echo("no fixtures")
        raise typer.Exit(1)
    extractor = FakeExtractor(fixtures) if offline else _extractor(None)
    total = ok_kind = ok_due = ok_gc = ok_type = 0
    for p in paths:
        exp_path = p.with_suffix(".expected.json")
        if not exp_path.exists():
            continue
        expected = LLMExtraction.model_validate(json.loads(exp_path.read_text()))
        parsed = parse_eml(p.read_bytes())
        item = ExtractionInput(
            message_id=p.stem,
            subject=parsed.subject,
            from_addr=parsed.from_addr,
            from_name=parsed.from_name,
            to=parsed.to,
            sent_at=parsed.sent_at or datetime.now(tz=UTC),
            body=parsed.body_trimmed,
            external_ref=p.stem,
        )
        got = extractor.extract(item)
        total += 1
        ok_kind += got.kind == expected.kind
        ok_gc += (got.gc_name.value or "").lower() == (expected.gc_name.value or "").lower()
        ok_type += got.project_type == expected.project_type
        exp_due = expected.bid_due.value[:10] if expected.bid_due.value else None
        got_due = got.bid_due.value.date().isoformat() if got.bid_due.value else None
        ok_due += exp_due == got_due
        typer.echo(
            f"{p.name}: kind={got.kind.value} due={got_due} gc={got.gc_name.value} type={got.project_type.value}"
        )
    typer.echo(
        f"\n{total} fixtures · kind {ok_kind}/{total} · due date {ok_due}/{total} · gc {ok_gc}/{total} · type {ok_type}/{total}"
    )
    if total and ok_due / total < 0.97:
        typer.echo("FAIL: due-date accuracy below 97%")
        raise typer.Exit(2)


@app.command()
def calibrate(labeled_csv: Path) -> None:
    """Score a labeled corpus (columns: opportunity_id,label) and print band precision/recall (SPEC-04)."""
    import csv

    from sqlalchemy import select

    from bidtriage.core.db import session_scope
    from bidtriage.core.models import Opportunity, Score

    if not labeled_csv.exists():
        typer.echo(f"{labeled_csv} not found; export the chief estimator's labels first (Phase 0).")
        raise typer.Exit(1)
    labels = {r["opportunity_id"]: r["label"] for r in csv.DictReader(labeled_csv.open())}
    tp = fp = fn = 0
    with session_scope() as s:
        for o in s.scalars(select(Opportunity)).all():
            sc = s.scalars(
                select(Score).where(Score.opportunity_id == o.id).order_by(Score.computed_at.desc())
            ).first()
            if o.id not in labels or sc is None:
                continue
            pred_bid = sc.band == "bid"
            truth_bid = labels[o.id] == "bid"
            tp += pred_bid and truth_bid
            fp += pred_bid and not truth_bid
            fn += (not pred_bid) and truth_bid
    p = tp / (tp + fp) if tp + fp else 0
    r = tp / (tp + fn) if tp + fn else 0
    typer.echo(f"bid band: precision {p:.2f} recall {r:.2f} (tp={tp} fp={fp} fn={fn})")


if __name__ == "__main__":
    app()
