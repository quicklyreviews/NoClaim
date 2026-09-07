/**
 * The mark, shared by both desks.
 *
 * Two blades sweeping up to a point with a shard held between them - it reads
 * as an "A", which is why it suits the name. Drawn as inline SVG rather than
 * shipped as a file so it inherits the theme's ink colour and stays crisp at
 * favicon size, where a raster would go to mush.
 *
 * `currentColor` throughout: the mark is one colour by design, and letting CSS
 * decide it is what keeps it legible on both the light and dark grounds.
 */

// Each blade is a four-point polygon: apex, bottom outer corner, a bottom-inner
// point well short of centre, and a kink partway up the inner edge. The two
// inner-bottom points sit far apart on purpose - that wide inverted V is what
// leaves the centre shard floating in white rather than welded to the blades.
const MARK_PATHS = `
  <path d="M46.5 3.5 3.5 96.5 32.5 79 43 49.5Z"/>
  <path d="M53.5 3.5 96.5 96.5 67.5 79 57 49.5Z"/>
  <path d="M50 47 60.5 69 50 77.5 39.5 69Z"/>
`;

/** Inline mark for the page itself.
 *
 *  Decorative rather than labelled: it always sits next to the wordmark, so a
 *  screen reader announcing the name twice is noise - and it carried the wrong
 *  name on the cover desk for as long as the label was hard-coded. */
export function markSvg(size = 30) {
  return `<svg class="mark" width="${size}" height="${size}" viewBox="0 0 100 100"
    aria-hidden="true" focusable="false" fill="currentColor">${MARK_PATHS}</svg>`;
}

/** Same geometry as a favicon. Data URI, so there is no second request and no
 *  file to fall out of sync with the mark on the page. */
export function faviconHref() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" fill="#141a24">${MARK_PATHS}</svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}
