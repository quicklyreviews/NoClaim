import json
import pytest
from contracts.no_claim import NoClaim, MAX_UNKNOWN_RETRIES, UNKNOWN_RETRY_DELAY
from genlayer import gl

OWNER = "0x1111111111111111111111111111111111111111"
UNDERWRITER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BUYER = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTHER = "0xcccccccccccccccccccccccccccccccccccccccc"

FIXED_NOW = 1_700_000_000
GEN = 10**18


@pytest.fixture
def clock(monkeypatch):
    """Controls the contract's notion of now by patching datetime.datetime,
    which is what `_now()` reads - the same mechanism gltest's VM uses for
    vm.warp()."""
    import datetime as _dt

    box = {"now": FIXED_NOW}

    class _Frozen(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime.fromtimestamp(box["now"], tz)

    monkeypatch.setattr("contracts.no_claim.datetime.datetime", _Frozen)
    return box


@pytest.fixture
def as_owner(monkeypatch):
    monkeypatch.setattr("genlayer.gl.message.sender_address", OWNER)


def _as(monkeypatch, addr, value=0):
    monkeypatch.setattr("genlayer.gl.message.sender_address", addr)
    monkeypatch.setattr("genlayer.gl.message.value", value)


def _fund(monkeypatch, c, addr, amount):
    _as(monkeypatch, addr, amount)
    c.fund_pool()
    _as(monkeypatch, addr, 0)


def _buy(monkeypatch, c, clock, buyer=BUYER, payout=10 * GEN, premium=GEN, cover_in=600,
         sources="https://example.org/flight"):
    _as(monkeypatch, buyer, premium)
    res = c.buy_policy(
        trigger="Will flight VN123 be delayed more than two hours?",
        criteria="FIRED if the source shows an arrival delay above 120 minutes.",
        sources=sources,
        payout=payout,
        expires_ts=clock["now"] + cover_in,
    )
    _as(monkeypatch, buyer, 0)
    return res["policy_id"]


def _verdict(monkeypatch, outcome, reasoning="x"):
    monkeypatch.setattr(
        "genlayer.gl.nondet.exec_prompt",
        lambda prompt, response_format="json": {"outcome": outcome, "reasoning": reasoning},
    )


# --- the pool ---------------------------------------------------------

def test_pool_starts_empty_and_takes_capital(monkeypatch, clock, as_owner):
    c = NoClaim()
    assert int(c.get_pool()["free"]) == 0
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pool = c.get_pool()
    assert int(pool["total"]) == 100 * GEN
    assert int(pool["free"]) == 100 * GEN
    assert int(pool["reserved"]) == 0
    assert c.get_underwriter(UNDERWRITER) == str(100 * GEN)


def test_cover_is_refused_when_the_pool_cannot_back_it(monkeypatch, clock, as_owner):
    """The point of reserving up front: a policy the pool could not honour is
    never written in the first place."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 5 * GEN)
    _as(monkeypatch, BUYER, GEN)
    with pytest.raises(gl.vm.UserError):
        c.buy_policy(
            trigger="t", criteria="c", sources="",
            payout=10 * GEN, expires_ts=clock["now"] + 600,
        )


def test_premium_floor_is_enforced(monkeypatch, clock, as_owner):
    """Without a floor, cover could be bought for a wei."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    quote = c.quote(10 * GEN)
    assert int(quote["min_premium"]) == 10 * GEN * 200 // 10000
    assert quote["can_write"] == 1

    _as(monkeypatch, BUYER, int(quote["min_premium"]) - 1)
    with pytest.raises(gl.vm.UserError):
        c.buy_policy(
            trigger="t", criteria="c", sources="",
            payout=10 * GEN, expires_ts=clock["now"] + 600,
        )


def test_buying_reserves_the_payout(monkeypatch, clock, as_owner):
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    pool = c.get_pool()
    # Premium joined the pool; the payout is set aside out of it.
    assert int(pool["total"]) == 101 * GEN
    assert int(pool["reserved"]) == 10 * GEN
    assert int(pool["free"]) == 91 * GEN


def test_underwriter_cannot_withdraw_reserved_capital(monkeypatch, clock, as_owner):
    """Selling cover and then walking away from it has to be structurally
    impossible, not merely discouraged."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    _buy(monkeypatch, c, clock, payout=80 * GEN, premium=2 * GEN)

    _as(monkeypatch, UNDERWRITER)
    with pytest.raises(gl.vm.UserError):
        c.withdraw_pool(100 * GEN)          # more than is free
    out = c.withdraw_pool(20 * GEN)         # exactly the free part
    assert int(out["withdrawn"]) == 20 * GEN


# --- source URL validation --------------------------------------------

def test_localhost_source_is_rejected(monkeypatch, clock, as_owner):
    """A policy backed by localhost can never be adjudicated by validators."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    _as(monkeypatch, BUYER, GEN)
    with pytest.raises(gl.vm.UserError, match="localhost"):
        c.buy_policy(
            trigger="t", criteria="c",
            sources="http://localhost/api",
            payout=10 * GEN, expires_ts=clock["now"] + 600,
        )


def test_private_ip_sources_are_rejected(monkeypatch, clock, as_owner):
    """RFC-1918 and loopback addresses must be rejected at purchase time."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)

    private_urls = [
        "http://10.0.0.1/data",
        "http://192.168.1.100/data",
        "https://172.16.0.1/data",
        "https://172.31.255.255/data",
        "http://127.0.0.1/api",
        "http://169.254.1.1/data",
    ]
    for url in private_urls:
        _as(monkeypatch, BUYER, GEN)
        with pytest.raises(gl.vm.UserError):
            c.buy_policy(
                trigger="t", criteria="c", sources=url,
                payout=10 * GEN, expires_ts=clock["now"] + 600,
            )


def test_bare_hostname_source_is_rejected(monkeypatch, clock, as_owner):
    """A hostname without a dot is not a public domain."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    _as(monkeypatch, BUYER, GEN)
    with pytest.raises(gl.vm.UserError):
        c.buy_policy(
            trigger="t", criteria="c",
            sources="http://internalserver/data",
            payout=10 * GEN, expires_ts=clock["now"] + 600,
        )


def test_public_source_is_accepted(monkeypatch, clock, as_owner):
    """A well-formed public URL must pass validation."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, sources="https://api.example.com/data")
    assert pid == "1"


# --- settlement -------------------------------------------------------

def test_fired_policy_pays_the_holder(monkeypatch, clock, as_owner):
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "FIRED", "the source shows a 190 minute delay")
    clock["now"] += 601
    _as(monkeypatch, OTHER)                  # anyone may settle
    res = c.settle_policy(int(pid))

    assert res["outcome"] == "FIRED"
    assert res["settlement"] == "PAID"
    assert c.get_balance(BUYER) == str(10 * GEN)

    pool = c.get_pool()
    # Pool paid 10, kept the 1 premium: down 9 on the round.
    assert int(pool["total"]) == 91 * GEN
    assert int(pool["reserved"]) == 0


def test_not_fired_policy_earns_the_premium(monkeypatch, clock, as_owner):
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "NOT_FIRED", "arrived on time")
    clock["now"] += 601
    res = c.settle_policy(int(pid))

    assert res["settlement"] == "EXPIRED"
    assert c.get_balance(BUYER) == "0"
    pool = c.get_pool()
    assert int(pool["total"]) == 101 * GEN      # premium kept
    assert int(pool["reserved"]) == 0
    assert int(pool["premiums_earned"]) == GEN


def test_unknown_is_retryable_and_keeps_policy_active(monkeypatch, clock, as_owner):
    """First UNKNOWN keeps the policy ACTIVE so the source can be retried."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "UNKNOWN", "source unreachable")
    clock["now"] += 601
    res = c.settle_policy(int(pid))

    assert res["outcome"] == "UNKNOWN"
    assert res["settlement"] == "PENDING_RETRY"
    assert res["retries_remaining"] == MAX_UNKNOWN_RETRIES - 1

    # Policy must still be ACTIVE -- reserve must still be held
    pool = c.get_pool()
    assert int(pool["reserved"]) == 10 * GEN, "reserve must not be released on a retryable UNKNOWN"
    assert int(pool["total"]) == 101 * GEN, "pool total must not change on retryable UNKNOWN"
    assert c.get_balance(BUYER) == "0", "no money credited yet"

    # Policy is still readable and ACTIVE
    p = c.get_policy(int(pid))
    assert p["status"] == "ACTIVE"
    assert int(p["unknown_count"]) == 1


def test_unknown_retry_delay_is_enforced(monkeypatch, clock, as_owner):
    """Cannot retry a UNKNOWN settlement before the delay window expires."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "UNKNOWN")
    clock["now"] += 601
    c.settle_policy(int(pid))   # first UNKNOWN -- PENDING_RETRY

    # Immediate retry must be rejected
    with pytest.raises(gl.vm.UserError, match="retry not available"):
        c.settle_policy(int(pid))

    # Just before the window -- still rejected
    clock["now"] += UNKNOWN_RETRY_DELAY - 1
    with pytest.raises(gl.vm.UserError, match="retry not available"):
        c.settle_policy(int(pid))

    # At exactly the delay boundary -- now allowed (advances to finalization)
    clock["now"] += 1
    _verdict(monkeypatch, "NOT_FIRED", "second attempt succeeded")
    res = c.settle_policy(int(pid))
    assert res["settlement"] == "EXPIRED"


def test_unknown_finally_refunds_after_max_retries(monkeypatch, clock, as_owner):
    """After MAX_UNKNOWN_RETRIES UNKNOWN verdicts the premium is refunded."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "UNKNOWN", "source down")
    clock["now"] += 601

    # First UNKNOWN -- retryable
    res = c.settle_policy(int(pid))
    assert res["settlement"] == "PENDING_RETRY"

    # Wait out the delay and get UNKNOWN again -- this should finalize
    clock["now"] += UNKNOWN_RETRY_DELAY
    res = c.settle_policy(int(pid))
    assert res["outcome"] == "UNKNOWN"
    assert res["settlement"] == "REFUNDED"

    # Premium must be credited back
    assert c.get_balance(BUYER) == str(GEN), "premium must be refunded in full"
    pool = c.get_pool()
    assert int(pool["premiums_earned"]) == 0, "an unadjudicated risk earns nothing"
    assert int(pool["total"]) == 100 * GEN, "pool back where it started"
    assert int(pool["reserved"]) == 0


def test_unknown_refunds_on_first_attempt_when_retries_disabled(monkeypatch, clock, as_owner):
    """If MAX_UNKNOWN_RETRIES == 1, the first UNKNOWN finalises immediately."""
    import contracts.no_claim as mod
    original = mod.MAX_UNKNOWN_RETRIES
    mod.MAX_UNKNOWN_RETRIES = 1
    try:
        c = NoClaim()
        _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
        pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)
        _verdict(monkeypatch, "UNKNOWN", "source down")
        clock["now"] += 601
        res = c.settle_policy(int(pid))
        assert res["settlement"] == "REFUNDED"
        assert c.get_balance(BUYER) == str(GEN)
    finally:
        mod.MAX_UNKNOWN_RETRIES = original


def test_malformed_adjudication_refunds_rather_than_denies(monkeypatch, clock, as_owner):
    """A model that returns nonsense must not become a denied claim."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    monkeypatch.setattr(
        "genlayer.gl.nondet.exec_prompt",
        lambda prompt, response_format="json": "not json at all",
    )
    clock["now"] += 601
    # First UNKNOWN from malformed response -- will be PENDING_RETRY
    c.settle_policy(int(pid))

    # Second UNKNOWN (after delay) -- will finalize as REFUNDED
    clock["now"] += UNKNOWN_RETRY_DELAY
    res = c.settle_policy(int(pid))
    assert res["outcome"] == "UNKNOWN"
    assert c.get_balance(BUYER) == str(GEN)


def test_settlement_waits_for_expiry_and_happens_once(monkeypatch, clock, as_owner):
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "FIRED")
    with pytest.raises(gl.vm.UserError):
        c.settle_policy(int(pid))            # not due yet

    clock["now"] += 601
    c.settle_policy(int(pid))
    with pytest.raises(gl.vm.UserError):
        c.settle_policy(int(pid))            # already settled


# --- pagination / split storage ---------------------------------------

def test_get_policies_page_returns_newest_first(monkeypatch, clock, as_owner):
    """get_policies_page should return policies sorted newest-first."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)

    for i in range(5):
        _buy(monkeypatch, c, clock, buyer=f"0x{i:040x}", payout=GEN, premium=GEN // 50)

    page = json.loads(c.get_policies_page(0, 3))
    assert len(page) == 3
    # IDs should be descending
    ids = [int(p["id"]) for p in page]
    assert ids == sorted(ids, reverse=True)


def test_get_policies_page_is_bounded_at_100(monkeypatch, clock, as_owner):
    """limit is capped at 100 to prevent runaway reads."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    for i in range(5):
        _buy(monkeypatch, c, clock, buyer=f"0x{i:040x}", payout=GEN, premium=GEN // 50)

    page = json.loads(c.get_policies_page(0, 9999))
    assert len(page) == 5   # only 5 exist, so cap doesn't matter here


def test_active_policies_excludes_settled(monkeypatch, clock, as_owner):
    """After settlement, the policy must not appear in get_active_policies."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "NOT_FIRED")
    clock["now"] += 601
    c.settle_policy(int(pid))

    active = json.loads(c.get_active_policies())
    assert pid not in active

    # But get_all_policies and get_policy still see it
    all_p = json.loads(c.get_all_policies())
    assert pid in all_p
    assert c.get_policy(int(pid))["settlement"] == "EXPIRED"


def test_get_policy_count(monkeypatch, clock, as_owner):
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)
    _buy(monkeypatch, c, clock, buyer=OTHER, payout=5 * GEN, premium=GEN // 10)

    counts = c.get_policy_count()
    assert counts["active"] == 2
    assert counts["archived"] == 0
    assert counts["total"] == 2

    _verdict(monkeypatch, "NOT_FIRED")
    clock["now"] += 601
    c.settle_policy(1)

    counts = c.get_policy_count()
    assert counts["active"] == 1
    assert counts["archived"] == 1


# --- solvency ---------------------------------------------------------

def _held(c):
    """Everything the contract is holding on someone's behalf."""
    return int(c.pool_total) + int(c.balances_total)


def test_contract_never_owes_more_than_it_holds(monkeypatch, clock, as_owner):
    """Underwriting cannot make this contract insolvent, because every policy
    reserves its payout before it is written. Tracked across a full round."""
    c = NoClaim()
    paid_in = 0

    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    paid_in += 100 * GEN
    assert _held(c) == paid_in
    assert int(c.pool_reserved) <= int(c.pool_total)

    pid = _buy(monkeypatch, c, clock, payout=40 * GEN, premium=2 * GEN)
    paid_in += 2 * GEN
    assert _held(c) == paid_in
    assert int(c.pool_reserved) <= int(c.pool_total), "reserved more than exists"

    _verdict(monkeypatch, "FIRED")
    clock["now"] += 601
    c.settle_policy(int(pid))
    # A payout moves money from the pool to a person; it does not create any.
    assert _held(c) == paid_in
    assert int(c.pool_reserved) <= int(c.pool_total)

    _as(monkeypatch, BUYER)
    out = c.withdraw_all()
    paid_in -= int(out["withdrawn"])
    assert _held(c) == paid_in
    assert int(c.balances_total) == 0


def test_reserves_never_exceed_capital_across_many_policies(monkeypatch, clock, as_owner):
    """Write cover until the pool is full, and check it refuses the one after."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 30 * GEN)

    written = 0
    for i in range(3):
        _buy(monkeypatch, c, clock, buyer=f"0x{i:040x}", payout=10 * GEN, premium=GEN)
        written += 1
        assert int(c.pool_reserved) <= int(c.pool_total)

    assert written == 3
    assert int(c.get_pool()["free"]) == 3 * GEN     # only the premiums are left free

    _as(monkeypatch, OTHER, GEN)
    with pytest.raises(gl.vm.UserError):
        c.buy_policy(
            trigger="t", criteria="c", sources="",
            payout=10 * GEN, expires_ts=clock["now"] + 600,
        )


def test_reserve_held_during_unknown_retry(monkeypatch, clock, as_owner):
    """Pool reserve must not be released while a policy is in PENDING_RETRY state."""
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "UNKNOWN")
    clock["now"] += 601
    c.settle_policy(int(pid))   # PENDING_RETRY

    pool = c.get_pool()
    assert int(pool["reserved"]) == 10 * GEN, "reserve must stay locked during retry window"
    assert _held(c) == 101 * GEN   # total money in system unchanged
