# 0003. The browser only ever talks to one origin

Status: Accepted
Date: 2026-08-21

Retrospective record. The rewrite landed 2026-08-15 with the frontend scaffold; evidence cited
below (the missing CSP, gap 13) is Day 8 to 9 work.

## Context

The web app is on Vercel, the API on Render (`render.yaml`: one Docker web service `difflens-api`,
free plan). Two registrable domains, because the $0 free tiers are on different providers.

`api/app/routers/auth.py` mints an opaque session token and sets a `session` cookie: `httponly`,
`samesite="lax"`, `path="/"`, `secure=_secure_cookies()`. Called cross-origin it is a third-party
cookie, and `SameSite=Lax` alone means a `fetch()` from Vercel to Render carries no session.
Making it work needs `SameSite=None; Secure` and a credentialed CORS allowance.

Render free also spins down after about 15 minutes idle and wakes in 30 to 60s
(`docs/DEPLOYMENT.md` section 9; 32s measured 2026-08-20, mitigated by the keep-warm cron in step
7). That constrains every option, so it is not a cost of this one.

## Decision

Every browser API call goes through a Next.js rewrite in `web/next.config.ts`:

```
source: "/api/backend/:path*",
destination: `${process.env.API_ORIGIN ?? "http://localhost:8000"}/:path*`,
```

`API_ORIGIN` is `https://difflens-api.onrender.com` in the Vercel project (`docs/DEPLOYMENT.md`
step 4). The prefix is stripped in transit: `/api/backend/auth/me` arrives at `/auth/me`.

Four files know the prefix: `web/src/lib/api.ts`, where `apiFetch` prepends it, and three sign-in
links that cannot go through `apiFetch` because a link is not a fetch (`login/page.tsx:43`,
`repositories/page.tsx:89`, `repositories/[id]/page.tsx:151`). No absolute base URL and no
`NEXT_PUBLIC_` API host exists under `web/src`, and nothing tests that.

OAuth uses the same path: `auth.py:43` sets `redirect_uri` to
`settings.frontend_origin + "/api/backend/auth/github/callback"`, so `Set-Cookie` arrives from the
Vercel origin and is first-party.

`api/app/main.py` imports no `CORSMiddleware`. That removes the *need* for a CORS allowance but is
not itself a defence: absent middleware still lets a cross-site request reach Render, it only
stops the caller reading the reply. `SameSite=Lax` withholds the cookie, and that plus JSON-only
state-changing methods is the pair `docs/THREAT_MODEL.md` gap 3 rests on; the no-CORS point is an
aside there, not a layer.

## Alternatives considered

**CORS with `SameSite=None` third-party cookies.** Rejected: it stakes login on third-party cookie
policy browsers are narrowing. The preflight cost usually cited against it is weak, preflights
cache per `Access-Control-Max-Age` while the hop does not. Neither side was measured.

**Bearer token in `localStorage`.** Rejected: readable by JavaScript, and this app renders
attacker-authored pull request text with no CSP (`web/next.config.ts` explains why), so `HttpOnly`
is doing real work.

**Custom domain, API on a subdomain.** Rejected: a domain is not $0, and still cross-origin.

**Catch-all Route Handler** at `app/api/backend/[...path]/route.ts`. As route-agnostic as a
rewrite, so "keeping it in step with new API routes" was never a real reason. Rejected because it
runs our function code on every API call instead of the platform's routing layer; judgement, not
measurement.

**Both halves on one host.** One origin, no proxy. Rejected: Render free is one Docker service
already running FastAPI and the worker, so it would also build and serve Next, losing Vercel's
build pipeline and putting the frontend behind the same cold start. `docs/SCOPE.md` keeps
single-host on the post-sprint list.

## Consequences

- Vercel becomes a hard dependency for reaching an otherwise healthy API, and every byte,
  including the 2.5s review poll (`reviews/[id]/page.tsx`, `demo/page.tsx`), counts against a
  Hobby quota this $0 design otherwise ignores.
- The proxy manufactures failures the API never sees, carrying no `X-Request-ID` and no error
  envelope (`web/src/lib/api.ts:48` works around them). `docs/THREAT_MODEL.md` says an error
  screenshot maps to a log line; these map to nothing in Render's logs.
- Unmeasured and load-bearing: what Vercel does with a rewrite to a sleeping origin. Holding open
  for 60s and erroring at some threshold need different retry designs; nothing here answers which.
- `API_ORIGIN` unset or misspelled on Vercel fails silently: the rewrite falls back to
  `http://localhost:8000`, the build succeeds, Render's `/health` passes, and every API call dies
  inside Vercel.
- Preview deployments cannot sign in: one registered callback URL and one `FRONTEND_ORIGIN`, both
  the production host (`docs/DEPLOYMENT.md` step 3), so OAuth fails on every preview hostname.
- The API loses the caller's address; it arrives only in forgeable `X-Forwarded-For`
  (`api/app/rate_limit.py` collapses unparseable values into one `unknown` bucket, gap 13).
- The proxy is a cookie convenience, not access control. `https://difflens-api.onrender.com`
  answers anyone with curl, as `.github/workflows/keep-warm.yml` does every 10 minutes; nothing
  authenticates Vercel to Render, so authorization must be correct at the API for every route.
