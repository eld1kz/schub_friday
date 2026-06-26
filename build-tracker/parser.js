// ─── Shared progress.md parser + project merge ───────────────────────────────
// Isomorphic: loaded as a <script> in the browser (exposes window.TrackerParser)
// AND require()'d by the Node build script (build/fetch.cjs). Wrapped in an IIFE
// so the helper functions don't leak into the shared global scope of classic
// <script> tags (which would collide with app.js's `const { parseProgress }`).

(function (root, factory) {
  const api = factory();
  if (typeof window !== "undefined") root.TrackerParser = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function normStatus(raw) {
    const s = (raw || "").trim().toLowerCase();
    if (["done", "complete", "completed", "shipped", "x"].includes(s)) return "done";
    if (["in-progress", "in progress", "wip", "doing", "active"].includes(s)) return "in-progress";
    return "planned";
  }

  // Frontmatter (--- key: value ---) + status-tagged checklist:
  //   - [done] Feature name — optional note
  function parseProgress(text) {
    const meta = {};
    const features = [];
    let body = text;

    const fm = text.match(/^---\s*\n([\s\S]*?)\n---\s*\n?/);
    if (fm) {
      fm[1].split("\n").forEach((line) => {
        const m = line.match(/^([A-Za-z_]+)\s*:\s*(.+)$/);
        if (m) meta[m[1].trim().toLowerCase()] = m[2].trim();
      });
      body = text.slice(fm[0].length);
    }

    body.split("\n").forEach((line) => {
      const m = line.match(/^\s*-\s*\[([^\]]*)\]\s*(.+)$/);
      if (!m) return;
      const status = normStatus(m[1]);
      let name = m[2].trim();
      let note = "";
      const split = name.split(/\s+[—–-]\s+/); // em/en dash or hyphen separator
      if (split.length > 1) {
        name = split[0].trim();
        note = split.slice(1).join(" — ").trim();
      }
      features.push({ name, note, status });
    });

    const stack = meta.stack ? meta.stack.split(",").map((s) => s.trim()).filter(Boolean) : [];
    return { title: meta.title || "", description: meta.description || "", stack, features };
  }

  // Merge repo metadata + (optional) parsed tracker into the project shape the
  // UI renders. `repo` = { name, description, language, stars, url, pushed_at }.
  function mergeProject(repo, tracker) {
    return {
      name: repo.name,
      title: (tracker && tracker.title) || repo.name,
      description: (tracker && tracker.description) || repo.description || "",
      stack: tracker && tracker.stack.length ? tracker.stack : (repo.language ? [repo.language] : []),
      features: tracker ? tracker.features : [],
      hasTracker: !!tracker,
      language: repo.language,
      stars: repo.stars,
      url: repo.url,
      pushed_at: repo.pushed_at,
    };
  }

  return { normStatus, parseProgress, mergeProject };
});
