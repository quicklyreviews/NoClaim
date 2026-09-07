"""Put settleable cover on the board, and prove the money actually moves.

Not a test - a seeding run against the deployed contract. It does two jobs.

**One.** It writes three policies, each expiring in a few minutes, chosen so
that between them they exercise every settlement path the contract has: one the
evidence will confirm, one it will deny, and one it cannot judge at all. They
are left unsettled on purpose. Settlement is permissionless, so anybody -
including someone demoing the site who has never touched this script - can walk
up and adjudicate them.

**Two.** It follows the money end to end and prints the wallet balance at each
step, because "the transaction succeeded" and "the GEN arrived" are different
claims and only the second one matters. `emit_transfer` does not move anything
inside the call that asks for it: the contract emits a transfer, and that
becomes a second transaction reaching consensus a minute or so later. So every
payout here is confirmed by watching the recipient's own balance rise, never by
trusting a receipt.

    gltest tests/integration/demo_cover.py --network studionet -v -s

Budget fifteen minutes. Cover cannot be written for less than five, every
transaction waits for consensus, and the payout waits again after that.
"""

import json
import os
import time

import pytest
import requests
from genlayer_py import create_account
from gltest import get_contract_factory, get_accounts, get_gl_client
from gltest.assertions import tx_execution_succeeded


GEN = 10**18
RPC = "https://studio.genlayer.com/api"
CONTRACT = os.environ.get(
    "NOCLAIM_CONTRACT", "0xBF326FA29B839cF95d3c9d0895b7A852031C3822"
)

# Six minutes: over the contract's five-minute floor, and short enough that
# somebody watching a demo will not lose interest before it can be settled.
COVER_SECONDS = 360

PRICE_SOURCES = ",".join([
    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    "https://api.coinbase.com/v2/prices/BTC-USD/spot",
])

# One policy per settlement path. The thresholds are absurd on purpose: what is
# being demonstrated is the contract's behaviour, not the model's cleverness, so
# the right answer has to be beyond argument in all three cases.
POLICIES = [
    {
        "name": "PAID",
        "trigger": "Is Bitcoin trading above 1,000 USD?",
        "criteria": (
            "Read the BTC/USD price from any source body. Answer FIRED if that price "
            "is above 1000. Answer NOT_FIRED if it is 1000 or below. If no source "
            "returned a usable price, answer UNKNOWN."
        ),
        "sources": PRICE_SOURCES,
        "payout": 2 * GEN,
        "premium": GEN // 10,
        "expect": "the sum insured is credited to the holder",
    },
    {
        "name": "EXPIRED",
        "trigger": "Has Bitcoin fallen below 1,000 USD?",
        "criteria": (
            "Read the BTC/USD price from any source body. Answer FIRED if that price "
            "is below 1000. Answer NOT_FIRED if it is 1000 or above. If no source "
            "returned a usable price, answer UNKNOWN."
        ),
        "sources": PRICE_SOURCES,
        "payout": 2 * GEN,
        "premium": GEN // 10,
        "expect": "nothing is paid and the pool earns the premium",
    },
    {
        "name": "REFUNDED",
        "trigger": "Did the policyholder's cat sleep through the night?",
        "criteria": (
            "Answer FIRED only if a source explicitly reports that the cat slept "
            "through the night, and NOT_FIRED only if a source explicitly reports "
            "that it did not. If the sources say nothing about any cat, answer UNKNOWN."
        ),
        "sources": PRICE_SOURCES,
        "payout": 2 * GEN,
        "premium": GEN // 20,
        "expect": "the premium comes back in full and the pool earns nothing",
    },
]


def balance(address, tries=6):
    """The wallet's own view, read straight off the chain."""
    last = None
    for _ in range(tries):
        try:
            body = requests.post(
                RPC,
                json={"jsonrpc": "2.0", "method": "eth_getBalance",
                      "params": [address, "latest"], "id": 1},
                timeout=60,
            ).json()
            return int(body["result"], 16)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(4)
    raise RuntimeError(f"could not read balance of {address}: {last}")


def wait_for_credit(address, before, label, seconds=180):
    """Watch until the money is actually there.

    The whole point of this file. A receipt says the contract agreed to pay;
    only the balance says it paid.
    """
    print(f"    waiting for {label} to land...", flush=True)
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(6)
        now = balance(address)
        if now > before:
            print(f"    ARRIVED: +{(now - before) / GEN:.3f} GEN "
                  f"({before / GEN:.3f} -> {now / GEN:.3f})")
            return now
    raise AssertionError(
        f"{label}: nothing arrived at {address} within {seconds}s "
        f"(still {before / GEN:.3f} GEN)"
    )


def fund(address, amount, tries=5):
    last = None
    for _ in range(tries):
        try:
            get_gl_client().fund_account(address, amount)
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"could not fund {address}: {last}")


@pytest.fixture(scope="module")
def desk():
    factory = get_contract_factory("NoClaim")
    holder = create_account()
    fund(holder.address, 30 * GEN)
    contract = factory.build_contract(contract_address=CONTRACT, account=holder)

    pool = contract.get_pool(args=[]).call()
    print(f"\nNoClaim at {CONTRACT}")
    print(f"pool: {int(pool['total']) / GEN:.2f} GEN in, "
          f"{int(pool['free']) / GEN:.2f} free to underwrite")
    print(f"demo holder: {holder.address}")
    return contract, holder


def test_write_three_settleable_policies(desk):
    """Leave one policy on the board per settlement path, ready to adjudicate."""
    contract, holder = desk
    expiry = int(time.time()) + COVER_SECONDS
    written = []

    for spec in POLICIES:
        quote = contract.quote(args=[spec["payout"]]).call()
        assert int(quote["can_write"]) == 1, (
            f"pool cannot back {spec['payout'] / GEN} GEN of cover right now"
        )
        premium = max(spec["premium"], int(quote["min_premium"]))

        assert tx_execution_succeeded(
            contract.connect(holder).buy_policy(
                args=[spec["trigger"], spec["criteria"], spec["sources"],
                      spec["payout"], expiry]
            ).transact(value=premium)
        ), f"could not write the {spec['name']} policy"

        policies = json.loads(contract.get_all_policies(args=[]).call() or "{}")
        pid = max(int(k) for k in policies)
        written.append((pid, spec))
        print(f"\npolicy {pid}: {spec['trigger']}")
        print(f"  {spec['payout'] / GEN:.1f} GEN cover for {premium / GEN:.3f} premium")
        print(f"  on settlement, expect: {spec['expect']}")

    ready_in = expiry - int(time.time())
    print(f"\n{len(written)} policies written, settleable in about {ready_in // 60}m "
          f"{ready_in % 60}s.")
    print("Anyone can settle them - that is the point. Open Your cover or Every "
          "policy and click Settle now.")
    desk_state = contract.get_pool(args=[]).call()
    print(f"pool now reserves {int(desk_state['reserved']) / GEN:.2f} GEN "
          f"against live cover")


@pytest.mark.slow
def test_money_reaches_the_holders_wallet(desk):
    """Settle everything, collect, and prove the GEN is in the wallet.

    This is the claim the interface makes on every payout, checked the only way
    it can honestly be checked: against the recipient's balance.
    """
    contract, holder = desk
    policies = json.loads(contract.get_all_policies(args=[]).call() or "{}")
    mine = [p for p in policies.values()
            if p["holder"].lower() == holder.address.lower()
            and p["status"] == "ACTIVE"]
    assert mine, "no policies of ours are waiting to be settled"

    last_expiry = max(int(p["expires_ts"]) for p in mine)
    wait = last_expiry - int(time.time()) + 5
    if wait > 0:
        print(f"\nwaiting {wait}s for the last policy to expire...")
        time.sleep(wait)

    outcomes = {}
    for p in sorted(mine, key=lambda x: int(x["id"])):
        pid = int(p["id"])
        print(f"\nsettling policy {pid}: {p['trigger']}")
        assert tx_execution_succeeded(
            contract.connect(holder).settle_policy(args=[pid]).transact()
        ), f"settlement of policy {pid} did not reach consensus"
        fresh = contract.get_policy(args=[pid]).call()
        outcomes[pid] = fresh["settlement"]
        print(f"  {fresh['outcome']} / {fresh['settlement']}")
        print(f"  {fresh['reasoning']}")

    assert "PAID" in outcomes.values(), (
        f"expected one policy to pay out, got {outcomes}"
    )

    owed = int(contract.get_balance(args=[holder.address]).call())
    print(f"\ncontract owes the holder {owed / GEN:.3f} GEN")
    assert owed > 0, "settlement credited nothing, so there is nothing to collect"

    before = balance(holder.address)
    print(f"holder wallet before collecting: {before / GEN:.3f} GEN")
    assert tx_execution_succeeded(
        contract.connect(holder).withdraw_all(args=[]).transact()
    ), "withdraw_all did not reach consensus"

    after = wait_for_credit(holder.address, before, "the collected payout")
    assert after - before >= owed * 0.99, (
        f"expected about {owed / GEN:.3f} GEN, wallet rose by "
        f"{(after - before) / GEN:.3f}"
    )
    assert int(contract.get_balance(args=[holder.address]).call()) == 0, \
        "the contract still says it owes something after a full withdrawal"


@pytest.mark.slow
def test_pool_capital_comes_back_to_the_wallet(desk):
    """Underwrite, then take it back out, and check the wallet actually grew."""
    contract, holder = desk
    stake = 3 * GEN

    before_fund = balance(holder.address)
    assert tx_execution_succeeded(
        contract.connect(holder).fund_pool(args=[]).transact(value=stake)
    ), "fund_pool did not reach consensus"
    assert int(contract.get_underwriter(args=[holder.address]).call()) == stake, \
        "the pool did not record the stake"
    print(f"\nunderwrote {stake / GEN:.1f} GEN "
          f"(wallet was {before_fund / GEN:.3f})")

    before_withdraw = balance(holder.address)
    assert tx_execution_succeeded(
        contract.connect(holder).withdraw_pool(args=[stake]).transact()
    ), "withdraw_pool did not reach consensus"

    after = wait_for_credit(holder.address, before_withdraw, "the withdrawn capital")
    assert after - before_withdraw >= stake * 0.99, (
        f"expected about {stake / GEN:.1f} GEN back, wallet rose by "
        f"{(after - before_withdraw) / GEN:.3f}"
    )
    assert int(contract.get_underwriter(args=[holder.address]).call()) == 0, \
        "the pool still records a stake after a full withdrawal"
