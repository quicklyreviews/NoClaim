# NoClaim — cover that pays without a claim being filed

**Track:** Prediction Markets & Real-World Settlement — Agent Tank hackathon
(`https://portal.genlayer.foundation/agent-tank/hackathon`)

**Demo:** <https://ntclick.github.io/noclaim/> — the landing page.
The cover desk, where you buy, settle and collect, is at
[`/noclaim.html`](https://ntclick.github.io/noclaim/noclaim.html).

**Contract:** [`0xBF326FA29B839cF95d3c9d0895b7A852031C3822`](https://explorer-studio.genlayer.com/address/0xBF326FA29B839cF95d3c9d0895b7A852031C3822)
on GenLayer StudioNet, chain 61999 — every policy ever written, and the
reasoning the validators agreed on, is readable there without trusting this
repository, or the link above, at all.

## Try it in four steps

1. **Connect a wallet** on the cover desk. StudioNet is added for you; the bar
   at the top names the chain, the contract and your balance, and turns amber
   if the wallet is anywhere else.
2. **Take test GEN** from the checklist. It is a test network and the GEN is
   free.
3. **Buy cover.** Click the Rainfall template — it reads the live forecast and
   sets the threshold just above it — set the window to 6 minutes, and buy.
4. **Settle it** once the countdown ends. Or settle somebody else's: the
   *Every policy* tab usually has expired ones waiting, and anyone may
   adjudicate them. That is the point.

If cover pays out, the amount is credited to you and **Collect** moves it to
your wallet. Watch the balance in the top bar rather than your wallet's own
screen: the transfer is a second transaction that lands about a minute later,
and a few GEN arriving in an account holding millions is easy to miss.

Parametric insurance, settled by validator consensus. Name the event you want
cover against, say how it should be judged and where the evidence lives. At
expiry every validator fetches those sources itself and runs the same prompt —
and if they cannot tell, your premium comes back in full.

## Why insurance and not a market

A prediction market pays you out of the pocket of whoever took the other side.
That works for a question people genuinely disagree about, and fails completely
for the ones worth insuring: nobody wants to be paid for betting that your
warehouse *will* flood. So the risks worth covering are exactly the risks a
market cannot price — put an unloved question into a parimutuel pool and the
honest outcome is a refund, which is having no cover at all, arrived at more
slowly.

Replacing the counterparty with an underwriting pool fixes that. An underwriter
is paid a premium for carrying risk, not for holding an opinion, so cover can
exist for an event nobody wants to argue about.

## The three verdicts

| Outcome | What happens |
| --- | --- |
| `FIRED` | The evidence says the event happened. The full sum insured is credited to the policyholder. |
| `NOT_FIRED` | It did not. The pool keeps the premium — this is what underwriters are paid for. |
| `UNKNOWN` | Absent, partial or ambiguous evidence. **The premium is refunded in full and the pool earns nothing.** |

The third column is the design decision the contract is built around. Real
insurance denies the claim and keeps the premium when the evidence is thin,
which quietly rewards writing cover on things nobody can check. Here an
unadjudicated risk earns the pool nothing, so the incentive runs the other way.

That is not a claim about intent — it is observable on-chain. Policy 2 was
written deliberately on something the sources could not establish:

> The provided evidence consists only of two Bitcoin/USD price API responses
> from CoinGecko and Coinbase. Neither source mentions any cat or whether it
> slept through the night, so the evidence does not settle the insured event.

The validators agreed on `UNKNOWN`, the premium went back, and
`premiums_earned` did not move by a single wei.

## Solvency is structural

- `buy_policy` reserves the **full payout** out of the pool at the moment the
  policy is written. Cover the pool cannot back is refused at the till, not
  discovered at settlement — `quote` tells you before you pay.
- `withdraw_pool` will only release capital that is not reserved against a live
  policy.
- There is no admin key, no discretionary payout, and no point at which the
  contract can owe more than it holds.
  `test_pool_is_solvent_on_chain` asserts exactly that against the real chain
  balance.

## Why this equivalence strategy

`strict_eq` is the obvious choice and it is wrong here. It demands
byte-identical output across validators — fine for a price feed, impossible for
a judgment, since two honest adjudications of the same evidence will word their
reasoning differently while agreeing completely on the verdict.

So the comparison is narrowed to the one field that has to match for consensus
to mean anything:

```python
def validator_fn(leaders_res) -> bool:
    mine   = leader_fn()                 # I fetch the sources myself
    theirs = leaders_res.calldata        # what the leader concluded
    return mine["outcome"] == theirs["outcome"]

result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
```

`reasoning` is left free to vary and is stored alongside the verdict, so every
settlement carries the validators' own account of why.

## Project layout

```
contracts/no_claim.py                        the Intelligent Contract (Python, GenVM)
frontend/index.html + home.js                the landing page - live figures, read-only
frontend/noclaim.html + noclaim.js           the cover desk - buy, hold, settle, underwrite
frontend/cover-templates.js                  parametric cover built from live data
frontend/wallet.js                           wallet, network check, and the request queue
frontend/brand.js                            the mark, as SVG geometry shared by header and favicon
tests/direct/conftest.py                     mock `genlayer` module, no GenVM needed
tests/direct/test_no_claim.py                fast unit tests (12)
tests/integration/test_noclaim_studionet.py  cover written, settled and refunded on a real network
tests/integration/probe_sources.py           which URLs can validators actually reach
```

## Running it

```bash
python -m http.server 5174 --directory frontend
```

Then open <http://localhost:5174>. There is no build step — the import map
pulls `genlayer-js` from a CDN.

`index.html` is the landing page: read-only and wallet-free, because a page
that opens a wallet prompt before saying what the product is has the order
backwards. Its figures and policy cards are read off the chain on load rather
than written into the HTML, so it is either current or honestly blank.

`noclaim.html` is the desk. A sticky bar names the chain, the chain id, the
contract and the connected account, and turns amber the moment the wallet is on
anything else — a page that asks for a signature should never leave you
guessing where the signature goes. "How to take part" is a live checklist whose
ticks come from real state, so it answers *which step am I on* rather than
listing steps.

## Tests

```bash
pytest tests/direct                                              # 12, seconds
gltest tests/integration/test_noclaim_studionet.py --network studionet -v -s
```

The direct tests prove the accounting against a mocked `genlayer` module. The
integration test proves the part that cannot be mocked: that independent
validators fetch the evidence themselves, adjudicate, and agree. Budget about
ten minutes — cover cannot be written for less than five, and every transaction
waits for consensus.

## Things that bit, written down

**StudioNet rate-limits, and disguises it as CORS.** Reads get 300 a minute,
transactions far fewer. When it refuses it answers **429 without CORS headers**,
so the browser cannot read the response and reports a CORS policy block. CORS
is configured correctly — the server reflects the origin on every successful
call. The give-away is in those same headers: `X-RateLimit-Remaining`,
`Retry-After`. Naive retrying made it considerably worse. Calls are now
serialised onto one lane with a floor on the gap between them, a 429 triggers a
real backoff, and polling stops entirely while the tab is hidden.

**Validators are geo-blocked from some sources, and the source lies about it.**
Binance answers GenLayer's validator nodes with **HTTP 200 and an error body**,
so code trusting the status line would have parsed a price out of an error
page. `tests/integration/probe_sources.py` checks what a validator actually
receives; the cover form warns before you write a policy against such a host.

**Contract source must be pure ASCII.** `genlayer-py` hex-encodes the source
ASCII-only, and an em dash in a comment breaks deployment *after* the contract
has already been created.

**`time.time()` is non-deterministic and will not pass the linter.** `_now()`
uses `datetime.datetime.now()`, which `genvm-lint` accepts and `gltest`'s
`vm.warp()` can patch.

## Deploying

There is no backend, so any static host works. `render.yaml` is a Render
blueprint — point Render at this repository as a Blueprint and the service is
created with the publish path and headers already set. Nothing to configure and
no secret to supply. `.github/workflows/pages.yml` publishes `frontend/` to
GitHub Pages on every push to `main` instead, if you prefer.

One consequence of having no keeper: a policy that expires sits at *awaiting
adjudication* until somebody clicks **Settle now**. That is correct rather than
broken — the contract deliberately gives no one special authority over
settlement — but a deployment nobody visits will accumulate unsettled policies.

## Known gaps

- **State grows without bound.** Policies are never pruned, so the JSON blob
  the contract stores gets monotonically larger. Fine for a hackathon,
  not fine forever.
- **No anti-spam bond on writing cover.** The premium floor stops free cover
  but not a flood of tiny policies.
- **The pool is one bucket.** Every underwriter shares every risk pro rata;
  there is no tranching and no per-trigger exposure limit, so one badly
  correlated set of policies can drain it.
