# Deployment

Production topology: Next.js on Vercel, FastAPI plus the review worker in one container on Render (free plan, Docker), Postgres on Neon, Redis on Upstash (dispatch doorbell only; Postgres holds job state).

Heads up before starting: steps 3 to 5 are circular. Render needs the Vercel URL (`FRONTEND_ORIGIN`), Vercel needs the Render URL (`API_ORIGIN`), and the GitHub OAuth app needs the Vercel URL for its callback. The order below untangles it: deploy Render first with a placeholder `FRONTEND_ORIGIN` (the Render URL is predictable from the service name), then Vercel, then fill in the real values and create the OAuth app last.

## 1. Neon (Postgres)

1. Create a project at console.neon.tech: free tier, name `difflens`, Postgres 16.
2. Copy the **pooled** connection string (the default one; the host contains `-pooler`).
3. Change the scheme from `postgresql://` to `postgresql+psycopg://` so SQLAlchemy picks the psycopg3 driver:

   ```
   postgresql+psycopg://USER:PASS@ep-xxx-pooler.us-east-2.aws.neon.tech/difflens?sslmode=require
   ```

That full string is `DATABASE_URL` in step 2. Alembic runs inside the Render start command against this same pooled URL. If DDL ever misbehaves under pgbouncer (rare, but transaction pooling can break some statements), the fallback is: run `alembic upgrade head` locally against Neon's **direct** (unpooled) connection string, then redeploy.

## 2. Render (API)

1. Dashboard: New > Blueprint, point it at this repo. `render.yaml` drives everything: one Docker web service named `difflens-api` on the free plan, health check on `/health`, auto-deploy on push.
2. Open the service's Environment tab and fill the `sync: false` variables:

   | Variable | Where it comes from |
   | --- | --- |
   | `DATABASE_URL` | Neon pooled string from step 1, scheme edited to `postgresql+psycopg://` |
   | `GITHUB_CLIENT_ID` | production OAuth app (step 3): use `placeholder` for now |
   | `GITHUB_CLIENT_SECRET` | same OAuth app: `placeholder` for now |
   | `SESSION_SECRET` | generated fresh, first command below |
   | `TOKEN_ENCRYPTION_KEY` | generated fresh, second command below |
   | `FRONTEND_ORIGIN` | the Vercel URL (step 4): use `https://placeholder.invalid` for now |
   | `REDIS_URL` | Upstash `rediss://` string (step 6): `redis://placeholder:6379/0` until then; the worker then falls back to polling Postgres every 25s, so reviews still run |
   | `GEMINI_API_KEY` | aistudio.google.com/apikey (free tier). Only read when `AI_PROVIDER=gemini` |
   | `ANTHROPIC_API_KEY` | console.anthropic.com > API keys. Only read when `AI_PROVIDER=anthropic` |
   | `OPENAI_API_KEY` | platform.openai.com > API keys. Only read when `AI_PROVIDER=openai` |

   `AI_PROVIDER` and `AI_MODEL` ship in render.yaml with visible values (`mock`, empty). Mock mode runs the whole AI pipeline with zero API calls and zero AI findings, so the platform works end to end before any key exists. The zero-cost way to real reviews: create a free key at aistudio.google.com/apikey, set `GEMINI_API_KEY`, flip `AI_PROVIDER` to `gemini`, save (redeploys). The paid alternatives are `anthropic` + `ANTHROPIC_API_KEY` (claude-opus-5, $5/$25 per 1M tokens) and `openai` + `OPENAI_API_KEY` (gpt-5.6-terra, $1/$6 per 1M; gpt-5.6-luna is cheaper still and gpt-5.6-sol stronger). `AI_MODEL` left empty means the provider default (`gemini-3.6-flash` / `claude-opus-5` / `gpt-5.6-terra`). Independently of all this, signed-in users can store their own key under Settings in the web app; reviews they start use their key instead of the server's, encrypted at rest with `TOKEN_ENCRYPTION_KEY`.

3. Generate fresh production secrets. Do **not** reuse the dev values from `.env`. From the repo root (PowerShell):

   ```
   & api\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
   & api\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   First output is `SESSION_SECRET`, second is `TOKEN_ENCRYPTION_KEY`.

4. Deploy. The service boots, runs migrations, and passes `/health` even with the OAuth placeholders: login just does not work yet.

## 3. Production GitHub OAuth app

One OAuth app can serve both environments: register the **production** callback, and local dev keeps working through GitHub's loopback exception (`localhost` redirect URIs are always accepted for OAuth apps, regardless of the registered callback). Point the existing app at production, or create a second app if you prefer strict separation; both work.

- Application name: `DiffLens`
- Homepage URL: `https://<vercel-app>.vercel.app`
- Callback URL: `https://<vercel-app>.vercel.app/api/backend/auth/github/callback`

You do not know `<vercel-app>` until step 4, so set the callback after step 4 (it is why the OAuth app is last in the order). Generate a client secret (an app can hold two active secrets, so dev and prod can each use their own) and put the client ID and secret into the Render Environment tab, replacing the placeholders.

## 4. Vercel (web)

1. vercel.com > Add New > Project > import the repo.
2. Root Directory: `web`. Framework preset autodetects Next.js; keep the default build settings.
3. Environment variable: `API_ORIGIN` = `https://difflens-api.onrender.com` (the Render URL is deterministic: the service is named `difflens-api` in `render.yaml`).
4. Deploy and note the production URL, e.g. `https://difflens.vercel.app`.

## 5. Close the loop

Back on Render, set `FRONTEND_ORIGIN` to the exact Vercel URL from step 4 (no trailing slash). The API uses it to build the OAuth `redirect_uri` and for cookie settings, so the placeholder value breaks login until this is done. Saving env vars triggers a redeploy. Then do step 3 for real if you deferred it.

## 6. Upstash (Redis)

1. Create a database at console.upstash.com: free tier, name `difflens`, region matching Render (`us-west` for oregon), TLS on.
2. Copy the `rediss://` connection string (the TLS scheme, with `default` as the user).
3. Set it as `REDIS_URL` in the Render Environment tab, replacing the placeholder. Saving triggers a redeploy.

Budget math, so nobody "optimizes" this later: the worker's idle cost is one BRPOP per 25s ≈ 3.5k commands/day against the 10k/day free limit. Sweeps and real reviews ride in the remaining headroom. If the limit is ever hit, Upstash rejects commands and the worker degrades to sweep-polling Postgres: reviews get slower, never lost.

## 7. Verify

- [ ] `https://difflens-api.onrender.com/health` returns 200 (first hit after idle takes 30-60s, see below)
- [ ] The Vercel URL loads and Sign in with GitHub completes the OAuth round trip
- [ ] The repository list loads after login
- [ ] Neon dashboard > Tables shows the schema (users, repos, and the alembic_version row at the current head)
- [ ] Render logs show `worker_started` after boot
- [ ] POST a review (once the UI wires it, or via the API) and watch it go queued -> running -> completed in Neon's `review_jobs` table

## 8. Free-tier facts

- Render free spins the service down after about 15 minutes idle. The next request eats a 30-60s cold start. Fine for a demo, just warm it up before showing anyone.
- Spin-down also stops the worker (it shares the container). A review submitted while asleep waits for the next request to wake the service; the sweep then picks it up. A review interrupted by spin-down is recovered by the stale-heartbeat sweep on the next boot.
- Migrations run on **every** deploy via `start.sh` (`alembic upgrade head` before uvicorn). Alembic no-ops when already at head, so this is cheap, but a broken migration blocks boot: that is the intended failure mode.
- Neon free tier suspends compute after idle too; the first query after suspend takes a few hundred ms extra. Nothing to do about it, just do not mistake it for a bug.
