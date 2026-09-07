import json
import pytest
from contracts.no_claim import NoClaim
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


def _buy(monkeypatch, c, clock, buyer=BUYER, payout=10 * GEN, premium=GEN, cover_in=600):
    _as(monkeypatch, buyer, premium)
    res = c.buy_policy(
        trigger="Will flight VN123 be delayed more than two hours?",
        criteria="FIRED if the source shows an arrival delay above 120 minutes.",
        sources="https://example.org/flight",
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


def test_unknown_refunds_the_premium(monkeypatch, clock, as_owner):
    """The design decision this contract exists to make.

    Real insurance resolves doubt in the insurer's favour. Here nobody proved
    anything, so nobody is charged for it: the premium goes back in full and the
    pool keeps none of it.
    """
    c = NoClaim()
    _fund(monkeypatch, c, UNDERWRITER, 100 * GEN)
    pid = _buy(monkeypatch, c, clock, payout=10 * GEN, premium=GEN)

    _verdict(monkeypatch, "UNKNOWN", "the source did not report this flight")
    clock["now"] += 601
    res = c.settle_policy(int(pid))

    assert res["outcome"] == "UNKNOWN"
    assert res["settlement"] == "REFUNDED"
    assert c.get_balance(BUYER) == str(GEN), "the premium must come back in full"

    pool = c.get_pool()
    assert int(pool["premiums_earned"]) == 0, "an unadjudicated risk earns nothing"
    assert int(pool["total"]) == 100 * GEN, "the pool is exactly where it started"
    assert int(pool["reserved"]) == 0


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
