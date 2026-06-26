// ─── Build Tracker configuration ───────────────────────────────────────────
// Edit this file and commit. The GitHub Action re-bakes data.json on push.

const TRACKER_CONFIG = {
  // Your GitHub username.
  username: "eld1kz",

  // Your name, shown in the header.
  ownerName: "Eldar",

  // Curated list of repos to show on the board, in display order.
  // Use just the repo name (not the full owner/name path). Private repos are
  // fine — they're read at build time with a server-side token, never the
  // browser. See README "Private repos".
  repos: [
    "assistant",
    "schub_friday",
    // "another-project",
  ],

  // Name of the tracker file each repo contains (at its root).
  trackerFile: "progress.md",

  // Live-fetch fallback only (public repos): how long to cache in the browser.
  cacheTTLMinutes: 15,
};

// Isomorphic export: <script> tag in the browser, require() in the build script.
if (typeof window !== "undefined") window.TRACKER_CONFIG = TRACKER_CONFIG;
if (typeof module !== "undefined" && module.exports) module.exports = TRACKER_CONFIG;
