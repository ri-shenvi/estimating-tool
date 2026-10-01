"""`bidtriage` command line."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
        for src in sources:
            impl = ctx.source_impl(src)
            summary = ingest_job.run_poll(s, src, impl, ctx)
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


def load_eval_cases(fixtures: Path) -> list[Any]:
    """Every `.eml` in `fixtures` that has an adjacent `.expected.json`, parsed as an eval case."""
    from bidtriage.extraction.evaluate import EvalCase
    from bidtriage.extraction.protocol import AttachmentText, ExtractionInput
    from bidtriage.extraction.schema import LLMExtraction
    from bidtriage.ingestion.attachments import extract_text
    from bidtriage.ingestion.eml import parse_eml
    from bidtriage.ingestion.links import harvest_links

    cases = []
    for path in sorted(fixtures.glob("*.eml")):
        expectation = path.with_suffix(".expected.json")
        if not expectation.exists():
            continue
        parsed = parse_eml(path.read_bytes())
        attachments = []
        for a in parsed.attachments:
            text = extract_text(a.filename, a.mime, a.data)
            attachments.append(
                AttachmentText(
                    filename=a.filename,
                    text=text.text or "",
                    large_document=text.large_document,
                )
            )
        cases.append(
            EvalCase(
                name=path.name,
                item=ExtractionInput(
                    message_id=path.stem,
                    subject=parsed.subject,
                    from_addr=parsed.from_addr,
                    from_name=parsed.from_name,
                    to=parsed.to,
                    sent_at=parsed.sent_at or datetime.now(tz=UTC),
                    body=parsed.body_trimmed,
                    attachments=attachments,
                    links=[
                        str(lk["url"]) for lk in harvest_links(parsed.body_text, parsed.body_html)
                    ],
                    external_ref=path.stem,
                ),
                expected=LLMExtraction.model_validate(json.loads(expectation.read_text())),
            )
        )
    return cases


@app.command("eval-extraction")
def eval_extraction(
    fixtures: Path,
    offline: bool = typer.Option(
        False,
        help="Validate the fixture corpus only: schema, post-processing and the F2 invariants. "
        "No API calls, and no accuracy measurement — see --help notes.",
    ),
) -> None:
    """Measure extraction accuracy against the fixture corpus (SPEC-02).

    Without `--offline` this calls the configured model once per fixture and gates on the SPEC-02
    goals: due date 97%, GC name 98%, project type 90%, size band 80%, ITB boundary 97%.

    With `--offline` it checks the corpus instead. It cannot report accuracy: the offline extractor
    reads the same `.expected.json` the comparison uses, so every field would match however broken
    the post-processor is. Post-processing regressions are caught by `tests/test_extraction_fixtures.py`.
    """
    from bidtriage.extraction.evaluate import (
        evaluate,
        format_corpus_report,
        format_report,
        validate_corpus,
    )

    cases = load_eval_cases(fixtures)
    if not cases:
        typer.echo(f"no fixtures with an .expected.json in {fixtures}")
        raise typer.Exit(1)

    if offline:
        corpus = validate_corpus(cases)
        for line in format_corpus_report(corpus):
            typer.echo(line)
        if not corpus.ok:
            raise typer.Exit(2)
        return

    report = evaluate(cases, _extractor(None))
    for line in format_report(report):
        typer.echo(line)
    failures = report.gate_failures
    if failures or report.errors:
        for f in failures:
            typer.echo(f"FAIL: {f.name} accuracy {f.ratio:.0%} below {f.threshold:.0%}")
        raise typer.Exit(2)


@app.command("reextract")
def reextract(
    message_id: str = typer.Argument("", help="Message id; omit to sweep a stale prompt version"),
    fake_fixtures: Path | None = typer.Option(None, help="Fixture-backed extractor"),
    window_days: int | None = typer.Option(
        None, help="Sweep window for a prompt bump (default REEXTRACTION_WINDOW_DAYS)"
    ),
    queue: bool = typer.Option(
        False, help="Queue the work for the worker instead of running it now"
    ),
) -> None:
    """Re-extract one message, or queue re-extraction of everything on an older prompt (SPEC-02 F4)."""
    from bidtriage.core.config import get_settings
    from bidtriage.core.db import session_scope
    from bidtriage.core.models import RawMessage
    from bidtriage.extraction.prompts import PROMPT_VERSION
    from bidtriage.worker import pipeline
    from bidtriage.worker.handlers import Context

    settings = get_settings()
    with session_scope() as s:
        if not message_id:
            n = pipeline.enqueue_stale_prompt_reextractions(
                s, window_days=window_days or settings.reextraction_window_days
            )
            typer.echo(f"queued {n} message(s) for re-extraction on prompt {PROMPT_VERSION}")
            return
        msg = s.get(RawMessage, message_id)
        if msg is None:
            typer.echo(f"no such message {message_id}")
            raise typer.Exit(1)
        if queue:
            from bidtriage.core.jobs import enqueue

            enqueue(
                s,
                "reextract_message",
                f"reextract:{msg.id}:cli:{datetime.now(tz=UTC).isoformat()}",
                {"message_id": msg.id},
                priority=60,
            )
            typer.echo(f"queued re-extraction of {msg.id}")
            return
        ctx = Context(_extractor(fake_fixtures))
        ext = pipeline.reextract_message(
            s,
            msg,
            ctx.extractor,
            external_ref=pipeline.external_ref_for(s, msg),
            geocoder=ctx.geocoder_for(s),
        )
        typer.echo(
            f"{msg.id}: {msg.kind} (v{ext.version} prompt {ext.prompt_version})"
            if ext
            else f"{msg.id}: skipped by the pre-filter"
        )


@app.command()
def calibrate(
    labeled_csv: Path,
    gate: bool = typer.Option(True, help="Exit non-zero when the SPEC-04 goals are not met."),
) -> None:
    """Score a labeled corpus (columns: opportunity_id,label) and print the SPEC-04 calibration report."""
    import csv

    from sqlalchemy import select

    from bidtriage.core.db import session_scope
    from bidtriage.core.models import GC, Opportunity, Score
    from bidtriage.scoring.calibrate import LabeledScore, report

    if not labeled_csv.exists():
        typer.echo(f"{labeled_csv} not found; export the chief estimator's labels first (Phase 0).")
        raise typer.Exit(1)
    with labeled_csv.open() as fh:
        labels = {r["opportunity_id"]: r["label"] for r in csv.DictReader(fh)}
    rows: list[LabeledScore] = []
    missing = 0
    with session_scope() as s:
        for o in s.scalars(select(Opportunity)).all():
            if o.id not in labels:
                continue
            sc = s.scalars(
                select(Score)
                .where(Score.opportunity_id == o.id)
                .order_by(Score.computed_at.desc(), Score.id.desc())
            ).first()
            if sc is None:
                missing += 1
                continue
            gc = s.get(GC, o.gc_id) if o.gc_id else None
            rows.append(
                LabeledScore(
                    opportunity_id=o.id,
                    label=labels[o.id],
                    band=sc.band,
                    score=sc.score,
                    project_type=o.canonical.get("project_type", "unknown"),
                    gc_tier=gc.tier if gc else "unknown",
                )
            )
    text, passed = report(rows)
    typer.echo(text)
    unknown = len(labels) - len(rows) - missing
    if missing or unknown:
        typer.echo(
            f"\n{missing} labelled opportunit(y/ies) have no score yet; {unknown} label(s) match no opportunity."
        )
    if gate and not passed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
