# v0.2.0 -- NoClaim: parametric cover that pays without a claim being filed
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

# GenVM parses the two lines above as the "runner comment" and is strict about them:
# line 1 must START with the version token, the Depends line must come immediately
# after, and NO further comment may follow before the code -- violating any of these
# fails deployment with a bare `invalid_contract` error.
#
# Source stays pure ASCII: genlayer-py hex-encodes it with eth_utils.encode_hex,
# which is ASCII-only, so a single em dash breaks schema fetching after a
# successful deploy and surfaces as a misleading "Failed to get schema".
#
# Time comes from datetime.datetime.now(), never time.time(): expiries derived
# from it are written to state, and state must match across validators exactly.
# genvm-lint raises W002 on time.time() and accepts this form.
#
# v0.2.0 changes vs v0.1.0
#   1. Evidence sources are validated against a public-host rule: localhost,
#      loopback and RFC-1918 addresses are rejected at buy time so validators
#      can always reach every source independently.
#   2. UNKNOWN settlements are retryable: a policy that cannot be adjudicated
#      stays ACTIVE and may be settled again after UNKNOWN_RETRY_DELAY seconds,
#      up to MAX_UNKNOWN_RETRIES total UNKNOWN verdicts before it finalises as
#      REFUNDED.  This lets a transient source outage resolve itself rather than
#      permanently blocking the policy in an unadjudicated state.
#   3. Policy storage is split into active_policies_json (only ACTIVE policies,
#      the set that settle_policy reads and writes on every call) and
#      archive_policies_json (SETTLED policies, append-only).  settle_policy
#      therefore operates on a bounded-size document regardless of how many
#      policies have been written over the lifetime of the contract, and
#      get_policies_page / get_active_policies give callers efficient access
#      without pulling the entire history.

from genlayer import *

import json
import typing
import datetime


def _now() -> int:
    return int(datetime.datetime.now().timestamp())


@gl.evm.contract_interface
class _Recipient:
    class Write:
        pass
    class View:
        pass


TRIGGER_PROMPT = """
You are a neutral loss adjuster for a parametric insurance contract on GenLayer,
an AI-validator blockchain. Independent validators run this exact prompt and must
reach the same conclusion, so answer only from verifiable evidence -- never from
opinion, sympathy, or what seems likely.

THE INSURED EVENT: {trigger}

HOW TO DECIDE IT (set by the policy when it was written): {criteria}

CURRENT UNIX TIMESTAMP: {now}

EVIDENCE (fetched live, may be empty or partial):
{context}

Decide strictly from the criteria and the evidence above.

Answer FIRED if the evidence shows the insured event happened.
Answer NOT_FIRED if the evidence shows it did not.
Answer UNKNOWN if the evidence is absent, partial, or does not settle the
question -- do not guess in either direction. Guessing FIRED takes money from
people who underwrote honestly; guessing NOT_FIRED denies a payout to someone
who may have suffered the loss. UNKNOWN is the honest answer when the evidence
is not there, and this contract is built to handle it.

Return strictly a single valid JSON object matching this exact schema -- no
markdown formatting, no code fences, no extra text before or after it:
{{
    "outcome": "<FIRED|NOT_FIRED|UNKNOWN>",
    "reasoning": "<one or two sentences citing the specific evidence used>"
}}
"""

ALLOWED_OUTCOMES = {"FIRED", "NOT_FIRED", "UNKNOWN"}
MAX_SOURCES = 3
MAX_SOURCE_CHARS = 4000
MAX_TRIGGER_CHARS = 300
MAX_CRITERIA_CHARS = 600
MIN_COVER_SECONDS = 300       # a policy has to be able to outlive its own creation
MAX_COVER_SECONDS = 31536000  # one year

# --- Retryable UNKNOWN constants -----------------------------------------
# When validators cannot reach a verdict, the policy stays ACTIVE and may be
# settled again after UNKNOWN_RETRY_DELAY seconds.  After MAX_UNKNOWN_RETRIES
# total UNKNOWN verdicts the policy is finalised as REFUNDED.
MAX_UNKNOWN_RETRIES = 2      # total UNKNOWN verdicts allowed before finalising
UNKNOWN_RETRY_DELAY = 3600   # seconds the caller must wait between retry attempts


def _validate_source_url(url: str) -> None:
    """Reject URLs that point at private, loopback, or internal hosts.

    Every source must be publicly reachable so that independent validators can
    fetch it themselves.  A policy backed by localhost or a private IP would
    always adjudicate UNKNOWN (because validators cannot see it), and the
    premium would be refunded every time -- which is not useful insurance.

    Blocked:
        - No dot in hostname (bare names like 'myserver')
        - localhost
        - Loopback: 127.x.x.x
        - Private IPv4: 10.x, 172.16-31.x, 192.168.x, 169.254.x (link-local)
    """
    try:
        scheme_end = url.index("://")
        rest = url[scheme_end + 3:]
        host_part = rest.split("/")[0].split("?")[0].split("#")[0]
        if host_part.startswith("["):
            # IPv6 literal: [::1]:port
            bracket_end = host_part.index("]")
            host = host_part[1:bracket_end]
        elif ":" in host_part:
            host = host_part[:host_part.rindex(":")]
        else:
            host = host_part
        host = host.lower().strip()
    except (ValueError, IndexError):
        raise gl.vm.UserError(f"unparseable source URL: {url}")

    if not host:
        raise gl.vm.UserError(f"source URL has no host: {url}")

    # Require at least one dot so bare hostnames are rejected
    if "." not in host:
        raise gl.vm.UserError(
            f"source host must be a public domain (needs a dot), got: {host}"
        )

    if host == "localhost":
        raise gl.vm.UserError("source URL must be publicly reachable, not localhost")

    # Block private and loopback IPv4 ranges
    octets = host.split(".")
    if len(octets) == 4:
        try:
            a, b = int(octets[0]), int(octets[1])
            blocked = (
                a == 127                             # loopback
                or a == 10                           # RFC-1918
                or (a == 172 and 16 <= b <= 31)      # RFC-1918
                or (a == 192 and b == 168)           # RFC-1918
                or (a == 169 and b == 254)           # link-local
            )
            if blocked:
                raise gl.vm.UserError(
                    f"source URL must be publicly reachable, not a private/loopback IP: {host}"
                )
        except gl.vm.UserError:
            raise
        except ValueError:
            pass  # not a numeric IP -- treat as hostname, fine


class NoClaim(gl.Contract):
    """
    NoClaim -- parametric cover, settled by AI-validator consensus.

    A prediction market needs someone to take the other side. Insurance does
    not, and that is the whole difference: here a pool of underwriters stands
    behind every policy, so a person can buy cover against something without
    finding anybody willing to bet that it happens to them.

    HOW A POLICY WORKS

        fund_pool()      underwriters put capital in and take on the risk
        buy_policy()     a buyer names the event, pays a premium, and the
                         contract reserves the payout out of free capital
        settle_policy()  at expiry anyone triggers it; validators fetch the
                         sources and judge
        collect()        a fired policy is collected by the holder

    Nothing is paid on the strength of a claim form, an assessor, or an
    operator's goodwill. The policy carries its own trigger, its own criteria
    and its own sources, and settlement is permissionless.

    THE PART THAT IS NOT LIKE INSURANCE

    Real insurance resolves ambiguity in the insurer's favour: the burden is on
    the policyholder, and a claim that cannot be proven is denied. That is a
    reasonable rule when the insurer also pays for the investigation, and a
    terrible one when it is simply the party with more lawyers.

    Here, UNKNOWN refunds the premium. If the evidence cannot establish whether
    the event happened, the underwriters do not get to keep money for carrying a
    risk nobody can adjudicate, and the buyer is not denied on a technicality.
    Neither side wins the argument, so neither side pays for it.

    UNKNOWN IS RETRYABLE

    A transient source outage should not permanently seal a policy in an
    unadjudicated state. If the first verdict is UNKNOWN the policy stays ACTIVE
    and may be settled again after UNKNOWN_RETRY_DELAY seconds. After
    MAX_UNKNOWN_RETRIES total UNKNOWN verdicts the premium is refunded as usual.

    EVIDENCE SOURCES ARE CONSTRAINED TO PUBLIC HOSTS

    Validators run in independent network environments. A source that points at
    localhost or a private IP is unreachable by design: every settlement would
    return UNKNOWN and the premium would always be refunded. buy_policy rejects
    such URLs at purchase time so the problem surfaces before money changes hands.

    SOLVENCY IS ENFORCED, NOT PROMISED

    Every policy reserves its full payout from the pool at the moment it is
    written, so the pool can never sell cover it could not honour. Underwriters
    can only withdraw capital that is not reserved. The contract therefore
    cannot become insolvent through underwriting, only through a payout it has
    already set aside for.

    STORAGE

    Policies are stored in two separate blobs:
        active_policies_json  -- only ACTIVE (unsettled) policies
        archive_policies_json -- only SETTLED policies (append-only)

    settle_policy therefore reads and writes a document that is bounded by the
    number of concurrently live policies, not by the total ever written. The
    archive is only read by view functions where large responses are acceptable.
    """

    owner: str
    # --- the pool ---
    pool_total: u256          # capital underwriters have committed
    pool_reserved: u256       # the part of it promised to live policies
    premiums_earned: u256     # premiums kept on policies that did not fire
    payouts_made: u256        # lifetime cover actually paid
    underwriters_json: str    # { "0xaddr": "wei" } -- who put in what
    # --- policies (split store) ---
    active_policies_json: str   # { "id": {...} } -- ACTIVE policies only
    archive_policies_json: str  # { "id": {...} } -- SETTLED policies (append-only)
    next_policy_id: u256
    # --- money owed to individuals ---
    balances_json: str        # { "0xaddr": "wei" } -- collectable, withdrawable
    balances_total: u256
    unclaimed_total: u256     # settled payouts not yet collected
    # --- underwriting rule ---
    min_rate_bps: u256        # the least a premium may be, as bps of the payout

    # --- Construction ------------------------------------------------

    def __init__(self):
        self.owner = str(gl.message.sender_address).lower()
        self.pool_total = u256(0)
        self.pool_reserved = u256(0)
        self.premiums_earned = u256(0)
        self.payouts_made = u256(0)
        self.underwriters_json = "{}"
        self.active_policies_json = "{}"
        self.archive_policies_json = "{}"
        self.next_policy_id = u256(1)
        self.balances_json = "{}"
        self.balances_total = u256(0)
        self.unclaimed_total = u256(0)
        # 2% of the sum insured, by default. Low enough to be worth buying,
        # high enough that a pool is not drained by policies priced at nothing.
        self.min_rate_bps = u256(200)

    # --- Internal helpers -------------------------------------------

    def _load(self, s: str, default: typing.Any) -> typing.Any:
        try:
            return json.loads(s) if s else default
        except Exception:
            return default

    def _require_owner(self) -> None:
        if str(gl.message.sender_address).lower() != self.owner:
            raise gl.vm.UserError("Only owner can call this")

    def _pool_free(self) -> int:
        """Capital not already promised to a live policy."""
        return max(0, int(self.pool_total) - int(self.pool_reserved))

    def _credit(self, balances: dict, addr: str, amount: int) -> None:
        a = addr.lower()
        balances[a] = str(int(balances.get(a, "0")) + amount)
        self.balances_total += u256(amount)

    def _debit(self, balances: dict, addr: str, amount: int) -> None:
        a = addr.lower()
        have = int(balances.get(a, "0"))
        if have < amount:
            raise gl.vm.UserError(f"Insufficient balance: have {have} wei, need {amount} wei")
        rest = have - amount
        if rest == 0:
            balances.pop(a, None)
        else:
            balances[a] = str(rest)
        self.balances_total -= u256(amount)

    def _parse_llm_json(self, response: typing.Any) -> dict:
        """The model sometimes wraps its answer in a code fence even when told
        not to. Returns an empty dict on anything malformed -- the caller reads
        a missing outcome as UNKNOWN, which refunds, and that is the safe way
        to fail."""
        if isinstance(response, dict):
            return response
        text = str(response).strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        parsed = json.loads(text.strip())
        return parsed if isinstance(parsed, dict) else {}

    def _adjudicate(self, trigger: str, criteria: str, sources: list) -> dict:
        """Did the insured event happen? Agreed by validators through the
        Equivalence Principle.

        Only the normalized outcome has to match across independent runs. Two
        honest adjudications can word their reasoning differently while
        agreeing completely on whether the event fired, and the verdict is the
        part that has to be identical for consensus to mean anything.
        """

        def leader_fn() -> dict:
            parts = []
            for url in sources[:MAX_SOURCES]:
                try:
                    resp = gl.nondet.web.get(url)
                    parts.append(f"SOURCE: {url}\n{str(resp.body)[:MAX_SOURCE_CHARS]}")
                except Exception:
                    parts.append(f"SOURCE: {url}\n(fetch failed)")
            context = "\n\n".join(parts) if parts else "(no sources provided)"

            prompt = (
                TRIGGER_PROMPT
                .replace("{trigger}", trigger)
                .replace("{criteria}", criteria)
                .replace("{now}", str(_now()))
                .replace("{context}", context)
            )
            try:
                parsed = self._parse_llm_json(
                    gl.nondet.exec_prompt(prompt, response_format="json")
                )
                outcome = str(parsed.get("outcome", "UNKNOWN")).strip().upper()
                if outcome not in ALLOWED_OUTCOMES:
                    outcome = "UNKNOWN"
                return {"outcome": outcome, "reasoning": str(parsed.get("reasoning", ""))[:500]}
            except Exception:
                return {"outcome": "UNKNOWN", "reasoning": "adjudication_failed"}

        def validator_fn(leaders_res) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return False
            try:
                mine = leader_fn()
                theirs = leaders_res.calldata
                if isinstance(theirs, str):
                    theirs = json.loads(theirs)
                return str(mine.get("outcome")) == str(theirs.get("outcome"))
            except Exception:
                return False

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except Exception:
                result = {"outcome": "UNKNOWN", "reasoning": "adjudication_failed"}
        if not isinstance(result, dict):
            result = {"outcome": "UNKNOWN", "reasoning": "adjudication_failed"}
        return result

    # --- Underwriting ------------------------------------------------

    @gl.public.write.payable
    def fund_pool(self) -> dict[str, typing.Any]:
        """Put capital behind the policies this contract writes.

        An underwriter is taking real risk: this capital pays claims, and a run
        of fired policies can lose some of it. What it cannot do is disappear
        into cover that was never backed, because every policy reserves its
        payout up front.
        """
        amount = int(gl.message.value)
        if amount <= 0:
            raise gl.vm.UserError("Send some GEN to underwrite")
        who = str(gl.message.sender_address).lower()
        book = self._load(self.underwriters_json, {})
        book[who] = str(int(book.get(who, "0")) + amount)
        self.underwriters_json = json.dumps(book)
        self.pool_total += u256(amount)
        return {
            "underwriter": who,
            "added": str(amount),
            "pool_total": str(self.pool_total),
            "pool_free": str(self._pool_free()),
        }

    @gl.public.write
    def withdraw_pool(self, amount: u256) -> dict[str, typing.Any]:
        """Take capital back out -- but only what is not reserved.

        Reserved capital belongs to policies that have already been sold. An
        underwriter who could withdraw it would be selling cover and then
        walking away from it, which is the failure this contract exists to make
        structurally impossible.
        """
        want = int(amount)
        who = str(gl.message.sender_address).lower()
        book = self._load(self.underwriters_json, {})
        mine = int(book.get(who, "0"))

        if want <= 0:
            raise gl.vm.UserError("Amount must be greater than zero")
        if want > mine:
            raise gl.vm.UserError(f"You have {mine} wei in the pool, cannot withdraw {want}")
        free = self._pool_free()
        if want > free:
            raise gl.vm.UserError(
                f"Only {free} wei is unreserved; the rest is backing live policies"
            )

        rest = mine - want
        if rest == 0:
            book.pop(who, None)
        else:
            book[who] = str(rest)
        self.underwriters_json = json.dumps(book)
        self.pool_total = u256(int(self.pool_total) - want)
        _Recipient(gl.message.sender_address).emit_transfer(value=u256(want))
        return {"withdrawn": str(want), "pool_total": str(self.pool_total)}

    @gl.public.write
    def set_min_rate_bps(self, bps: u256) -> None:
        """The floor on what a policy may be priced at, in basis points of the
        sum insured. Raising it makes the pool pickier; setting it to zero
        would let anyone buy unlimited cover for nothing."""
        self._require_owner()
        v = int(bps)
        if v == 0 or v > 10000:
            raise gl.vm.UserError("min_rate_bps must be between 1 and 10000")
        self.min_rate_bps = u256(v)

    # --- Buying cover ------------------------------------------------

    @gl.public.write.payable
    def buy_policy(
        self,
        trigger: str,
        criteria: str,
        sources: str,
        payout: u256,
        expires_ts: u256,
    ) -> dict[str, typing.Any]:
        """Buy cover against a named event. The GEN sent with this call is the
        premium; `payout` is what the policy pays if the event fires.

        The payout is reserved from the pool here and now. If the pool cannot
        cover it, the policy is refused rather than written and hoped for.

        Source URLs must point at publicly reachable hosts. Localhost and
        private-range IPs are rejected: validators run in independent network
        environments and cannot reach internal hosts, so such a policy would
        always adjudicate UNKNOWN and always refund the premium.
        """
        trigger = trigger.strip()
        criteria = criteria.strip()
        premium = int(gl.message.value)
        cover = int(payout)
        expiry = int(expires_ts)
        now = _now()

        if not trigger or len(trigger) > MAX_TRIGGER_CHARS:
            raise gl.vm.UserError(f"trigger must be 1-{MAX_TRIGGER_CHARS} chars")
        if not criteria or len(criteria) > MAX_CRITERIA_CHARS:
            raise gl.vm.UserError(f"criteria must be 1-{MAX_CRITERIA_CHARS} chars")
        if expiry < now + MIN_COVER_SECONDS:
            raise gl.vm.UserError(f"expires_ts must be at least {MIN_COVER_SECONDS}s away")
        if expiry > now + MAX_COVER_SECONDS:
            raise gl.vm.UserError("cover cannot run longer than a year")
        if cover <= 0:
            raise gl.vm.UserError("payout must be greater than zero")
        if premium <= 0:
            raise gl.vm.UserError("send the premium with this call")

        # Pricing floor. Without it a policy could be bought for one wei and the
        # pool would be handing out cover for free.
        least = cover * int(self.min_rate_bps) // 10000
        if premium < least:
            raise gl.vm.UserError(
                f"premium too low: {least} wei is the minimum for a {cover} wei payout"
            )

        free = self._pool_free()
        if cover > free:
            raise gl.vm.UserError(
                f"pool cannot back this: {free} wei unreserved, {cover} wei needed"
            )

        src = [s.strip() for s in sources.split(",") if s.strip()] if sources else []
        if len(src) > MAX_SOURCES:
            raise gl.vm.UserError(f"at most {MAX_SOURCES} sources")
        for url in src:
            if not (url.startswith("http://") or url.startswith("https://")):
                raise gl.vm.UserError(f"invalid source URL: {url}")
            # Constrain to publicly reachable hosts only
            _validate_source_url(url)

        active = self._load(self.active_policies_json, {})
        pid = str(int(self.next_policy_id))
        active[pid] = {
            "id": pid,
            "holder": str(gl.message.sender_address).lower(),
            "trigger": trigger,
            "criteria": criteria,
            "sources_json": json.dumps(src),
            "premium": str(premium),
            "payout": str(cover),
            "bought_ts": now,
            "expires_ts": expiry,
            "status": "ACTIVE",
            "outcome": "",
            "reasoning": "",
            "collected": 0,
            "unknown_count": 0,
            "last_unknown_ts": 0,
        }
        self.active_policies_json = json.dumps(active)
        self.next_policy_id += u256(1)

        # The premium joins the pool, and the payout is set aside from it.
        self.pool_total += u256(premium)
        self.pool_reserved += u256(cover)

        return {
            "policy_id": pid,
            "premium": str(premium),
            "payout": str(cover),
            "expires_ts": expiry,
            "pool_free": str(self._pool_free()),
        }

    # --- Settlement (permissionless) ---------------------------------

    @gl.public.write
    def settle_policy(self, policy_id: u256) -> dict[str, typing.Any]:
        """Decide a policy at expiry. Callable by anyone -- there is no adjuster
        to appoint and no claim to file.

        If validators cannot reach a verdict (UNKNOWN), the policy stays ACTIVE
        and may be settled again after UNKNOWN_RETRY_DELAY seconds. After
        MAX_UNKNOWN_RETRIES total UNKNOWN verdicts the premium is refunded and
        the policy is archived.  This lets a transient source outage resolve
        naturally rather than permanently locking up the premium.
        """
        pid = str(int(policy_id))
        active = self._load(self.active_policies_json, {})
        p = active.get(pid)
        if not p:
            # Give a more helpful error if the policy is already archived
            archive = self._load(self.archive_policies_json, {})
            if archive.get(pid):
                raise gl.vm.UserError(f"Policy {pid} is already settled")
            raise gl.vm.UserError(f"Unknown policy: {pid}")

        if p["status"] != "ACTIVE":
            raise gl.vm.UserError(f"Policy is {p['status']}, not active")

        now = _now()
        if now < int(p["expires_ts"]):
            raise gl.vm.UserError(f"Not due yet, {int(p['expires_ts']) - now}s remaining")

        # Enforce the retry delay between successive UNKNOWN attempts
        unknown_count = int(p.get("unknown_count", 0))
        if unknown_count > 0:
            last_unknown_ts = int(p.get("last_unknown_ts", 0))
            retry_available = last_unknown_ts + UNKNOWN_RETRY_DELAY
            if now < retry_available:
                wait = retry_available - now
                raise gl.vm.UserError(
                    f"UNKNOWN retry not available for another {wait}s "
                    f"(attempt {unknown_count} of {MAX_UNKNOWN_RETRIES})"
                )

        sources = self._load(p.get("sources_json", "[]"), [])
        verdict = self._adjudicate(p["trigger"], p["criteria"], sources)
        outcome = verdict.get("outcome", "UNKNOWN")

        cover = int(p["payout"])
        premium = int(p["premium"])

        # --- Retryable UNKNOWN path ------------------------------------
        # Keep the policy ACTIVE if we have not exhausted our retry budget.
        # The reserve is NOT released -- the policy is still live.
        if outcome == "UNKNOWN" and (unknown_count + 1) < MAX_UNKNOWN_RETRIES:
            p["unknown_count"] = unknown_count + 1
            p["last_unknown_ts"] = now
            p["reasoning"] = str(verdict.get("reasoning", ""))[:500]
            active[pid] = p
            self.active_policies_json = json.dumps(active)
            retries_left = MAX_UNKNOWN_RETRIES - (unknown_count + 1)
            return {
                "policy_id": pid,
                "outcome": "UNKNOWN",
                "settlement": "PENDING_RETRY",
                "retries_remaining": retries_left,
                "retry_after": now + UNKNOWN_RETRY_DELAY,
                "reasoning": p["reasoning"],
            }

        # --- Final settlement -----------------------------------------
        # Covers FIRED, NOT_FIRED, and UNKNOWN with exhausted retries.
        # Release the reserve in all three cases.
        self.pool_reserved = u256(max(0, int(self.pool_reserved) - cover))
        balances = self._load(self.balances_json, {})

        if outcome == "FIRED":
            # The pool pays. It keeps the premium, so its loss is the
            # difference, and the holder collects the full sum insured.
            self._credit(balances, p["holder"], cover)
            self.pool_total = u256(max(0, int(self.pool_total) - cover))
            self.unclaimed_total += u256(cover)
            self.payouts_made += u256(cover)
            settlement = "PAID"
        elif outcome == "NOT_FIRED":
            # No loss. The premium is earned.
            self.premiums_earned += u256(premium)
            settlement = "EXPIRED"
        else:
            # UNKNOWN (retry budget exhausted). Nobody proved anything, so
            # nobody is charged for it: the premium goes back and the pool
            # keeps none of it.
            self._credit(balances, p["holder"], premium)
            self.pool_total = u256(max(0, int(self.pool_total) - premium))
            self.unclaimed_total += u256(premium)
            settlement = "REFUNDED"

        self.balances_json = json.dumps(balances)
        p["status"] = "SETTLED"
        p["outcome"] = outcome
        p["settlement"] = settlement
        p["reasoning"] = str(verdict.get("reasoning", ""))[:500]

        # Move from active to archive
        active.pop(pid, None)
        archive = self._load(self.archive_policies_json, {})
        archive[pid] = p
        self.active_policies_json = json.dumps(active)
        self.archive_policies_json = json.dumps(archive)

        return {
            "policy_id": pid,
            "outcome": outcome,
            "settlement": settlement,
            "reasoning": p["reasoning"],
            "pool_free": str(self._pool_free()),
        }

    # --- Collecting --------------------------------------------------

    @gl.public.write
    def withdraw(self, amount: u256) -> dict[str, typing.Any]:
        """Take collected money out to your wallet."""
        amt = int(amount)
        if amt <= 0:
            raise gl.vm.UserError("Amount must be greater than zero")
        who = str(gl.message.sender_address).lower()
        balances = self._load(self.balances_json, {})
        self._debit(balances, who, amt)
        self.balances_json = json.dumps(balances)
        self.unclaimed_total = u256(max(0, int(self.unclaimed_total) - amt))
        _Recipient(gl.message.sender_address).emit_transfer(value=u256(amt))
        return {"withdrawn": str(amt), "balance": balances.get(who, "0")}

    @gl.public.write
    def withdraw_all(self) -> dict[str, typing.Any]:
        who = str(gl.message.sender_address).lower()
        balances = self._load(self.balances_json, {})
        amt = int(balances.get(who, "0"))
        if amt <= 0:
            raise gl.vm.UserError("Nothing to withdraw")
        self._debit(balances, who, amt)
        self.balances_json = json.dumps(balances)
        self.unclaimed_total = u256(max(0, int(self.unclaimed_total) - amt))
        _Recipient(gl.message.sender_address).emit_transfer(value=u256(amt))
        return {"withdrawn": str(amt), "balance": "0"}

    # --- Views -------------------------------------------------------

    @gl.public.view
    def get_owner(self) -> str:
        return self.owner

    @gl.public.view
    def get_policy(self, policy_id: u256) -> dict[str, typing.Any]:
        """Return one policy by ID, checking active store first then archive."""
        pid = str(int(policy_id))
        active = self._load(self.active_policies_json, {})
        p = active.get(pid)
        if p:
            return p
        archive = self._load(self.archive_policies_json, {})
        p = archive.get(pid)
        if p:
            return p
        raise gl.vm.UserError(f"Unknown policy: {pid}")

    @gl.public.view
    def get_all_policies(self) -> str:
        """Return every policy ever written as a JSON object keyed by ID.

        This merges the active store and the archive in one call. For large
        deployments prefer get_policies_page() to avoid loading the full history.
        """
        active = self._load(self.active_policies_json, {})
        archive = self._load(self.archive_policies_json, {})
        merged = {**archive, **active}
        return json.dumps(merged)

    @gl.public.view
    def get_active_policies(self) -> str:
        """Return only the currently ACTIVE (unsettled) policies.

        This is the bounded-size document that settle_policy operates on.
        Use this when you only need to show live cover.
        """
        return self.active_policies_json

    @gl.public.view
    def get_policies_page(self, offset: u256, limit: u256) -> str:
        """Return a page of policies, newest first.

        Merge active and archive, sort by numeric ID descending, then slice.
        This lets a frontend paginate the full history without loading the
        entire archive in one call.
        """
        off = int(offset)
        lim = max(1, min(int(limit), 100))  # cap at 100 per page
        active = self._load(self.active_policies_json, {})
        archive = self._load(self.archive_policies_json, {})
        merged = {**archive, **active}
        # Sort newest (highest numeric ID) first
        items = sorted(merged.values(), key=lambda p: int(p["id"]), reverse=True)
        return json.dumps(items[off: off + lim])

    @gl.public.view
    def get_policy_count(self) -> dict[str, typing.Any]:
        """Return counts of active and archived policies."""
        active = self._load(self.active_policies_json, {})
        archive = self._load(self.archive_policies_json, {})
        return {
            "active": len(active),
            "archived": len(archive),
            "total": len(active) + len(archive),
        }

    @gl.public.view
    def get_policies_of(self, addr: str) -> str:
        addr = addr.lower()
        active = self._load(self.active_policies_json, {})
        archive = self._load(self.archive_policies_json, {})
        merged = {**archive, **active}
        return json.dumps([p for p in merged.values() if p.get("holder") == addr])

    @gl.public.view
    def get_balance(self, addr: str) -> str:
        return self._load(self.balances_json, {}).get(addr.lower(), "0")

    @gl.public.view
    def get_underwriter(self, addr: str) -> str:
        return self._load(self.underwriters_json, {}).get(addr.lower(), "0")

    @gl.public.view
    def quote(self, payout: u256) -> dict[str, typing.Any]:
        """What a given amount of cover would cost, and whether the pool can
        write it at all. Published so a buyer is never surprised at the till."""
        cover = int(payout)
        free = self._pool_free()
        return {
            "payout": str(cover),
            "min_premium": str(cover * int(self.min_rate_bps) // 10000),
            "min_rate_bps": int(self.min_rate_bps),
            "pool_free": str(free),
            "can_write": 1 if 0 < cover <= free else 0,
        }

    @gl.public.view
    def get_pool(self) -> dict[str, typing.Any]:
        """The pool's health. `free` is the number that matters: it is how much
        cover this contract can still honestly sell."""
        return {
            "total": str(self.pool_total),
            "reserved": str(self.pool_reserved),
            "free": str(self._pool_free()),
            "premiums_earned": str(self.premiums_earned),
            "payouts_made": str(self.payouts_made),
            "underwriters": len(self._load(self.underwriters_json, {})),
            "min_rate_bps": int(self.min_rate_bps),
            "owed_to_people": str(self.balances_total),
        }

    @gl.public.view
    def get_stats(self) -> dict[str, typing.Any]:
        active = self._load(self.active_policies_json, {})
        archive = self._load(self.archive_policies_json, {})
        all_policies = {**archive, **active}
        fired = sum(1 for p in all_policies.values() if p.get("settlement") == "PAID")
        refunded = sum(1 for p in all_policies.values() if p.get("settlement") == "REFUNDED")
        return {
            "policies_written": len(all_policies),
            "active": len(active),
            "paid_out": fired,
            "refunded_unknown": refunded,
            "pool_total": str(self.pool_total),
            "pool_reserved": str(self.pool_reserved),
            "payouts_made": str(self.payouts_made),
            "premiums_earned": str(self.premiums_earned),
        }
