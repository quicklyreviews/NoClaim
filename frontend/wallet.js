/**
 * Wallet, network and the small helpers every page here needs.
 *
 * Extracted rather than copied. The chain-switch path in particular took three
 * attempts to get right - MetaMask reports an unrecognised chain with the 4902
 * code nested inside err.data.originalError, adding a network does not reliably
 * select it, and handing createClient an endpoint alongside a provider stops
 * writes being signed by the wallet at all. Two copies of that would mean
 * fixing it twice and forgetting once.
 *
 * Each page owns its own rendering; this module owns the state and hands back
 * what changed.
 */
import { createClient } from 'genlayer-js';
import { studionet } from 'genlayer-js/chains';
import { TransactionStatus } from 'genlayer-js/types';

export const RPC = 'https://studio.genlayer.com/api';
export const EXPLORER = 'https://genlayer-explorer.vercel.app';
export const CHAIN_ID_HEX = '0xf22f'; // 61999
export const ONE_GEN = 10n ** 18n;

const MODE_STORAGE = 'gl_signer_mode';

// Chains a wallet is likely to be sitting on, so a wrong one can be named
// rather than shown as a bare hex id nobody reads.
const KNOWN_CHAINS = {
  '0xf22f': 'GenLayer StudioNet',
  '0x1': 'Ethereum mainnet',
  '0xaa36a7': 'Sepolia',
  '0x89': 'Polygon',
  '0x38': 'BNB Chain',
  '0x2105': 'Base',
  '0xa4b1': 'Arbitrum One',
};

export const signer = {
  mode: null,      // null = signed out, 'wallet' once connected
  address: null,
  client: null,
  chainId: null,
};

// Reading is not an account action, so it gets its own client and works signed
// out. Browsing is how somebody decides whether to connect at all.
export let readClient = null;

let onChange = () => {};

// --- formatting ------------------------------------------------------

export const $ = (id) => document.getElementById(id);

export function gen(wei, dp = 3) {
  const n = typeof wei === 'bigint' ? wei : BigInt(wei || 0);
  return (Number(n) / Number(ONE_GEN)).toFixed(dp);
}

export function toWei(amount) {
  const [whole, frac = ''] = String(amount).split('.');
  return BigInt(whole || 0) * ONE_GEN + BigInt((frac + '0'.repeat(18)).slice(0, 18));
}

export function shorten(addr) {
  return addr ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : '-';
}

export function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function setText(el, value) {
  if (el && el.textContent !== value) el.textContent = value;
}

export function toast(message, kind = 'info') {
  const el = $('toast');
  if (!el) return;
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 9000 : 5000);
}

/** Contract errors arrive wrapped in transport noise; the useful part is the
 *  message the contract itself raised. */
export function cleanError(e) {
  const raw = String(e?.message || e);
  const m = raw.match(/UserError[^"]*?:?\s*([^"'\\}]{5,200})/);
  if (m) return m[1].trim();
  return raw.length > 200 ? raw.slice(0, 200) + '...' : raw;
}

let busy = false;
export const isBusy = () => busy;

/** Runs an action with the buttons disabled, so a slow consensus round cannot
 *  be double-submitted by an impatient second click. */
export async function withBusy(label, fn) {
  if (busy) { toast('Still waiting on the previous transaction', 'info'); return; }
  busy = true;
  document.body.classList.add('busy');
  try {
    toast(`${label}... this takes about a minute to reach consensus`);
    return await fn();
  } catch (e) {
    console.error(e);
    toast(cleanError(e), 'error');
  } finally {
    busy = false;
    document.body.classList.remove('busy');
  }
}

/**
 * Every call to StudioNet goes through here, one at a time.
 *
 * The node rate-limits: reads get 300 a minute, writes far fewer. When it
 * refuses, it answers 429 *without* CORS headers, so the browser cannot read
 * the response and reports "blocked by CORS policy" instead - which sends you
 * hunting for a CORS bug that does not exist. The give-away is
 * `X-RateLimit-Remaining` in the headers of every successful reply.
 *
 * Retrying naively made this worse: each failure fired three more requests into
 * a bucket that was already empty. So calls are serialised, spaced, and backed
 * off when refused, and the page is told to stop asking rather than to ask
 * harder.
 */
let queue = Promise.resolve();
let backoffUntil = 0;
let rateLimitNotified = 0;

const MIN_GAP_MS = 120;     // never fire two calls back to back
let lastCallAt = 0;

export const isRateLimited = () => Date.now() < backoffUntil;

function looksRateLimited(e) {
  const s = String(e?.message || e).toLowerCase();
  return s.includes('rate limit') || s.includes('429')
    // A 429 with no CORS headers reaches the browser as an opaque network
    // failure, so this shape has to count as rate limiting too.
    || s.includes('failed to fetch') || s.includes('load failed');
}

function noteRateLimit(seconds = 20) {
  backoffUntil = Math.max(backoffUntil, Date.now() + seconds * 1000);
  // Say it once per pause rather than once per call.
  if (Date.now() - rateLimitNotified > 30000) {
    rateLimitNotified = Date.now();
    toast(`StudioNet is rate limiting this page. Pausing for ${seconds}s - the numbers below may be a moment behind.`, 'info');
  }
}

/** Serialises everything onto one lane, with a floor on the gap between calls. */
function enqueue(fn) {
  const run = queue.then(async () => {
    if (isRateLimited()) {
      await new Promise((r) => setTimeout(r, backoffUntil - Date.now()));
    }
    const gap = MIN_GAP_MS - (Date.now() - lastCallAt);
    if (gap > 0) await new Promise((r) => setTimeout(r, gap));
    lastCallAt = Date.now();
    return fn();
  });
  // Keep the lane open even when one call throws.
  queue = run.catch(() => {});
  return run;
}

export async function rpc(method, params, tries = 2) {
  let last;
  for (let i = 0; i < tries; i++) {
    try {
      return await enqueue(async () => {
        const res = await fetch(RPC, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ jsonrpc: '2.0', method, params, id: 1 }),
        });
        if (res.status === 429) {
          noteRateLimit(Number(res.headers.get('Retry-After')) || 20);
          throw new Error('rate limited');
        }
        const body = await res.json();
        if (body.error) throw new Error(body.error.message || JSON.stringify(body.error));
        return body.result;
      });
    } catch (e) {
      last = e;
      if (looksRateLimited(e)) { noteRateLimit(); break; }
      await new Promise((r) => setTimeout(r, 1200 * (i + 1)));
    }
  }
  throw last;
}

// --- network ---------------------------------------------------------

export function getProvider() {
  return window.ethereum || window.okxwallet || null;
}

/** MetaMask reports "unrecognised chain" in more than one shape: sometimes as
 *  err.code, sometimes buried in err.data.originalError.code. Missing the
 *  nested one means never offering to add the network. */
function isUnknownChainError(err) {
  const codes = [err?.code, err?.data?.originalError?.code, err?.data?.code, err?.cause?.code];
  return codes.includes(4902) || codes.includes(-32603);
}

export async function readChainId() {
  const provider = getProvider();
  if (!provider) return null;
  try {
    return await provider.request({ method: 'eth_chainId' });
  } catch {
    return null;
  }
}

/** Put the wallet on StudioNet, and say plainly whether it worked. Returns a
 *  boolean rather than throwing: a refused switch should leave the wallet
 *  connected and the problem visible, not discard the account just approved. */
export async function ensureStudioChain() {
  const provider = getProvider();
  if (!provider) return false;
  if ((await readChainId()) === CHAIN_ID_HEX) return true;

  try {
    await provider.request({
      method: 'wallet_switchEthereumChain',
      params: [{ chainId: CHAIN_ID_HEX }],
    });
  } catch (err) {
    if (!isUnknownChainError(err)) {
      console.error('switch chain failed', err);
      return (await readChainId()) === CHAIN_ID_HEX;
    }
    try {
      await provider.request({
        method: 'wallet_addEthereumChain',
        params: [{
          chainId: CHAIN_ID_HEX,
          chainName: 'GenLayer StudioNet',
          rpcUrls: [RPC],
          nativeCurrency: { name: 'GEN', symbol: 'GEN', decimals: 18 },
          blockExplorerUrls: [EXPLORER],
        }],
      });
      // Adding a network does not reliably select it, so ask again.
      try {
        await provider.request({
          method: 'wallet_switchEthereumChain',
          params: [{ chainId: CHAIN_ID_HEX }],
        });
      } catch { /* verified below */ }
    } catch (addErr) {
      console.error('add chain failed', addErr);
      return false;
    }
  }
  return (await readChainId()) === CHAIN_ID_HEX;
}

export function chainLabel() {
  const id = signer.chainId;
  if (id === CHAIN_ID_HEX) return { name: 'GenLayer StudioNet (61999)', ok: true };
  if (!id) return { name: 'Unknown', ok: false };
  return { name: `${KNOWN_CHAINS[id] || 'Unknown chain'} (${parseInt(id, 16)}) - wrong network`, ok: false };
}

// --- signing in ------------------------------------------------------

export const isSignedIn = () => signer.mode !== null;

/** The proven call shape: chain + account + provider, and no endpoint. Handing
 *  it an HTTP endpoint as well is what stops writes going through the wallet. */
function attachWallet(address, provider) {
  signer.mode = 'wallet';
  signer.address = address;
  signer.client = createClient({ chain: studionet, account: address, provider });
  localStorage.setItem(MODE_STORAGE, 'wallet');
}

function bindProviderEvents(provider) {
  if (provider._glBound) return;
  provider._glBound = true;

  provider.on?.('accountsChanged', async (accs) => {
    if (signer.mode !== 'wallet') return;
    if (!accs || !accs.length) { signOut(); return; }
    attachWallet(accs[0], provider);
    onChange();
    toast(`Switched to ${shorten(accs[0])}`);
  });

  // Reloading on every network change loses whatever the user was doing.
  provider.on?.('chainChanged', async (id) => {
    signer.chainId = id;
    onChange();
  });
}

export async function connectWallet({ silent = false } = {}) {
  const provider = getProvider();
  if (!provider) {
    if (!silent) toast('No wallet found - install MetaMask or another EVM wallet', 'error');
    return false;
  }

  let accounts;
  try {
    accounts = await provider.request({
      method: silent ? 'eth_accounts' : 'eth_requestAccounts',
    });
  } catch (e) {
    console.error('wallet connect rejected', e);
    if (!silent) toast(e?.code === 4001 ? 'Connection cancelled' : cleanError(e), 'error');
    return false;
  }
  if (!accounts || !accounts.length) {
    if (!silent) toast('Your wallet returned no accounts - unlock it and try again', 'error');
    return false;
  }

  // Register the account BEFORE touching the network. Switching chains can fail
  // or be refused, and none of that is a reason to throw away a wallet the user
  // just approved.
  attachWallet(accounts[0], provider);
  bindProviderEvents(provider);

  signer.chainId = await readChainId();
  if (signer.chainId !== CHAIN_ID_HEX) {
    const ok = await ensureStudioChain();
    signer.chainId = await readChainId();
    if (!ok && !silent) {
      toast('Connected, but your wallet is not on StudioNet - use Switch', 'error');
    }
  }

  onChange();
  if (!silent && signer.chainId === CHAIN_ID_HEX) {
    toast(`Connected ${shorten(accounts[0])} on StudioNet`, 'success');
  }
  return true;
}

export function signOut() {
  signer.mode = null;
  signer.address = null;
  signer.client = null;
  signer.chainId = null;
  localStorage.removeItem(MODE_STORAGE);
  onChange();
  toast('Disconnected. Your wallet keeps its keys - nothing of yours was stored here.');
}

/** Sets up the read client and restores a previous session, but only in ways
 *  that need no permission: opening a page must never raise a prompt. */
export async function initWallet({ onAuthChange = () => {} } = {}) {
  onChange = onAuthChange;
  readClient = createClient({ chain: studionet, endpoint: RPC });

  if (localStorage.getItem(MODE_STORAGE) === 'wallet') {
    // MetaMask can inject after this script runs, so give it a moment.
    for (let i = 0; i < 20 && !getProvider(); i++) await new Promise((r) => setTimeout(r, 100));
    if (getProvider()) await connectWallet({ silent: true });
  }
  onChange();
}

// --- contract calls --------------------------------------------------

export function makeContract(address) {
  return {
    address,

    async read(functionName, args = [], tries = 2) {
      let last;
      for (let i = 0; i < tries; i++) {
        try {
          return await enqueue(() =>
            (readClient || signer.client).readContract({ address, functionName, args }));
        } catch (e) {
          last = e;
          // Backing off beats retrying into an empty bucket.
          if (looksRateLimited(e)) { noteRateLimit(); break; }
          await new Promise((r) => setTimeout(r, 1200 * (i + 1)));
        }
      }
      throw last;
    },

    async write(functionName, args = [], value = 0n) {
      // A wallet signs for whatever chain it is on, and a signature against the
      // wrong one fails confusingly rather than loudly.
      const ok = await ensureStudioChain();
      signer.chainId = await readChainId();
      onChange();
      if (!ok) {
        throw new Error('Your wallet is not on GenLayer StudioNet (chain 61999). Switch network and try again.');
      }
      let hash;
      try {
        hash = await enqueue(() =>
          signer.client.writeContract({ address, functionName, args, value }));
      } catch (e) {
        // Writes have their own, much smaller budget. Say what actually
        // happened rather than letting a CORS-shaped error stand.
        if (looksRateLimited(e)) {
          noteRateLimit(30);
          throw new Error('StudioNet is rate limiting transactions right now. Wait about half a minute and try again.');
        }
        throw e;
      }
      await signer.client.waitForTransactionReceipt({
        hash, status: TransactionStatus.ACCEPTED, interval: 3000, retries: 60,
      });
      // ACCEPTED does not mean a read will see it yet: reading straight after a
      // write returned pre-transaction state, so a success message appeared over
      // unchanged numbers. Waiting a beat removes that.
      await new Promise((r) => setTimeout(r, 4000));
      return hash;
    },
  };
}
