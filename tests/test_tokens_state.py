from datetime import UTC, datetime, timedelta

import pytest

from bidtriage.decisions import (
    InvalidTransitionError,
    TokenError,
    sign_action,
    transition,
    verify_action,
)

KEY = "test-secret"


def test_roundtrip():  # type: ignore[no-untyped-def]
    t = sign_action("opp1", "bid", "u1", KEY)
    a = verify_action(t, KEY)
    assert (a.opportunity_id, a.action, a.recipient_id) == ("opp1", "bid", "u1")


def test_tamper():  # type: ignore[no-untyped-def]
    t = sign_action("opp1", "bid", "u1", KEY)
    v, p, s = t.split(".")
    bad = f"{v}.{p[:-2]}xx.{s}"
    with pytest.raises(TokenError):
        verify_action(bad, KEY)
    with pytest.raises(TokenError):
        verify_action(t, "other-key")


def test_expired_and_malformed():  # type: ignore[no-untyped-def]
    t = sign_action("opp1", "bid", "u1", KEY, issued_at=datetime.now(tz=UTC) - timedelta(days=8))
    with pytest.raises(TokenError, match="expired"):
        verify_action(t, KEY)
    with pytest.raises(TokenError):
        verify_action("nonsense", KEY)


def test_transitions():  # type: ignore[no-untyped-def]
    assert transition("new", "bidding") == "bidding"
    assert transition("bidding", "submitted") == "submitted"
    assert transition("submitted", "won") == "won"
    with pytest.raises(InvalidTransitionError):
        transition("won", "bidding")
    with pytest.raises(InvalidTransitionError):
        transition("new", "won")
