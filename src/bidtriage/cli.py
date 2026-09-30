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


@app.command("ingest-dir")
def ingest_dir(
    directory: Path,
    fake: bool = typer.Option(False, help="Use <name>.expected.json fixtures instead of Claude"),
) -> None:
    """Ingest every .eml in a directory synchronously through the whole pipeline (dev/demo)."""
    from sqlalchemy import select

    from bidtriage.core.config import get_settings
    from bidtriage.core.db import session_scope
    from bidtriage.core.models import Source
    from bidtriage.ingestion.eml import parse_eml
    from bidtriage.worker import pipeline

    extractor = _extractor(directory if fake else None)
    settings = get_settings()
    with session_scope() as s:
        src = s.scalar(select(Source).where(Source.kind == "file", Source.name == str(directory)))
        if src is None:
            src = Source(kind="file", name=str(directory), status="active")
            s.add(src)
            s.flush()
        parsed_all = [(p, parse_eml(p.read_bytes())) for p in sorted(directory.glob("*.eml"))]
        parsed_all.sort(
            key=lambda t: (t[1].sent_at is None, t[1].sent_at or datetime.min.replace(tzinfo=UTC))
        )
        for path, parsed in parsed_all:
            msg, is_new = pipeline.ingest_parsed(
                s, source_id=src.id, provider_message_id=path.name, parsed=parsed
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
