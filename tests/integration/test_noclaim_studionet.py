"""NoClaim on a real network: underwrite, sell cover, settle it from evidence.

The direct tests prove the accounting against a mocked `genlayer` module. This
proves the thing that cannot be mocked - that independent validators fetch the
evidence themselves, adjudicate, and agree, and that the pool pays or refunds
accordingly.

    gltest tests/integration/test_noclaim_studionet.py --network studionet -v -s

Budget about ten minutes: cover cannot be written for less than five, and every
transaction waits for consensus.
"""

import json
import os
import time

import pytest
from gltest import get_contract_factory, get_accounts, get_gl_client
from gltest.assertions import tx_execution_succeeded


GEN = 10**18
MIN_COVER_SECONDS = 300
CONTRACT = os.environ.get("NOCLAIM_CONTRACT", "").strip()

# A trigger whose answer is not in doubt, because what is under test is whether
# validators agree, not whether the model can do something clever. Bitcoin is
# emphatically not below 1000 USD, so an honest adjudicator says NOT_FIRED and
# the pool keeps the premium.
TRIGGER = "Has Bitcoin fallen below 1,000 USD?"
CRITERIA = (
    "Read the BTC/USD price from any source body. Answer FIRED if that price is "
    "below 1000. Answer NOT_FIRED if it is 1000 or above. If no source returned a "
    "usable price, answer UNKNOWN."
)
SOURCES = ",".join([
    "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    "https://api.coinbase.com/v2/prices/BTC-USD/spot",
])

# A trigger nothing can settle: the sources say nothing about it. This is the
# case the contract is built around, so it is the one worth paying to observe.
UNKNOWABLE = "Did the policyholder's cat sleep through the night?"
UNKNOWABLE_CRITERIA = (
    "Answer FIRED only if a source explicitly reports that the cat slept through "
    "the night, and NOT_FIRED only if a source explicitly reports that it did not. "
    "If the sources say nothing about any cat, answer UNKNOWN."
)


def _fund(address, amount, tries=5):
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
def deployed():
    accounts = get_accounts()
    underwriter, buyer = accounts[0], accounts[1]
    _fund(underwriter.address, 60 * GEN)
    _fund(buyer.address, 20 * GEN)

    factory = get_contract_factory("NoClaim")
    if CONTRACT:
        contract = factory.build_contract(contract_address=CONTRACT, account=underwriter)
    else:
        contract = factory.deploy(args=[], account=underwriter)
    print(f"\nNoClaim at {contract.address}")

    assert tx_execution_succeeded(
        contract.connect(underwriter).fund_pool().transact(value=40 * GEN)
    )
    pool = contract.get_pool(args=[]).call()
    print(f"pool funded: {int(pool['free']) / GEN:.1f} GEN free")
    return contract, underwriter, buyer


def test_pool_refuses_cover_it_cannot_back(deployed):
    """Before anything else: the contract will not sell what it cannot pay."""
    contract, _, buyer = deployed
    pool = contract.get_pool(args=[]).call()
    too_big = int(pool["free"]) + GEN

    quote = contract.quote(args=[too_big]).call()
    assert quote["can_write"] == 0, "quoted cover the pool cannot back"
    print(f"\nquote for {too_big / GEN:.1f} GEN: can_write={quote['can_write']} - refused")


def test_cover_is_written_and_settles_not_fired(deployed):
    """A policy on an event that did not happen: the pool earns the premium."""
    contract, _, buyer = deployed
    payout = 5 * GEN
    quote = contract.quote(args=[payout]).call()
    premium = max(int(quote["min_premium"]), GEN // 10)

    expires = int(time.time()) + MIN_COVER_SECONDS + 30
    assert tx_execution_succeeded(
        contract.connect(buyer).buy_policy(
            args=[TRIGGER, CRITERIA, SOURCES, payout, expires]
        ).transact(value=premium)
    )

    policies = json.loads(contract.get_all_policies(args=[]).call() or "{}")
    pid = max(int(k) for k in policies)
    before = contract.get_pool(args=[]).call()
    assert int(before["reserved"]) >= payout, "the payout was not reserved"
    print(f"\npolicy {pid}: {payout / GEN:.1f} GEN cover for {premium / GEN:.2f} premium, "
          f"{int(before['reserved']) / GEN:.1f} reserved")

    wait = expires - int(time.time()) + 5
    if wait > 0:
        print(f"waiting {wait}s for the policy to expire...")
        time.sleep(wait)

    print("settling - validators fetch the evidence and adjudicate...")
    receipt = contract.connect(buyer).settle_policy(args=[pid]).transact()
    assert tx_execution_succeeded(receipt), "settlement did not reach consensus"

    p = contract.get_policy(args=[pid]).call()
    print(f"  {p['outcome']} / {p['settlement']}")
    print(f"  {p['reasoning']}")

    assert p["outcome"] == "NOT_FIRED", (
        f"expected NOT_FIRED for a trigger that plainly did not happen, "
        f"got {p['outcome']!r}: {p['reasoning']!r}"
    )
    assert p["settlement"] == "EXPIRED"

    after = contract.get_pool(args=[]).call()
    assert int(after["reserved"]) == int(before["reserved"]) - payout, "reserve not released"
    assert int(after["premiums_earned"]) >= premium, "premium was not earned"
    assert contract.get_balance(args=[buyer.address]).call() == "0", \
        "nothing should be owed to the buyer on a policy that did not fire"


@pytest.mark.slow
def test_unknowable_trigger_refunds_the_premium(deployed):
    """The design decision, on a real network.

    Nothing in these sources can establish whether the event happened. Real
    insurance would deny the claim and keep the premium. This refunds it.
    """
    contract, _, buyer = deployed
    payout = 2 * GEN
    premium = max(int(contract.quote(args=[payout]).call()["min_premium"]), GEN // 20)

    expires = int(time.time()) + MIN_COVER_SECONDS + 30
    assert tx_execution_succeeded(
        contract.connect(buyer).buy_policy(
            args=[UNKNOWABLE, UNKNOWABLE_CRITERIA, SOURCES, payout, expires]
        ).transact(value=premium)
    )
    policies = json.loads(contract.get_all_policies(args=[]).call() or "{}")
    pid = max(int(k) for k in policies)

    owed_before = int(contract.get_balance(args=[buyer.address]).call())
    earned_before = int(contract.get_pool(args=[]).call()["premiums_earned"])

    wait = expires - int(time.time()) + 5
    if wait > 0:
        print(f"\nwaiting {wait}s for the policy to expire...")
        time.sleep(wait)

    print("settling an unknowable trigger...")
    assert tx_execution_succeeded(
        contract.connect(buyer).settle_policy(args=[pid]).transact()
    )

    p = contract.get_policy(args=[pid]).call()
    print(f"  {p['outcome']} / {p['settlement']}")
    print(f"  {p['reasoning']}")

    assert p["outcome"] == "UNKNOWN", (
        f"validators claimed to settle something the evidence cannot settle: "
        f"{p['outcome']!r} - {p['reasoning']!r}"
    )
    assert p["settlement"] == "REFUNDED"

    owed_after = int(contract.get_balance(args=[buyer.address]).call())
    assert owed_after - owed_before == premium, "the premium was not refunded in full"
    assert int(contract.get_pool(args=[]).call()["premiums_earned"]) == earned_before, \
        "an unadjudicated risk must earn the pool nothing"
    print(f"  premium of {premium / GEN:.2f} GEN returned in full")


def test_pool_is_solvent_on_chain(deployed):
    """What the ledger says it holds, against what the chain says it holds."""
    import requests

    contract, _, _ = deployed
    pool = contract.get_pool(args=[]).call()
    owed = int(pool["total"]) + int(pool["owed_to_people"])

    held = None
    for _ in range(6):
        try:
            body = requests.post(
                "https://studio.genlayer.com/api",
                json={"jsonrpc": "2.0", "method": "eth_getBalance",
                      "params": [contract.address, "latest"], "id": 1},
                timeout=60,
            ).json()
            held = int(body["result"], 16)
            break
        except Exception:  # noqa: BLE001
            time.sleep(4)
    assert held is not None, "could not read the contract balance"

    print(f"\nholds {held / GEN:.3f} GEN, pool + owed {owed / GEN:.3f}")
    assert int(pool["reserved"]) <= int(pool["total"]), \
        "reserved more cover than the pool holds"
    assert held >= owed, f"INSOLVENT: holds {held}, owes {owed}"
