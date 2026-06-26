// ─── Build Tracker ──────────────────────────────────────────────────────────
// Renders a dashboard + kanban + per-project detail. Primary data source is a
// baked data.json (produced by the GitHub Action — works for PRIVATE repos
// with no token in the browser). Falls back to the live GitHub API when there's
// no data.json, which only works for public repos.

const CFG = window.TRACKER_CONFIG || {};
const { normStatus, parseProgress, mergeProject } = window.TrackerParser;
const APP = document.getElementById("app");

const STATUS = {
  planned: { key: "planned", label: "Planned", css: "planned" },
  "in-progress": { key: "in-progress", label: "In Progress", css: "progress" },
  done: { key: "done", label: "Done", css: "done" },
};

// Restrained palette for language dots (Apple-ish accents).
const LANG_COLORS = {
  JavaScript: "#f1c40f", TypeScript: "#2997ff", Python: "#34c759",
  Swift: "#ff9f0a", HTML: "#ff6b5e", CSS: "#a06bff", Go: "#5ac8e8",
  Rust: "#d2855a", Java: "#e07a5f", Shell: "#86868b", Ruby: "#ff453a",
};

// ─── Utilities ───────────────────────────────────────────────────────────────
function timeAgo(iso) {
  const then = new Date(iso).getTime();
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${Math.floor(days / 365)}y ago`;
}

function esc(str) {
  const d = document.createElement("div");
  d.textContent = str == null ? "" : String(str);
  return d.innerHTML;
}

// ─── Data layer (with localStorage cache) ────────────────────────────────────
function cacheGet(key) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const { ts, data } = JSON.parse(raw);
    const ttl = (CFG.cacheTTLMinutes || 15) * 60000;
    if (Date.now() - ts > ttl) return null;
    return data;
  } catch { return null; }
}
function cacheSet(key, data) {
  try { localStorage.setItem(key, JSON.stringify({ ts: Date.now(), data })); } catch {}
}

async function fetchRepoList() {
  const key = `bt:repos:${CFG.username}`;
  const cached = cacheGet(key);
  if (cached) return cached;
  const res = await fetch(
    `https://api.github.com/users/${CFG.username}/repos?per_page=100&sort=updated`,
    { headers: { Accept: "application/vnd.github+json" } }
  );
  if (!res.ok) throw new Error(`GitHub API ${res.status}`);
  const list = await res.json();
  const slim = list.map((r) => ({
    name: r.name, description: r.description, language: r.language,
    stars: r.stargazers_count, url: r.html_url,
    pushed_at: r.pushed_at, default_branch: r.default_branch,
  }));
  cacheSet(key, slim);
  return slim;
}

async function fetchTracker(repo) {
  const url = `https://raw.githubusercontent.com/${CFG.username}/${repo.name}/${repo.default_branch}/${CFG.trackerFile}`;
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return parseProgress(await res.text());
  } catch { return null; }
}

// Build the merged model for all curated repos.
async function loadProjects() {
  const all = await fetchRepoList();
  const byName = Object.fromEntries(all.map((r) => [r.name.toLowerCase(), r]));
  const wanted = (CFG.repos && CFG.repos.length ? CFG.repos : all.map((r) => r.name));

  const projects = [];
  for (const name of wanted) {
    const repo = byName[name.toLowerCase()];
    if (!repo) continue; // skip names not found on the account
    const tracker = await fetchTracker(repo);
    projects.push(mergeProject(repo, tracker));
  }
  return projects;
}

// Baked data produced by the GitHub Action. Present in deployed/private setups.
async function fetchBakedData() {
  try {
    const res = await fetch(`data.json?t=${Date.now()}`, { cache: "no-store" });
    if (!res.ok) return null;
    const data = await res.json();
    return Array.isArray(data) && data.length ? data : null;
  } catch { return null; }
}

// ─── Rendering ───────────────────────────────────────────────────────────────
let STATE = { projects: [], filter: "all" };

function allFeatures(projects) {
  return projects.flatMap((p) => p.features.map((f) => ({ ...f, project: p.name })));
}

function langDot(lang) {
  if (!lang) return "";
  const c = LANG_COLORS[lang] || "#86868b";
  return `<span class="lang-dot" style="background:${c}"></span>`;
}

function renderDashboard(projects) {
  const feats = allFeatures(projects);
  const count = (s) => feats.filter((f) => f.status === s).length;
  const recent = [...projects].sort((a, b) => new Date(b.pushed_at) - new Date(a.pushed_at)).slice(0, 8);

  return `
    <h1 class="page-title">${esc(CFG.ownerName || "My")}'s Builds</h1>
    <p class="page-subtitle">Live progress across ${projects.length} project${projects.length === 1 ? "" : "s"}, straight from GitHub.</p>

    <div class="stats">
      <a class="stat-card" href="#/" style="text-decoration:none">
        <div class="stat-num">${count("done")}</div>
        <div class="stat-label"><span class="dot done"></span>Shipped</div>
      </a>
      <div class="stat-card">
        <div class="stat-num">${count("in-progress")}</div>
        <div class="stat-label"><span class="dot progress"></span>In Progress</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">${count("planned")}</div>
        <div class="stat-label"><span class="dot planned"></span>Planned</div>
      </div>
    </div>

    <section class="section">
      <div class="section-title">Recent Activity</div>
      <div class="activity-strip">
        ${recent.map((p) => `
          <a class="activity-card" href="#/project/${encodeURIComponent(p.name)}">
            <div class="activity-name">${esc(p.title)}</div>
            <div class="activity-meta">
              ${p.language ? `<span>${langDot(p.language)}${esc(p.language)}</span>` : ""}
              ${p.stars ? `<span>★ ${p.stars}</span>` : ""}
              <span>${p.pushed_at ? timeAgo(p.pushed_at) : ""}</span>
            </div>
          </a>`).join("")}
      </div>
    </section>`;
}

function renderFilters(projects) {
  const chips = ["all", ...projects.filter((p) => p.hasTracker).map((p) => p.name)];
  return `
    <section class="section">
      <div class="section-title">Board</div>
      <div class="filters">
        ${chips.map((c) => `
          <button class="chip ${STATE.filter === c ? "active" : ""}" data-filter="${esc(c)}">
            ${c === "all" ? "All projects" : esc(c)}
          </button>`).join("")}
      </div>
    </section>`;
}

function cardHTML(f) {
  const css = STATUS[f.status].css;
  return `
    <a class="card" href="#/project/${encodeURIComponent(f.project)}">
      <div class="status-bar ${css}"></div>
      <div class="card-title">${esc(f.name)}</div>
      ${f.note ? `<div class="card-note">${esc(f.note)}</div>` : ""}
      <div class="card-foot"><span class="card-proj">${esc(f.project)}</span></div>
    </a>`;
}

function renderKanban(projects) {
  let feats = allFeatures(projects);
  if (STATE.filter !== "all") feats = feats.filter((f) => f.project === STATE.filter);

  const col = (statusKey, title) => {
    const items = feats.filter((f) => f.status === statusKey);
    const css = STATUS[statusKey].css;
    return `
      <div class="column">
        <div class="column-head">
          <div class="column-title"><span class="dot ${css}"></span>${title}</div>
          <span class="column-count">${items.length}</span>
        </div>
        <div class="cards">
          ${items.length ? items.map(cardHTML).join("") : `<div class="empty-col">Nothing here yet</div>`}
        </div>
      </div>`;
  };

  return `<div class="kanban">
    ${col("planned", "Planned")}
    ${col("in-progress", "In Progress")}
    ${col("done", "Done")}
  </div>`;
}

function renderHome() {
  const p = STATE.projects;
  const noTracked = p.every((x) => !x.hasTracker);
  APP.innerHTML = `<div class="fade-in">
    ${renderDashboard(p)}
    ${noTracked ? `<div class="banner">No <code>${esc(CFG.trackerFile)}</code> found in your repos yet. Add one (see the sample) and the board fills in automatically.</div>` : ""}
    ${renderFilters(p)}
    ${renderKanban(p)}
  </div>`;

  APP.querySelectorAll(".chip").forEach((btn) =>
    btn.addEventListener("click", () => { STATE.filter = btn.dataset.filter; renderHome(); })
  );
}

function renderProject(name) {
  const p = STATE.projects.find((x) => x.name.toLowerCase() === name.toLowerCase());
  if (!p) { location.hash = "#/"; return; }

  const total = p.features.length;
  const done = p.features.filter((f) => f.status === "done").length;
  const pct = total ? Math.round((done / total) * 100) : 0;

  APP.innerHTML = `<div class="fade-in">
    <a class="back-link" href="#/">← All projects</a>
    <h1 class="page-title">${esc(p.title)}</h1>
    ${p.description ? `<p class="page-subtitle">${esc(p.description)}</p>` : ""}
    <div class="detail-meta">
      ${p.language ? `<span>${langDot(p.language)}${esc(p.language)}</span>` : ""}
      ${p.stars ? `<span>★ ${p.stars} stars</span>` : ""}
      ${p.pushed_at ? `<span>Updated ${timeAgo(p.pushed_at)}</span>` : ""}
      <span><a style="color:var(--accent)" href="${esc(p.url)}" target="_blank" rel="noopener">View on GitHub ↗</a></span>
    </div>

    ${p.stack.length ? `<div class="stack">${p.stack.map((s) => `<span class="stack-chip">${esc(s)}</span>`).join("")}</div>` : ""}

    ${p.hasTracker ? `
      <div class="progress-wrap">
        <div class="progress-head"><span>${done} of ${total} features shipped</span><span>${pct}%</span></div>
        <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
      </div>
      <div class="checklist">
        ${p.features.map((f) => {
          const css = STATUS[f.status].css;
          const mark = f.status === "done" ? "✓" : f.status === "in-progress" ? "•" : "";
          return `
            <div class="check-item ${css}">
              <div class="check-mark ${css}">${mark}</div>
              <div class="check-body">
                <div class="check-name">${esc(f.name)}</div>
                ${f.note ? `<div class="check-note">${esc(f.note)}</div>` : ""}
              </div>
              <span class="check-status ${css}">${STATUS[f.status].label}</span>
            </div>`;
        }).join("")}
      </div>`
    : `<div class="no-tracker">This project doesn't have a <code>${esc(CFG.trackerFile)}</code> file yet, so there's no feature breakdown. Add one to its repo root to track progress here.</div>`}
  </div>`;
}

// ─── Router ──────────────────────────────────────────────────────────────────
function route() {
  const hash = location.hash || "#/";
  const m = hash.match(/^#\/project\/(.+)$/);
  if (m) renderProject(decodeURIComponent(m[1]));
  else renderHome();
}

// ─── Theme ───────────────────────────────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem("bt:theme");
  const prefersDark = matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = saved || (prefersDark ? "dark" : "light");
  document.documentElement.dataset.theme = theme;
  updateThemeIcon(theme);
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("bt:theme", next);
    updateThemeIcon(next);
  });
}
function updateThemeIcon(theme) {
  document.querySelector(".theme-icon").textContent = theme === "dark" ? "☀" : "☾";
}

// ─── Boot ────────────────────────────────────────────────────────────────────
async function boot() {
  initTheme();
  document.getElementById("brand-name").textContent = `${CFG.ownerName || "My"} · Builds`;
  const gh = document.getElementById("github-link");
  gh.href = `https://github.com/${CFG.username}`;

  if (!CFG.username || CFG.username === "your-github-username") {
    APP.innerHTML = `<div class="banner" style="margin-top:48px">
      Set your GitHub username in <code>config.js</code> to load the board.
    </div>`;
    return;
  }

  // 1) Prefer baked data.json (works for private repos, no token in browser).
  const baked = await fetchBakedData();
  if (baked) {
    STATE.projects = baked;
    window.addEventListener("hashchange", route);
    route();
    return;
  }

  // 2) Fall back to the live GitHub API (public repos only).
  try {
    STATE.projects = await loadProjects();
    window.addEventListener("hashchange", route);
    route();
  } catch (err) {
    APP.innerHTML = `<div class="banner" style="margin-top:48px">
      No <code>data.json</code> yet, and the live GitHub API returned: ${esc(err.message)}.
      ${String(err.message).includes("404") || String(err.message).includes("403")
        ? "If your repos are private, run the GitHub Action (or <code>node build/fetch.cjs</code>) to bake <code>data.json</code> — see the README."
        : "Check the username in <code>config.js</code>."}
    </div>`;
  }
}

boot();
