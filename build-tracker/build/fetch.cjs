#!/usr/bin/env node
// ─── Bake data.json from GitHub (server-side) ─────────────────────────────────
// Reads each curated repo's metadata + progress.md using an authenticated token,
// then writes build-tracker/data.json. The token stays here (CI secret / local
// env) and never reaches the browser, so PRIVATE repos work and stay private.
//
// Usage:  GITHUB_TOKEN=ghp_xxx node build/fetch.cjs
// Token needs read-only "Contents" + "Metadata" on the listed repos.

const fs = require("fs");
const path = require("path");
const CFG = require("../config.js");
const { parseProgress, mergeProject } = require("../parser.js");

const TOKEN = process.env.TRACKER_TOKEN || process.env.GITHUB_TOKEN || "";
const OUT = path.join(__dirname, "..", "data.json");

if (!TOKEN) {
  console.error("✗ No token. Set TRACKER_TOKEN or GITHUB_TOKEN.");
  process.exit(1);
}

const gh = async (url) => {
  const res = await fetch(url, {
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "build-tracker",
    },
  });
  return res;
};

async function fetchRepo(name) {
  const metaRes = await gh(`https://api.github.com/repos/${CFG.username}/${name}`);
  if (!metaRes.ok) {
    console.warn(`  ! ${name}: repo fetch ${metaRes.status} — skipped`);
    return null;
  }
  const r = await metaRes.json();
  const repo = {
    name: r.name,
    description: r.description,
    language: r.language,
    stars: r.stargazers_count,
    url: r.html_url,
    pushed_at: r.pushed_at,
  };

  // progress.md via the contents API (base64). 404 = no tracker file yet.
  let tracker = null;
  const fileRes = await gh(
    `https://api.github.com/repos/${CFG.username}/${name}/contents/${CFG.trackerFile}`
  );
  if (fileRes.ok) {
    const body = await fileRes.json();
    const text = Buffer.from(body.content, body.encoding || "base64").toString("utf8");
    tracker = parseProgress(text);
  } else if (fileRes.status !== 404) {
    console.warn(`  ! ${name}: ${CFG.trackerFile} fetch ${fileRes.status}`);
  }

  console.log(`  ✓ ${name}${tracker ? ` (${tracker.features.length} features)` : " (no tracker)"}`);
  return mergeProject(repo, tracker);
}

(async () => {
  console.log(`Baking data.json for ${CFG.username} (${CFG.repos.length} repos)…`);
  const projects = [];
  for (const name of CFG.repos) {
    const p = await fetchRepo(name);
    if (p) projects.push(p);
  }
  fs.writeFileSync(OUT, JSON.stringify(projects, null, 2) + "\n");
  console.log(`Wrote ${OUT} (${projects.length} projects).`);
})().catch((err) => {
  console.error("✗ Build failed:", err.message);
  process.exit(1);
});
