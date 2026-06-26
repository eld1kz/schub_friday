# Build Tracker

An at-a-glance dashboard for all my projects — dashboard summary, a Kanban
board (Planned → In Progress → Done), and per-project detail with a feature
checklist. **GitHub is the source of truth:** each repo carries a small
`progress.md` file, and the board is built from those files.

Apple-style design — clean, minimal, bold typography, restrained color,
dark mode, fully responsive.

## How data flows

A GitHub Action reads each repo's `progress.md` + metadata and bakes a static
`build-tracker/data.json`. The page loads `data.json` — **no GitHub token ever
ships to the browser**, so private repos work and stay private. The only thing
that becomes public is whatever you put in `progress.md`.

```
GitHub Action (holds the token as a secret)
   ├─ reads private repos' progress.md + metadata via API
   ├─ writes build-tracker/data.json  ── committed back to the repo
   └─ runs on push + every 6h + manual
                  │
   Static page loads data.json  ← no token in the browser; repos stay private
```

(If you only ever track **public** repos, you can skip the Action entirely: the
page falls back to live, unauthenticated GitHub API calls when there's no
`data.json`.)

## Setup

1. Edit **`config.js`** — `username`, `ownerName`, and the curated `repos` list.
2. Drop a **`progress.md`** in each repo's root (copy `progress.sample.md`).
3. **Private repos** — create the token + secret (one time):
   - GitHub → Settings → Developer settings → **Fine-grained tokens** → generate
     one with **read-only** access to the repos in `config.js`:
     Repository permissions → **Contents: Read** and **Metadata: Read**.
   - In the repo hosting this site: Settings → Secrets and variables → Actions →
     New repository secret, name **`TRACKER_TOKEN`**, paste the token.
   - The workflow (`.github/workflows/build-tracker.yml`) runs on push / every 6h
     / manual (Actions tab → "Run workflow") and re-bakes `data.json`.

### Generate `data.json` locally (optional)

```bash
cd build-tracker
TRACKER_TOKEN=your_token node build/fetch.cjs   # writes data.json
python3 -m http.server 8000                     # open http://localhost:8000
```

## The `progress.md` format

```markdown
---
title: My Project
description: One-line description.
stack: Python, FastAPI, Docker
---

- [done] Feature name — optional note
- [in-progress] Another feature
- [planned] Something later
```

- **Status** is the bracket tag: `[done]`, `[in-progress]`, `[planned]`
  (aliases like `wip`, `x`, `shipped` are understood).
- Text after ` — ` becomes the card's note.
- Frontmatter is optional; falls back to the repo's name / description / language.
- Repos without a `progress.md` still show in recent activity — they're just
  left off the board.

## Deploy

**Vercel (recommended for private repos)** — import the repo, set the project
root to `build-tracker/`, no build command, output directory `.`. Vercel serves
the static files (including the committed `data.json`) and redeploys whenever the
Action commits a refresh. Works with private repos on the free tier.

**GitHub Pages** — point Pages at the `build-tracker/` folder. Note: publishing
Pages from a *private* repo requires a paid GitHub plan; otherwise use Vercel.

## Files

| File | Role |
|------|------|
| `index.html` / `styles.css` | Page + Apple-style design system |
| `config.js` | Your username + curated repo list (isomorphic: browser + Node) |
| `parser.js` | Shared `progress.md` parser + project merge (browser + Node) |
| `app.js` | Loads `data.json` (or live API), renders dashboard/kanban/detail |
| `build/fetch.cjs` | Server-side baker → writes `data.json` |
| `data.json` | Baked board data (committed; refreshed by the Action) |
| `progress.sample.md` | Template to copy into a repo |
