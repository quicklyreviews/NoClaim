/**
 * NoClaim - the cover desk.
 *
 * Three things shape this page.
 *
 * The network is not a footnote. Which chain this talks to, which contract, and
 * whether the connected wallet agrees, sit above everything else and turn amber
 * the moment they stop matching - because a page that asks you to sign should
 * never leave you guessing where the signature is going.
 *
 * "How to take part" is a live checklist rather than a set of instructions. It
 * ticks itself off from real state, so the question it answers is not "what are
 * the steps" but "which step am I on".
 *
 * Two audiences want opposite things from the same pool - a buyer wants the
 * trigger not to fire, an underwriter is paid for the risk that it might - so
 * they get tabs rather than competing for one long scroll.
 */
import {
  $, signer, isSignedIn, initWallet, connectWallet, signOut, ensureStudioChain,
  readChainId, chainLabel, makeContract, toast, withBusy, cleanError,
  gen, toWei, shorten, escapeHtml, setText, rpc, isBusy, isRateLimited,
  EXPLORER, ONE_GEN,
} from './wallet.js';
import { markSvg, faviconHref } from './brand.js';
import { COVER_TEMPLATES } from './cover-templates.js';

// Deployed and exercised by tests/integration/test_noclaim_studionet.py: cover
// refused when unbacked, a policy settled from real price evidence, and an
// unknowable trigger refunded in full.
const CONTRACT = '0xBF326FA29B839cF95d3c9d0895b7A852031C3822';
const BLOCKED_SOURCE_HOSTS = ['binance.com'];

const contract = makeContract(CONTRACT);

let pool = null;
let policies = [];
let mine = [];
let walletGen = 0n;
let uwStake = 0n;
let owed = 0n;

// --- the network bar -------------------------------------------------

function renderNetbar() {
  const connected = isSignedIn();
  const chain = chainLabel();
  const wrong = connected && !chain.ok;

  $('netbar').className = `netbar${wrong ? ' wrong' : ''}`;
  $('net-dot').className = `dot${wrong ? ' bad' : ''}`;
  setText($('net-name'), wrong ? chain.name : 'GenLayer StudioNet');
  $('net-id').hidden = wrong;
  $('btn-switch-chain').hidden = !wrong;

  $('net-account').hidden = !connected;
  $('btn-connect').hidden = connected;
  setText($('wallet-address'), shorten(signer.address));
  $('wallet-address').title = signer.address || '';
}

// --- how to take part ------------------------------------------------

/** The third step is done once you actually hold cover or have capital in the
 *  pool - not merely once you have looked at the form. */
function renderSteps() {
  const states = [
    ['step-connect', isSignedIn()],
    ['step-gen', walletGen > 0n],
    ['step-act', mine.length > 0 || uwStake > 0n],
  ];
  let pending = true;
  let done = 0;
  for (const [id, isDone] of states) {
    const li = $(id);
    li.classList.toggle('done', isDone);
    li.classList.toggle('now', !isDone && pending);
    if (isDone) done++;
    else pending = false;
  }
  // The checklist stays on the page once it is complete. Hiding it would take
  // away the only place that says how any of this works, which is exactly what
  // someone arriving second on a shared screen needs to read.
  setText($('start-progress'), done === 3 ? 'all set' : `step ${done + 1} of 3`);
  $('start').classList.toggle('complete', done === 3);
}

// --- the pool --------------------------------------------------------

async function loadPool() {
  pool = await contract.read('get_pool');
  setText($('stat-free'), `${gen(BigInt(pool.free), 2)} GEN`);
  setText($('stat-total'), `${gen(BigInt(pool.total), 2)} GEN`);
  setText($('stat-reserved'), `${gen(BigInt(pool.reserved), 2)} GEN`);
  setText($('stat-earned'), `${gen(BigInt(pool.premiums_earned), 3)} GEN`);
  setText($('stat-paid'), `${gen(BigInt(pool.payouts_made), 2)} GEN`);
  setText($('uw-earned'), gen(BigInt(pool.premiums_earned)));
  setText($('uw-free'), gen(BigInt(pool.free)));
  setText($('uw-count'), String(pool.underwriters ?? '-'));
  renderQuote();
}

// --- policies --------------------------------------------------------

/** Live cover first, then anything waiting on a verdict, then history. */
function sortForDisplay(list) {
  const rank = (p) => {
    if (p.status !== 'ACTIVE') return 2;
    return Number(p.expires_ts) > Math.floor(Date.now() / 1000) ? 0 : 1;
  };
  return [...list].sort((a, b) => rank(a) - rank(b) || Number(b.id) - Number(a.id));
}

/** `mine` changes the wording, not the facts.
 *
 *  "premium earned" is true, and it is the pool's sentence. Shown on your own
 *  policy it reads as though you earned something, when what actually happened
 *  is that the event did not occur and the cover paid nothing - which is the
 *  ordinary outcome of insurance and has to be said in those words. */
function policyState(p, mine = false) {
  const now = Math.floor(Date.now() / 1000);
  if (p.status === 'ACTIVE') {
    const left = Number(p.expires_ts) - now;
    if (left > 0) return { key: 'live', label: `cover ends in ${countdown(left)}` };
    return { key: 'due', label: 'expired - awaiting adjudication' };
  }
  if (p.settlement === 'PAID') {
    return { key: 'paid', label: mine ? 'fired - paid to you' : 'fired - paid out' };
  }
  if (p.settlement === 'REFUNDED') {
    return { key: 'refunded', label: 'unknowable - premium refunded' };
  }
  return {
    key: 'expired',
    label: mine ? 'did not happen - this paid nothing' : 'did not fire - premium earned',
  };
}

function countdown(seconds) {
  if (seconds <= 0) return 'a moment';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (seconds >= 300) return `${m}m`;
  if (m) return `${m}m ${String(s).padStart(2, '0')}s`;
  return `${s}s`;
}

function policyCard(p) {
  const isMine = signer.address && p.holder === signer.address.toLowerCase();
  const state = policyState(p, isMine);
  const payout = BigInt(p.payout || 0);
  const premium = BigInt(p.premium || 0);

  const card = document.createElement('article');
  card.className = `policy ${state.key}`;
  card.innerHTML = `
    <div class="policy-head">
      <h3>${escapeHtml(p.trigger)}</h3>
      <span class="badge ${state.key}">${escapeHtml(state.label)}</span>
    </div>
    <p class="criteria">${escapeHtml(p.criteria)}</p>
    <div class="terms">
      <div class="term">
        <span class="term-label">Sum insured</span>
        <span class="term-value">${gen(payout)} <span class="unit">GEN</span></span>
      </div>
      <div class="term">
        <span class="term-label">Premium</span>
        <span class="term-value">${gen(premium)} <span class="unit">GEN</span></span>
      </div>
      <div class="term">
        <span class="term-label">Held by</span>
        <span class="term-value mono">${escapeHtml(shorten(p.holder))}${isMine ? ' <span class="you">you</span>' : ''}</span>
      </div>
    </div>
    ${p.reasoning ? `
      <div class="verdict ${state.key}">
        <span class="label">GenLayer validators adjudicated</span>
        <p>${escapeHtml(p.reasoning)}</p>
      </div>` : ''}
    <div class="policy-actions"></div>
  `;

  if (state.key === 'due') {
    const b = document.createElement('button');
    b.className = 'ghost small';
    b.textContent = 'Settle now';
    b.title = 'Anyone can settle an expired policy - there is no adjuster to appoint';
    b.onclick = () => settle(p);
    card.querySelector('.policy-actions').appendChild(b);
  }
  return card;
}

async function loadPolicies() {
  const raw = await contract.read('get_all_policies');
  const parsed = typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {});
  policies = sortForDisplay(Object.values(parsed));

  // Derived from the list already in hand rather than asked for separately.
  // get_policies_of is the more principled source, but it costs a whole request
  // per refresh for a value this reply already contains, and the node's rate
  // limit is the binding constraint on this page.
  const me = signer.address ? signer.address.toLowerCase() : null;
  mine = me ? policies.filter((p) => p.holder === me) : [];
  renderPolicies();
}

function renderPolicies() {
  setText($('tab-book-count'), policies.length ? String(policies.length) : '');
  setText($('tab-mine-count'), mine.length ? String(mine.length) : '');

  const book = $('policies');
  book.innerHTML = '';
  if (!policies.length) {
    book.innerHTML = '<p class="empty">No cover has been written yet.</p>';
  } else {
    for (const p of policies) book.appendChild(policyCard(p));
  }

  const host = $('mine-policies');
  host.innerHTML = '';
  if (!isSignedIn()) {
    host.innerHTML = '<p class="empty">Connect a wallet to see the cover you hold.</p>';
  } else if (!mine.length) {
    host.innerHTML = '<p class="empty">You hold no cover yet. Buy some from the first tab.</p>';
  } else {
    for (const p of mine) host.appendChild(policyCard(p));
  }

  const now = Math.floor(Date.now() / 1000);
  let active = 0n, premiums = 0n, paid = 0n, refunded = 0n;
  for (const p of mine) {
    premiums += BigInt(p.premium || 0);
    if (p.status === 'ACTIVE' && Number(p.expires_ts) > now) active += BigInt(p.payout || 0);
    if (p.settlement === 'PAID') paid += BigInt(p.payout || 0);
    if (p.settlement === 'REFUNDED') refunded += BigInt(p.premium || 0);
  }
  setText($('mine-active'), gen(active, 2));
  setText($('mine-premiums'), gen(premiums, 3));
  setText($('mine-paid'), gen(paid, 2));
  setText($('mine-refunded'), gen(refunded, 3));

  renderSteps();
}

// --- quoting ---------------------------------------------------------

/** What the cover costs and whether the pool can back it, shown while the
 *  numbers are still being typed rather than after a refusal. */
function renderQuote() {
  const host = $('quote');
  if (!host || !pool) return;

  const payout = toWei($('f-payout').value || '0');
  const premium = toWei($('f-premium').value || '0');
  const free = BigInt(pool.free);
  const rate = BigInt(pool.min_rate_bps);
  const minPremium = (payout * rate) / 10000n;

  const backed = payout > 0n && payout <= free;
  const priced = premium > 0n && premium >= minPremium;

  host.innerHTML = `
    <div class="quote-row ${backed ? 'ok' : 'bad'}">
      <span class="q-label">Pool can back it</span>
      <span class="q-value">${backed
        ? `yes - ${gen(free, 2)} GEN is unreserved`
        : `no - only ${gen(free, 2)} GEN is unreserved`}</span>
    </div>
    <div class="quote-row ${priced ? 'ok' : 'bad'}">
      <span class="q-label">Minimum premium</span>
      <span class="q-value">${gen(minPremium)} GEN
        <span class="q-note">(${Number(rate) / 100}% of the sum insured)</span></span>
    </div>
    <div class="quote-row">
      <span class="q-label">If it happens you receive</span>
      <span class="q-value">${gen(payout)} GEN</span>
    </div>
    <div class="quote-row">
      <span class="q-label">If it does not happen</span>
      <span class="q-value">nothing - the pool keeps your ${gen(premium)} GEN</span>
    </div>
    <div class="quote-row">
      <span class="q-label">If it cannot be judged</span>
      <span class="q-value">${gen(premium)} GEN back - the pool earns nothing</span>
    </div>
  `;

  $('btn-buy').disabled = !(backed && priced && isSignedIn());
  setText($('buy-hint'),
    !isSignedIn() ? 'Connect a wallet to buy cover.'
      : !backed ? 'Lower the sum insured, or add capital to the pool.'
      : !priced ? 'Raise the premium to at least the minimum above.'
      : '');
}

// --- account ---------------------------------------------------------

function renderAccount() {
  renderNetbar();
  $('uw-actions').style.display = isSignedIn() ? '' : 'none';
  renderPolicies();
}

async function refreshAccount() {
  renderAccount();
  if (!signer.address) {
    walletGen = 0n; uwStake = 0n; owed = 0n;
    setText($('uw-stake'), '-');
    $('collect-panel').hidden = true;
    renderSteps();
    return;
  }
  // Each of these keeps its last good figure if the node refuses us, rather
  // than blanking the number. They are logged: a silent catch here once hid a
  // collectable balance behind a hidden banner for an entire session.
  try {
    walletGen = BigInt(await rpc('eth_getBalance', [signer.address, 'latest']));
  } catch (e) { console.debug('wallet balance', e); }
  try {
    owed = BigInt(await contract.read('get_balance', [signer.address]));
    $('collect-panel').hidden = owed === 0n;
    setText($('collect-line'), owed > 0n ? `${gen(owed)} GEN is yours to collect` : '');
  } catch (e) { console.debug('get_balance', e); }
  try {
    uwStake = BigInt(await contract.read('get_underwriter', [signer.address]));
    setText($('uw-stake'), gen(uwStake));
  } catch (e) { console.debug('get_underwriter', e); }
  renderSteps();
  renderQuote();
}

// --- actions ---------------------------------------------------------

/** Wait for money the contract sent to actually land, and say so only then.
 *
 *  `emit_transfer` does not move GEN inside the call that asks for it. The
 *  contract emits a transfer, and that becomes a *second* transaction which
 *  reaches consensus about a minute later. So the write returning means the
 *  contract agreed to pay, not that you have been paid - and this page used to
 *  say "Paid out to your wallet" at that moment, which was a straight lie:
 *  the user checked, saw nothing, and reasonably concluded it was broken.
 *
 *  Polls the wallet's own balance rather than trusting a timer, and gives up
 *  saying so honestly instead of pretending. */
/** Read fresh rather than trusting the cached figure: the comparison below is
 *  only meaningful against the balance as it stood the moment before we asked
 *  to be paid. */
async function walletBalanceNow() {
  try {
    return BigInt(await rpc('eth_getBalance', [signer.address, 'latest']));
  } catch {
    return walletGen;
  }
}

async function awaitPayout(before, label) {
  toast(`${label} - the contract is sending the GEN now, this takes about another minute`, 'info');
  for (let i = 0; i < 20; i++) {
    await new Promise((r) => setTimeout(r, 6000));
    let now;
    try {
      now = BigInt(await rpc('eth_getBalance', [signer.address, 'latest']));
    } catch { continue; }
    if (now > before) {
      walletGen = now;
      renderSteps();
      toast(`${gen(now - before)} GEN has arrived in your wallet`, 'success');
      return true;
    }
  }
  toast('The transfer has not shown up yet. It is on its way - check your wallet in a minute.', 'info');
  return false;
}

function requireSignIn(what) {
  if (isSignedIn()) return true;
  toast(`Connect a wallet to ${what}`, 'info');
  $('start').scrollIntoView({ behavior: 'smooth', block: 'center' });
  return false;
}

async function buyCover(event) {
  event.preventDefault();
  if (!requireSignIn('buy cover')) return;
  const expires = Math.floor(Date.now() / 1000) + Number($('f-window').value) * 60;
  await withBusy('Writing the policy', async () => {
    await contract.write('buy_policy', [
      $('f-trigger').value.trim(),
      $('f-criteria').value.trim(),
      $('f-sources').value.trim(),
      toWei($('f-payout').value),
      expires,
    ], toWei($('f-premium').value));
    toast('Cover is live', 'success');
    $('buy-form').reset();
    $('source-preview').hidden = true;
    await reloadAll({ force: true });
    showTab('mine');
  });
}

async function settle(policy) {
  if (!requireSignIn('settle a policy')) return;
  await withBusy('Adjudicating - validators are fetching the evidence', async () => {
    await contract.write('settle_policy', [Number(policy.id)]);
    await reloadAll({ force: true });
    const fresh = policies.find((p) => p.id === policy.id);
    // Said from the policyholder's side, because that is who is reading it.
    // "The pool keeps the premium" is the same fact, but it answers a question
    // they did not ask and leaves theirs - do I get anything? - unanswered.
    const said = {
      PAID: 'The event happened - the sum insured is yours, click Collect to take it',
      REFUNDED: 'Could not be judged - your premium is refunded, click Collect to take it',
      EXPIRED: 'The event did not happen, so this cover pays nothing. The premium stays with the pool.',
    }[fresh?.settlement];
    toast(said || 'Settled', fresh?.settlement === 'EXPIRED' ? 'info' : 'success');
  });
}

async function collect() {
  if (!requireSignIn('collect')) return;
  await withBusy('Collecting', async () => {
    const before = await walletBalanceNow();
    await contract.write('withdraw_all', []);
    await refreshAccount();
    await awaitPayout(before, 'Collected');
    await refreshAccount();
  });
}

async function fundPool() {
  if (!requireSignIn('underwrite')) return;
  const amount = prompt('How much GEN to put behind this pool?', '10');
  if (!amount) return;
  await withBusy('Adding capital', async () => {
    await contract.write('fund_pool', [], toWei(amount));
    toast('Capital added - you are now underwriting', 'success');
    await reloadAll({ force: true });
  });
}

async function withdrawPool() {
  if (!requireSignIn('withdraw')) return;
  const free = pool ? gen(BigInt(pool.free)) : '0';
  const amount = prompt(`Only unreserved capital can leave. ${free} GEN is free.`, free);
  if (!amount) return;
  await withBusy('Withdrawing', async () => {
    const before = await walletBalanceNow();
    await contract.write('withdraw_pool', [toWei(amount)]);
    await reloadAll({ force: true });
    await awaitPayout(before, 'Withdrawn from the pool');
    await refreshAccount();
  });
}

async function getTestGen() {
  if (!requireSignIn('get test GEN')) return;
  await withBusy('Requesting test GEN', async () => {
    await rpc('sim_fundAccount', [signer.address, Number(20n * ONE_GEN)]);
    await new Promise((r) => setTimeout(r, 2000));
    await refreshAccount();
    toast('Funded with 20 test GEN', 'success');
  });
}

// --- sources ---------------------------------------------------------

function checkSources() {
  const value = $('f-sources').value;
  const el = $('source-warning');
  const parts = value.split(',').map((u) => u.trim()).filter(Boolean);
  const broken = parts.find((u) => !/^https?:\/\//.test(u));
  if (broken) {
    el.textContent = `"${broken.slice(0, 40)}" is not a URL. Sources are comma separated, so a URL that itself contains a comma must use %2C instead.`;
    el.hidden = false;
    return;
  }
  const blocked = BLOCKED_SOURCE_HOSTS.find((h) => value.includes(h));
  if (blocked) {
    el.textContent = `${blocked} blocks GenLayer's validators by region and answers them with an error page, so a policy sourced from it can only ever settle UNKNOWN.`;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

/** Fetches whatever is in the sources box, so a trigger can be checked against
 *  real evidence before any money is committed to it. */
async function previewSources() {
  const urls = ($('f-sources').value || '').split(',').map((u) => u.trim()).filter(Boolean).slice(0, 3);
  const host = $('source-preview');
  if (!urls.length) { host.hidden = true; host.innerHTML = ''; return; }

  host.hidden = false;
  host.innerHTML = '<div class="source-row"><span class="status">...</span><div class="body">Reading sources</div></div>';

  const rows = await Promise.all(urls.map(async (url) => {
    try {
      const res = await fetch(url, { cache: 'no-store' });
      return { url, ok: res.ok, status: res.ok ? 'ok' : `HTTP ${res.status}`, text: (await res.text()).slice(0, 400) };
    } catch {
      // A browser CORS refusal says nothing about whether a validator can read
      // it - they fetch server-side - so this is unknown, not bad.
      return {
        url, ok: null, status: 'no preview',
        text: 'The browser could not fetch this, usually CORS. Validators fetch it themselves, so it may still settle fine.',
      };
    }
  }));

  host.innerHTML = '';
  for (const r of rows) {
    const row = document.createElement('div');
    row.className = `source-row ${r.ok === true ? 'ok' : r.ok === false ? 'bad' : ''}`;
    row.innerHTML = `
      <span class="status">${escapeHtml(r.status)}</span>
      <div class="body">
        <span class="url">${escapeHtml(r.url)}</span>
        <pre>${escapeHtml(r.text)}</pre>
      </div>`;
    host.appendChild(row);
  }
}

function buildTemplates() {
  const host = $('template-chips');
  for (const t of COVER_TEMPLATES) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chip';
    b.textContent = t.label;
    b.onclick = async () => {
      const original = b.textContent;
      b.textContent = 'Reading source...';
      b.disabled = true;
      try {
        const built = await t.build();
        if (!built) { toast('That source is not answering right now', 'error'); return; }
        $('f-trigger').value = built.trigger;
        $('f-criteria').value = built.criteria;
        $('f-sources').value = t.sources;
        $('f-window').value = String(t.minutes);
        if (built.payout) $('f-payout').value = built.payout;
        if (built.premium) $('f-premium').value = built.premium;
        setText($('template-note'), built.note);
        checkSources();
        renderQuote();
        await previewSources();
        $('f-trigger').focus();
      } catch (e) {
        console.error(e);
        toast(`Could not read the source: ${cleanError(e)}`, 'error');
      } finally {
        b.textContent = original;
        b.disabled = false;
      }
    };
    host.appendChild(b);
  }
}

// --- tabs ------------------------------------------------------------

function showTab(name) {
  for (const t of document.querySelectorAll('.tab')) {
    const on = t.dataset.tab === name;
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', String(on));
  }
  for (const p of document.querySelectorAll('.tabpanel')) {
    p.classList.toggle('active', p.id === `panel-${name}`);
  }
}

// --- boot ------------------------------------------------------------

let reloading = null;

/** Coalesced, because two callers arrive together at the two moments that
 *  matter: boot both initialises the wallet and renders, and connecting fires
 *  an auth change *and* returns to whoever clicked. Left alone that is ten
 *  requests in one burst, which is what trips the node's rate limit - and a
 *  rate-limited read fails quietly, so the page just looks wrong.
 *
 *  `force` is for after a write, where the point is to see the new state and
 *  reusing a reload that started beforehand would show the old one. */
function reloadAll({ force = false } = {}) {
  if (reloading && !force) return reloading;
  const previous = reloading;
  const run = (async () => {
    if (previous) await previous.catch(() => {});
    await loadPool().catch((e) => console.error('pool', e));
    await loadPolicies().catch((e) => console.error('policies', e));
    await refreshAccount();
  })();
  reloading = run;
  run.catch(() => {}).then(() => { if (reloading === run) reloading = null; });
  return run;
}

async function main() {
  $('mark-slot').innerHTML = markSvg(30);
  $('favicon').href = faviconHref();
  setText($('contract-address'), shorten(CONTRACT));
  $('net-contract').href = `${EXPLORER}/address/${CONTRACT}`;
  $('net-contract').title = CONTRACT;
  $('explorer-link').href = `${EXPLORER}/address/${CONTRACT}`;

  // Connecting changes which policies are "mine", so an auth change re-reads
  // rather than merely re-rendering what is already in hand.
  await initWallet({
    onAuthChange: () => {
      renderAccount();
      reloadAll().catch((e) => console.error('reload', e));
    },
  });

  // connectWallet fires the auth change, which reloads; doing it again here is
  // how the burst that trips the rate limit gets built.
  const connect = () => connectWallet();
  $('btn-connect').onclick = connect;
  $('step-connect-btn').onclick = connect;
  $('btn-signout').onclick = signOut;
  $('btn-switch-chain').onclick = async () => {
    const ok = await ensureStudioChain();
    signer.chainId = await readChainId();
    renderNetbar();
    toast(ok ? 'Now on StudioNet' : 'Could not switch - approve it in your wallet', ok ? 'success' : 'error');
    if (ok) await reloadAll({ force: true });
  };

  $('step-gen-btn').onclick = getTestGen;
  $('step-buy-btn').onclick = () => { showTab('buy'); $('panel-buy').scrollIntoView({ behavior: 'smooth' }); };
  $('step-uw-btn').onclick = () => { showTab('underwrite'); $('panel-underwrite').scrollIntoView({ behavior: 'smooth' }); };

  $('btn-collect-all').onclick = collect;
  $('btn-fund-pool').onclick = fundPool;
  $('btn-withdraw-pool').onclick = withdrawPool;

  $('buy-form').addEventListener('submit', buyCover);
  $('btn-check-sources').onclick = () => previewSources();
  $('f-sources').addEventListener('input', checkSources);
  for (const id of ['f-payout', 'f-premium']) $(id).addEventListener('input', renderQuote);
  $('btn-refresh').onclick = () => reloadAll({ force: true }).catch((e) => toast(cleanError(e), 'error'));

  for (const t of document.querySelectorAll('.tab')) {
    t.onclick = () => showTab(t.dataset.tab);
  }

  buildTemplates();
  await reloadAll();

  // Countdowns are local and free. The chain is asked once a minute, and not at
  // all while the tab is hidden or the node is refusing us.
  setInterval(() => { if (!isBusy()) renderPolicies(); }, 1000);
  setInterval(() => {
    if (!isBusy() && !document.hidden && !isRateLimited()) reloadAll().catch(() => {});
  }, 60000);
}

main();
