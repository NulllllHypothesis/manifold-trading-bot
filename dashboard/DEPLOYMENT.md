# Dashboard Deployment Plan

## Phase 1 — Local development (NOW)

**Where it runs:** your laptop, `localhost:3000`
**Where the data lives:** the sandbox (or a local clone for testing)
**Who can see it:** only you

### Setup

```bash
cd dashboard
pnpm install
pnpm dev
```

The dashboard reads the bot's data files directly via Node `fs`. By default it
looks in the parent directory (`../`). Override via env var if needed:

```bash
WORKSPACE_ROOT=/absolute/path/to/manifold-bot-workspace pnpm dev
```

### Reading data from the sandbox

Prebuilt scripts do this for you — see [scripts/README.md](scripts/README.md)
for full details.

```bash
pnpm check          # diagnose the full connection pipeline (read-only)
pnpm sync           # one-shot pull from sandbox to /tmp/manifold-sandbox-snapshot
pnpm dev:sandbox    # sync, then start dev server against the snapshot
pnpm sync:watch     # re-sync every 60 s (run in a second terminal for live mode)
```

First time, you need to set up auth to the sandbox:

```bash
ssh-copy-id hackathon-server         # preferred (one-time password entry)
# OR
echo 'SANDBOX_PASS=…' >> ../.env     # fallback, sshpass-based
```

Run `pnpm check` to verify. It reports TCP reachability, auth path, remote file
listing, and the sandbox's running bot processes.

### Why we're NOT running it on the sandbox

- Sandbox CPU is already pressured (Ollama timeout rate is direct evidence).
  Adding a Node process competing for cores would worsen the AI reliability problem.
- Exposing HTTP on someone else's home network has security implications we'd
  rather not litigate.
- This phase is for development; "deploying" it is overkill.

## Phase 2 — Vercel + sandbox API shim (LATER)

**When to do this:** when the dashboard is good enough that you want to share it
with someone outside your Tailscale network (a friend, a potential investor, a
co-founder).

**Architecture:**

```
[any browser] ──HTTPS──> [Vercel: Next.js dashboard]
                              │
                              └─fetch──> [Tailscale Funnel HTTPS endpoint]
                                              │
                                              └─> [sandbox: dashboard_api/ FastAPI]
                                                       │
                                                       └─> reads JSON/SQLite locally
```

**Tasks:**

1. Build out `dashboard_api/` (currently a stub) — see its README for design.
2. Run it on the sandbox under tmux or systemd-user, bound to Tailscale IP only,
   bearer-token auth from `DASHBOARD_API_TOKEN`.
3. Expose via Tailscale Funnel (`tailscale funnel 8000` on the sandbox) for
   HTTPS without opening any port on the friend's home router. Confirm with
   the sandbox owner before flipping the funnel on.
4. Switch `dashboard/src/lib/data/*` adapters from `fs.readFile` to `fetch()`
   against the API. Components don't change.
5. Deploy to Vercel: `cd dashboard && vercel`. Set env vars on Vercel:
   - `API_BASE_URL=https://<sandbox-funnel-host>`
   - `DASHBOARD_API_TOKEN=<same as on sandbox>`

**Why not now:** maintaining two copies of every data-access function
(Python + TypeScript) before either is needed is wasted work.

## Phase 3 — Migrate bot to a dedicated VPS (LATER STILL)

**When to do this:** the moment the dashboard becomes a thing other humans see
regularly, or the moment the sandbox owner says they want their resources back.

**Architecture:**

```
[any browser] ──HTTPS──> [Vercel: Next.js dashboard]
                              │
                              └─fetch──> [Hetzner CX22 ($4.50/mo): bot + API]
                                              │
                                              └─> reads JSON/SQLite locally
                                              └─> Manifold + Ollama + cron
```

Sandbox goes back to being just a sandbox. Migration cost: ~1 day to copy the
SQLite DBs, point cron jobs at the new host, and update DNS/Tailscale.

**Why this is the eventual right answer:**
- ~$5/mo is nothing once this is even half-serious.
- Dedicated hardware fixes the Ollama timeout problem (we'd actually have CPU
  to spare for the model).
- Clean separation of dev/prod.
- Foundation for Phase 4 (multi-tenant SaaS) if we ever go there.

## Phase 4 — Multi-tenant SaaS (IF the project becomes a product)

**When to do this:** if and only if real users want to plug in their own bots.

**Architecture changes:**

- `BOT_ID` becomes per-user (already plumbed through `lib/config.ts` + every
  data adapter — that's the whole point of the `botId` parameter).
- Postgres replaces JSON/SQLite (Supabase or Neon, free tier).
- Auth.js (already installed, no-op) gets turned on with GitHub OAuth.
- Bot-runner becomes a per-user container (Fly.io machines, Railway, or
  custom k8s — whichever is cheapest at the time).

**Why we don't need to think about this in detail yet:** the seam is in.
When the time comes, the codebase won't fight us.
