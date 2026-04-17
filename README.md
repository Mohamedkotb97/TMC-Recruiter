# TMC Recruiter CRM

Capture LinkedIn conversations & profiles, auto-brief each thread with Claude,
auto-enrich profiles via Apify, and run the whole recruiting flow (Inbox, Pools,
Roles, Templates, Kanban) from one dashboard.

```
  LinkedIn DM  ──click "Save"──►  Chrome extension  ──POST bulk──►  FastAPI
                                                                      │
                                                      ┌───────────────┤
                                                      ▼               ▼
                                                Postgres/SQLite  Background worker
                                                                      │
                                                         ┌────────────┤
                                                         ▼            ▼
                                                      Apify        Claude
                                                   (profile)    (brief + fit)
```

Each saved thread triggers background enrichment + AI analysis — the recruiter
keeps syncing the next thread while briefs fill in asynchronously.

---

## Running locally

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# Required
export ANTHROPIC_API_KEY=sk-ant-...
export CRM_API_KEY=dev-key-change-me      # admin fallback only (not for the extension anymore)
# Optional
export DEFAULT_ADMIN_PASSWORD=tmc-admin   # first-run password for user "habib"
export DATABASE_URL=sqlite:///./recruiter.db  # default

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open http://localhost:8000 → log in (`habib` / `tmc-admin`).

Integration keys (Apify, Unipile, Greenhouse) go in **Settings** inside the app.

---

## Install the Chrome extension

1. Chrome → `chrome://extensions` → toggle **Developer mode**.
2. Click **Load unpacked** → select the `extension/` folder.
3. Click the extension icon:
   - Set **Backend URL** to your dashboard origin (e.g. `https://recruiter.yourcompany.com` or `http://localhost:8000` for dev).
   - Click **Open dashboard → Settings**, sign in, copy the **🔑 Chrome Extension — your personal key** (starts with `tmc_…`).
   - Paste it into the popup's **Your personal key** field and click **Save**. You should see `✓ Connected as <your name>`.
4. Go to `linkedin.com/messaging/`. Three floating buttons appear:
   - **Save current** — saves the open thread
   - **Save last 5** — saves the 5 newest conversations in the sidebar
   - **Sync ALL** — lazy-loads the entire sidebar, saves every thread

Every button uses the same `/api/conversations/bulk` endpoint — deduped by
thread URL, so re-syncing never creates duplicates. The extension can push
~100 conversations in under 5 minutes (with the new 900 ms history-scroll
waits it's more thorough, not faster).

Each recruiter on the team uses their OWN `tmc_…` key, so every saved
thread is attributed to them and shows up only in their Inbox.

When the upload completes, the server queues each new/updated thread into a
background worker pool. In the dashboard you'll see:
  - An **analysis status chip** at the top of the Inbox (`N analyzing · M pending`)
  - A status badge on each conversation card (`queued for brief` → `⏳ analyzing…` → done)
  - A **↻ Re-brief** button on every conversation to re-run manually

---

## Production deployment

The stack this repo is tuned for: **Render (or Railway / Fly) for the API +
dashboard, Supabase for the Postgres database, Chrome Web Store for the
extension.** All three have generous free tiers; total cost at small
volume is **$0/month**.

### 1. Postgres on Supabase (free)

1. Create a project at <https://supabase.com>. Region: pick one close to your API host.
2. Project Settings → **Database** → scroll to **Connection string** → **URI** tab → **Transaction pooler** (port `6543`).
3. Copy the URI, insert your DB password, and prepend `+psycopg` to the driver:

   ```
   postgresql+psycopg://postgres.<proj>:PASSWORD@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```

4. That's your `DATABASE_URL`. First backend boot creates every table +
   index + migration automatically (see `models.init_db`).

> ℹ️ Use the **pooler** (6543) URL, not the direct connection (5432). The
> pooler survives Supabase's idle-connection kills that would otherwise
> crash long-lived workers.

### 2. Backend + dashboard on Render (free web service)

The repo ships a production-ready `Dockerfile` — Render auto-detects it.

1. Push this repo to GitHub.
2. <https://dashboard.render.com> → **New → Web Service** → pick the repo.
3. Environment: **Docker**. Instance type: **Free** is fine to start.
4. Add env vars (Settings → Environment):

   | Key                    | Value                                                             |
   | ---------------------- | ----------------------------------------------------------------- |
   | `ANTHROPIC_API_KEY`    | `sk-ant-…`                                                        |
   | `CRM_API_KEY`          | any long random string (admin fallback, not for the extension)    |
   | `DATABASE_URL`         | the Supabase URI from step 1                                      |
   | `DEFAULT_ADMIN_PASSWORD` | first-boot password for `habib`                                 |
   | `ALLOWED_ORIGINS`      | your final dashboard URL, e.g. `https://recruiter.onrender.com`   |
   | `WEB_CONCURRENCY`      | `2` (raise to match CPU count on paid plans)                      |

5. Deploy. Render gives you `https://recruiter-<hash>.onrender.com`. Open it, log in as `habib` / the password you set, go to **Settings** and change the password immediately.

Railway / Fly / Docker-Compose / bare-metal all work the same way — the
Dockerfile is portable.

### 3. (Optional) Custom domain

Point `recruiter.yourcompany.com` at Render (CNAME → `recruiter-<hash>.onrender.com`),
then update `ALLOWED_ORIGINS` to the custom URL and redeploy.

### 4. Ship the Chrome extension to the team

1. `extension/manifest.json` already sets the version. Bump it whenever you change `content.js`.
2. Each recruiter installs it (Load unpacked in dev, or publish via Chrome Web Store).
3. They each log into the dashboard, copy their personal `tmc_…` key from **Settings → Chrome Extension**, paste into the popup, set **Backend URL** to the public dashboard URL.

No hard-coded URL in `content.js` — the backend URL lives in `chrome.storage.local`
per install, so the same build works in dev and prod.

### 5. Secrets + settings table

| Variable                  | What                                                         |
| ------------------------- | ------------------------------------------------------------ |
| `ANTHROPIC_API_KEY`       | Claude for briefs + reply drafts                             |
| `CRM_API_KEY`             | Admin fallback only (e.g. `/api/admin/purge`). The extension does **not** use this anymore — each recruiter has a per-user `tmc_…` key. |
| `DATABASE_URL`            | Supabase Postgres URI in prod, SQLite in dev                 |
| `DEFAULT_ADMIN_PASSWORD`  | First-run password for `habib` (only used on empty DB)       |
| `ALLOWED_ORIGINS`         | Comma-separated list of dashboard origins (default `*`)      |
| `DISABLE_BG_ANALYSIS`     | `1` on serverless hosts — switches the worker to on-demand   |
| `WEB_CONCURRENCY`         | Uvicorn workers (Docker default 2)                           |
| `PORT`                    | Server port — most PaaS set this automatically               |

Apify / Unipile / Greenhouse / Lever keys are stored **in the database via
the Settings UI**, not env vars — so each deployment can connect its own
tenant of each tool.

### 6. PaaS compatibility matrix

| Host         | Fit           | Notes                                                            |
| ------------ | ------------- | ---------------------------------------------------------------- |
| Render       | ✅ Recommended | Docker auto-detect, free Postgres alternative to Supabase, free TLS |
| Railway      | ✅ Recommended | Same story, slightly cheaper on scale                            |
| Fly.io       | ✅ Great      | Global regions, Docker-first                                      |
| Supabase     | ✅ For DB only | Use its Postgres, NOT its edge functions for this FastAPI        |
| Vercel       | ⚠️ Limited   | Python runtime works, but background worker can't persist. Set `DISABLE_BG_ANALYSIS=1` and accept on-demand briefs. Best used only for a future marketing page, not this API. |
| AWS Lambda   | ⚠️ Limited   | Same constraints as Vercel serverless                            |

### 7. Backing up

Supabase has point-in-time recovery on paid plans. On free plans, schedule a
weekly `pg_dump` to your own storage — everything critical is in the DB
(users, conversations, candidates, settings).

### 8. CORS notes

By default (`ALLOWED_ORIGINS=*`), any website can hit your API if they
happen to have a valid key. Since keys are strongly random (`tmc_` +
32 URL-safe bytes) this is fine for most deployments, but tightening it
to your dashboard origin is a one-line env var change.

The Chrome extension origin (`chrome-extension://<id>`) is **always**
allowed via regex — no matter what you set `ALLOWED_ORIGINS` to — because
the install id differs per user and is unstable.

---

## Publishing the Chrome extension

1. Bump the version in `extension/manifest.json`.
2. Change the hard-coded `BACKEND_URL` in `extension/content.js` to your
   public API (https only).
3. Zip the `extension/` folder (NOT a parent folder containing it).
4. Chrome Web Store dashboard → **New item** → upload the zip.
5. Fill in: description, privacy policy URL, icons (128×128 / 48×48 / 16×16),
   single screenshot, justifications for `host_permissions` (LinkedIn) and
   `storage` (API key).
6. Submit for review — usually 1-3 business days.

During review, Google will test that the extension does nothing without an
explicit click by the user — it already passes because all capture happens
in the button click handlers.

---

## Multi-user

- First boot seeds the admin user **habib** (`DEFAULT_ADMIN_PASSWORD`).
- Admins see an **Admin** nav item with:
  - User list (all, not just active)
  - Per-user usage (candidates + threads owned)
  - Buttons: Approve / Suspend / Promote / Demote / Reset password
  - Toggles: `Allow self-registration`, `Auto-approve new accounts`
- Self-registration endpoint: `POST /api/auth/register` (disabled by default).
  When on, new accounts are created but `is_active=False` until an admin
  clicks **Approve** — unless `auto_approve_signups` is also on.
- Each conversation/candidate stores `owner_user_id`. The dashboard filters
  every list by owner:
  - A candidate shows up in your Inbox if you own the candidate row OR you
    own at least one conversation on it (so shared LinkedIn profiles stay
    visible to whoever actually talked to them).
  - Admins see every user's data.
- On every boot, orphan candidates (owner_user_id NULL) are auto-claimed
  from any conversation that already has an owner — protects legacy rows
  from the pre-multi-user days.

---

## File layout

```
recruiter_app/
├── backend/
│   ├── main.py            # FastAPI routes + background worker
│   ├── models.py          # SQLAlchemy models; auto Postgres/SQLite
│   ├── ai_service.py      # Claude + Apify + Unipile helpers
│   ├── requirements.txt
│   └── static/
│       └── index.html     # Single-file SPA
├── extension/
│   ├── manifest.json
│   ├── content.js         # 3 save buttons + bulk upload
│   ├── content.css
│   ├── popup.html
│   └── popup.js
├── Dockerfile
└── README.md
```

---

## Security notes

- Everything runs on your own infrastructure; only outbound calls go to
  Claude (AI briefs), Apify (profile enrichment), and optionally Unipile
  (messaging). LinkedIn cookies never leave the user's browser.
- The Postgres/SQLite database is the single source of truth. Back it up.
- The Chrome extension stores only the recruiter's per-user `tmc_…` key
  (plus the backend URL) in `chrome.storage.local` and sends it as
  `X-API-Key` to your own backend. Rotate it from Settings → Regenerate at
  any time — the old key stops working immediately.
- The global `CRM_API_KEY` env var is NO LONGER accepted by the extension
  endpoints (save_conversation, bulk_save_conversations, save_candidate).
  It is only used for admin tooling (purge, user admin). If a legacy copy
  of the extension still sends it, every save will 401 with a clear
  message — upgrade the install to v1.3.0+.
