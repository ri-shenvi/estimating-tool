from bidtriage.core.models import Base


def test_all_tables_create(session):  # type: ignore[no-untyped-def]
    names = set(Base.metadata.tables)
    assert {
        "raw_messages",
        "opportunities",
        "gcs",
        "scores",
        "jobs",
        "audit_events",
        "digests",
    } <= names
