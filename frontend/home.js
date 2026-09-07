/**
 * The NoClaim home page.
 *
 * Read-only and wallet-free by design: nothing here needs an account, so
 * nothing here asks for one. A landing page that opens a wallet prompt before
 * saying what the product is has the order backwards.
 *
 * The figures and the policy cards come off the chain on load rather than
 * being written into the HTML. A landing page quoting numbers that were true
 * the day someone wrote them is a brochure; this one is either current or
 * honestly blank.
 */
import { createClient } from 'genlayer-js';
import { studionet } from 'genlayer-js/chains';
import { markSvg, faviconHref } from './brand.js';

const RPC = 'https://studio.genlayer.com/api';
const EXPLORER = 'https://genlayer-explorer.vercel.app';
const CONTRACT = '0xBF326FA29B839cF95d3c9d0895b7A852031C3822';
const ONE_GEN = 10n ** 18n;

const $ = (id) => document.getElementById(id);
const client = createClient({ chain: studionet, endpoint: RPC });

const shorten = (addr) => (addr ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : '-');

function gen(wei, dp = 2) {
  return (Number(typeof wei === 'bigint' ? wei : BigInt(wei || 0)) / Number(ONE_GEN)).toFixed(dp);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/** Reads retry, because DNS to studio.genlayer.com is intermittently flaky and
 *  a dropped lookup would otherwise leave the page looking dead. */
async function read(functionName, args = [], tries = 3) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      return await client.readContract({ address: CONTRACT, functionName, args });
    } catch (e) {
      last = e;
      await new Promise((r) => setTimeout(r, 1200 * (i + 1)));
    }
  }
  throw last;
}

function countdown(seconds) {
  if (seconds <= 0) return 'expiring';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m || 1}m`;
}

/** One read, three places on the page. The pool is the whole argument this
 *  page makes, so it is quoted in the hero, in the solvency table, and
 *  reconciled out loud underneath it. */
async function loadPool() {
  const pool = await read('get_pool');
  const total = BigInt(pool.total);
  const reserved = BigInt(pool.reserved);
  const free = BigInt(pool.free);
  const earned = BigInt(pool.premiums_earned);

  $('stat-pool').textContent = `${gen(total)} GEN`;
  $('stat-free').textContent = `${gen(free)} GEN`;

  $('sv-total').textContent = `${gen(total)} GEN`;
  $('sv-reserved').textContent = `${gen(reserved)} GEN`;
  $('sv-free').textContent = `${gen(free)} GEN`;
  $('sv-earned').textContent = `${gen(earned, 3)} GEN`;

  // Said as an equation rather than left for the reader to spot, because
  // "reserved never exceeds capital" is the claim, and a claim stated next to
  // the numbers that prove it is worth more than one stated on its own.
  $('solvency-note').textContent = reserved === 0n
    ? `No cover is live right now, so all ${gen(total)} GEN is free to underwrite.`
    : `${gen(reserved)} of ${gen(total)} GEN is spoken for, leaving ${gen(free)} GEN `
      + 'that can still be written or withdrawn. Underwriters cannot take out the rest.';
}

async function loadStats() {
  const st = await read('get_stats');
  $('stat-policies').textContent = String(st.policies_written ?? '-');
}

const STATE = {
  live: { key: 'live', label: (p, now) => `cover ends in ${countdown(Number(p.expires_ts) - now)}` },
  due: { key: 'due', label: () => 'expired - awaiting adjudication' },
  paid: { key: 'paid', label: () => 'fired - paid out' },
  refunded: { key: 'refunded', label: () => 'unknowable - premium refunded' },
  expired: { key: 'expired', label: () => 'did not fire - premium earned' },
};

function stateOf(p, now) {
  if (p.status === 'ACTIVE') return Number(p.expires_ts) > now ? STATE.live : STATE.due;
  if (p.settlement === 'PAID') return STATE.paid;
  if (p.settlement === 'REFUNDED') return STATE.refunded;
  return STATE.expired;
}

async function loadPolicies() {
  const raw = await read('get_all_policies');
  const all = Object.values(typeof raw === 'string' ? JSON.parse(raw || '{}') : (raw || {}));
  const now = Math.floor(Date.now() / 1000);

  // Live cover is the point of the page. Only when none is in force does it
  // fall back to the rest of the book - showing an empty shell when the
  // contract has a history of real adjudications would undersell it.
  const live = all
    .filter((p) => p.status === 'ACTIVE' && Number(p.expires_ts) > now)
    .sort((a, b) => Number(a.expires_ts) - Number(b.expires_ts));

  // Everything else, newest first, and deliberately not filtered to settled
  // policies: one that has expired and is waiting for someone to adjudicate it
  // belongs to neither bucket, and an earlier version dropped it off the page
  // altogether - which is the single most interesting card here, because
  // anyone can settle it.
  const rest = all
    .filter((p) => !live.includes(p))
    .sort((a, b) => Number(b.id) - Number(a.id));

  // The heading has to describe what is actually below it. Promising "in force
  // right now" over a list of settled policies would be the page telling a
  // small lie about itself, which is the one thing a contract built on
  // refusing to guess cannot do.
  const showingLive = live.length > 0;
  const showing = (showingLive ? live : rest).slice(0, 3);
  $('policies-heading').textContent = showingLive ? 'Cover in force right now' : 'Recently written';

  const host = $('home-policies');
  if (!showing.length) {
    host.innerHTML = '<p class="empty">No cover written yet. Be the first.</p>';
    return;
  }

  host.innerHTML = '';
  for (const p of showing) {
    const state = stateOf(p, now);
    const card = document.createElement('a');
    card.className = `home-market ${state.key}`;
    card.href = 'noclaim.html';
    card.innerHTML = `
      <div class="hm-head">
        <h3>${escapeHtml(p.trigger)}</h3>
        <span class="badge ${state.key}">${escapeHtml(state.label(p, now))}</span>
      </div>
      ${p.reasoning ? `<p class="hm-reason">${escapeHtml(p.reasoning)}</p>` : ''}
      <div class="hm-foot">
        <span class="hm-yes">${gen(BigInt(p.payout || 0))} GEN insured</span>
        <span class="hm-no">${gen(BigInt(p.premium || 0), 3)} GEN premium</span>
      </div>
    `;
    host.appendChild(card);
  }
}

async function main() {
  $('mark-slot').innerHTML = markSvg(28);
  $('favicon').href = faviconHref();

  $('cover-address').textContent = shorten(CONTRACT);
  $('net-contract-cover').href = `${EXPLORER}/address/${CONTRACT}`;
  $('net-contract-cover').title = CONTRACT;
  $('footer-address').textContent = CONTRACT;
  $('explorer-link').href = `${EXPLORER}/address/${CONTRACT}`;

  // Sequential, not parallel: three reads fired at once is the burst StudioNet
  // rate-limits, and a landing page has no deadline worth that risk. Each is
  // caught on its own so a stumble on one does not blank the others.
  try { await loadPool(); } catch (e) {
    console.error('pool', e);
    $('solvency-note').textContent = 'Could not reach the contract just now.';
  }
  try { await loadStats(); } catch (e) { console.error('stats', e); }
  try { await loadPolicies(); } catch (e) {
    console.error('policies', e);
    $('home-policies').innerHTML =
      '<p class="empty">Could not reach the contract just now. The cover desk still works.</p>';
  }
}

main();
