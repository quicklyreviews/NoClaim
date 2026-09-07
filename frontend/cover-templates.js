/**
 * Worked examples of parametric cover.
 *
 * Each reads its source when clicked and sets the trigger just past where
 * things currently stand, because a threshold typed last week is either
 * already breached or unreachable, and neither is a risk anyone would pay to
 * cover.
 *
 * They are all genuinely parametric: a named measurement crosses a named
 * threshold, and that is the whole claim process. No loss assessment, no
 * receipts, no argument about what "damage" means.
 *
 * Every host was checked against what a GenLayer validator actually receives,
 * with tests/integration/probe_sources.py. Do not add one that has not been.
 */

const USGS_M5_24H =
  'https://earthquake.usgs.gov/fdsnws/event/1/count?format=geojson&minmagnitude=5&starttime=now-1days';
const COINGECKO_BTC = 'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd';
const COINBASE_BTC = 'https://api.coinbase.com/v2/prices/BTC-USD/spot';

// One field per URL: the sources box is comma separated, so a URL carrying a
// literal comma gets split in half and half of it registered as a broken source.
const meteo = (lat, lon, tz, field) =>
  `https://api.open-meteo.com/v1/forecast?latitude=${lat}&longitude=${lon}`
  + `&timezone=${tz}&forecast_days=2&daily=${field}`;

const HANOI_RAIN = meteo('21.0278', '105.8342', 'Asia%2FBangkok', 'precipitation_sum');
const HANOI_HEAT = meteo('21.0278', '105.8342', 'Asia%2FBangkok', 'temperature_2m_max');

async function fetchJson(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export const COVER_TEMPLATES = [
  {
    label: 'Rainfall cover',
    minutes: 1440,
    sources: HANOI_RAIN,
    async build() {
      const data = await fetchJson(HANOI_RAIN);
      const mm = Number(data?.daily?.precipitation_sum?.[1]);
      // Cover the downpour, not the drizzle: a threshold a little above the
      // forecast is the shape a real weather policy takes.
      const threshold = Number.isFinite(mm) ? Math.max(5, Math.ceil(mm) + 5) : 10;
      return {
        trigger: `Will Hanoi get more than ${threshold}mm of rain tomorrow?`,
        criteria:
          'The source is an Open-Meteo forecast for Hanoi. Read the second entry of '
          + `daily.precipitation_sum, which is tomorrow, in millimetres. Answer FIRED if it `
          + `is more than ${threshold}. Answer NOT_FIRED if it is ${threshold} or less. If `
          + 'that entry is missing, answer UNKNOWN.',
        payout: '5',
        premium: '0.25',
        note: Number.isFinite(mm)
          ? `Tomorrow is forecast at ${mm}mm, so this covers a wetter day than expected - the case a market stall or a delivery round would insure.`
          : 'Forecast unavailable at the moment.',
      };
    },
  },
  {
    label: 'Heatwave cover',
    minutes: 1440,
    sources: HANOI_HEAT,
    async build() {
      const data = await fetchJson(HANOI_HEAT);
      const t = Number(data?.daily?.temperature_2m_max?.[1]);
      if (!Number.isFinite(t)) return null;
      const threshold = Math.ceil(t) + 2;
      return {
        trigger: `Will Hanoi exceed ${threshold}C tomorrow?`,
        criteria:
          'The source is an Open-Meteo forecast for Hanoi. Read the second entry of '
          + `daily.temperature_2m_max, tomorrow's high in Celsius. Answer FIRED if it is `
          + `above ${threshold}. Answer NOT_FIRED if it is ${threshold} or below. If that `
          + 'entry is missing, answer UNKNOWN.',
        payout: '5',
        premium: '0.2',
        note: `Forecast high is ${t}C. Cover above that is what an outdoor crew or a cold chain would buy.`,
      };
    },
  },
  {
    label: 'Earthquake cover',
    minutes: 1440,
    sources: USGS_M5_24H,
    async build() {
      const data = await fetchJson(USGS_M5_24H);
      const count = Number(data?.count);
      if (!Number.isFinite(count)) return null;
      const threshold = count + 4;
      return {
        trigger: `Will more than ${threshold} magnitude-5+ earthquakes strike worldwide in the next 24 hours?`,
        criteria:
          'The source counts magnitude-5.0-and-above earthquakes anywhere on Earth over '
          + `the last 24 hours. Read the "count" field. Answer FIRED if it is greater than `
          + `${threshold}. Answer NOT_FIRED if it is ${threshold} or fewer. If the field is `
          + 'missing, answer UNKNOWN.',
        payout: '10',
        premium: '0.5',
        note: `The last 24 hours saw ${count}. This is catastrophe cover in miniature: a global measurement, paid on the number rather than on an assessment of anyone's loss.`,
      };
    },
  },
  {
    label: 'Price crash cover',
    minutes: 360,
    sources: [COINGECKO_BTC, COINBASE_BTC].join(','),
    async build() {
      const data = await fetchJson(COINGECKO_BTC);
      const price = Number(data?.bitcoin?.usd);
      if (!price) return null;
      // A floor a few per cent below the market: the shape of a hedge rather
      // than a bet on a crash that has already happened.
      const floor = Math.floor((price * 0.97) / 100) * 100;
      return {
        trigger: `Will Bitcoin fall below ${floor.toLocaleString('en-US')} USD?`,
        criteria:
          'Read the BTC/USD price from any of the source JSON bodies. Answer FIRED if that '
          + `price is below ${floor}. Answer NOT_FIRED if it is ${floor} or above. If no `
          + 'source returned a usable price, answer UNKNOWN.',
        payout: '8',
        premium: '0.4',
        note: `BTC is ${price.toLocaleString('en-US')} USD, so this pays if it drops about 3% inside six hours - a hedge, bought by someone who would rather it did not.`,
      };
    },
  },
];
