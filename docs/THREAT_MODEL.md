# Threat model: DiffLens

Written Day 8 (2026-08-20). This is the model the code was actually built
against, not an aspirational one. Where a control does not exist, it is in
"Accepted gaps" with the reason, because a threat model whose every row says
"mitigated" is a marketing document.

DiffLens reads other people's source code and hands it to a language model.
That single sentence produces most of what follows: everything inside a pull
request is written by someone who is not the user and not us.

## What is worth stealing

| Asset | Where it lives | Why it matters |
|---|---|---|
| GitHub OAuth access tokens | `provider_connections.access_token_enc`, Fernet-encrypted | Reads a user's repositories. Empty scope, so read-only and public only, which is the point of the scope choice |
| Session tokens | Cookie in the browser; only a SHA-256 hash in `sessions.token_hash` | Full impersonation for as long as the row lives |
| User AI provider keys | `user_ai_keys.key_enc`, Fernet-encrypted | Directly billable. A stolen Anthropic key costs its owner money |
| Server AI key and infrastructure credentials | Render and Vercel environment variables | Same, plus the deployment itself |
| Review contents | `reviews`, `findings` | A user's review of their own code is private to them, including which repositories they looked at |
| Availability | The free tier | One caller can exhaust a free quota for everyone, which is a denial of service that costs nothing to mount |

## Trust boundaries

```
   [browser]                          untrusted
       |  HTTPS, first-party cookie
       v
   [Vercel: Next.js]                  our code, public edge
       |  /api/backend/* rewrite (same origin, so no CORS and no third-party cookie)
       v
   [Render: FastAPI + worker]         our code, the real authorization gate
       |            |            |
       |            |            +--> [Postgres (Neon)]   source of truth
       |            |            +--> [Redis (Upstash)]   dispatch only, job ids
       |            |
       |            +--> [AI provider]   receives attacker-authored diff text
       |
       +--> [GitHub API]   outbound, carrying one user's token

   [pull request content] ------------ UNTRUSTED INPUT, crosses into
                                       analyzers, the AI prompt, and the UI
```

Boundary 1 (browser to Vercel) and boundary 2 (Vercel to Render) are the ones
an attacker reaches directly. The middleware in `web/src/middleware.ts` is a
UX redirect and explicitly not a gate; the API is the gate.

The boundary that gets underestimated is the last one. Repository content is
input, not code we trust, and it is the only input an attacker fully controls.

## STRIDE, briefly, against the boundaries above

### Spoofing

The only identity is GitHub's. There is no password to guess and no account
recovery flow to attack.

- The session cookie is an opaque 32-byte token; the database stores only
  `sha256(token)`, so a database read does not yield usable sessions.
- `HttpOnly`, `SameSite=Lax`, `Secure` in production, `Path=/`
  (`app/routers/auth.py`).
- Sessions are server-side rows and therefore revocable. Sign-out deletes the
  row, so the cookie is dead even if it was copied.
- OAuth `state` is HMAC-signed with a TTL (`app/security.py`), so a forged
  callback fails before any token exchange happens.
- Swept by test: every authenticated endpoint answers 401 for a missing
  cookie, a well-formed cookie that was never minted, and an expired session
  (`api/tests/test_authz.py`).

### Tampering

- A review is pinned to immutable base and head SHAs at creation. The worker
  fetches by those SHAs and never by branch name, so a push mid-review cannot
  change what was reviewed.
- Job state transitions are guarded UPDATEs that include the expected current
  state and the owning worker id (`api/worker/jobs.py`). Two workers racing
  produce one winner and one no-op, structurally rather than by convention.
- One live review per (pull request, head SHA) and one live job per review are
  partial unique indexes in Postgres, not application checks.
- The workspace built from the GitHub contents API rejects paths that would
  escape it (`api/worker/runner.py`), so a file named `../../etc/passwd` in a
  pull request writes nothing.

### Repudiation

Every request carries an `X-Request-ID` (echoed if supplied, generated
otherwise), and it appears on every log line for that request and inside every
error envelope, so a user's screenshot of an error maps to a log line. See
"Accepted gaps" for what this is not.

### Information disclosure

This is the category with the most surface, because everything here is
per-user data.

- Cross-account reads answer **404, never 403**. A 403 would confirm the row
  exists, turning review ids into an existence oracle. `api/tests/test_authz.py`
  asserts the foreign answer is byte-identical to the answer for an id that
  belongs to nobody.
- A stored AI key is never returned. The status endpoint exposes the provider,
  the model, and the last four characters, and nothing else
  (`app/routers/ai_settings.py`).
- Logs are scrubbed on two axes: by field name, and by credential shape inside
  any string, including flattened tracebacks and URL query strings
  (`app/logging_setup.py`). The second axis is the one that matters, because
  secrets escape inside formatted exceptions, not inside fields somebody
  named `token`.
- Schema rejections do not echo the rejected value. FastAPI's stock 422 body
  returns the offending `input`, which on `PUT /settings/ai-key` is an API
  key; the handler in `app/main.py` drops it.
- The OpenAPI schema and interactive docs are not served in production.
- The public demo routes are scoped by a column value, not by an argument.
  Every query starts from `Repository.is_demo` and no demo route accepts an
  id, so there is no parameter a caller can supply to widen what is returned;
  a demo route cannot serve a real user's review because it never has one in
  scope. With `DEMO_MODE` off they answer 404 rather than 403, so the surface
  is invisible rather than merely closed. Both are classified in
  `tests/test_authz.py`, whose completeness guard would have failed the build
  had they been added without an authorization decision.
- The GitHub scope is empty, so even a fully compromised token cannot read a
  private repository or write anywhere.

### Denial of service

- Starting a review is rate limited per user, counted in Redis
  (`app/rate_limit.py`). Reads are not limited: they are Postgres-bound and
  cheap, while a review spends GitHub quota, worker time, and AI tokens.
- The AI stage is skipped above a diff size cap rather than truncated, and it
  says so in the summary.
- GitHub's undocumented 300-file compare cap fails the review honestly rather
  than reviewing a silently truncated diff.
- Analyzers run under timeouts and are isolated from each other; one hung tool
  costs its own findings and nothing else (`app/analysis/analyzers/base.py`).
- Jobs retry at most `max_attempts` times with exponential backoff, so a
  poisoned job cannot spin forever.
- Persisted findings are capped at 100 per review.
- **The public demo is the only unauthenticated endpoint that starts work**,
  and its cap is structural rather than a check. The partial unique index
  `uq_reviews_pr_sha_live` permits one live review per (pull request, head
  sha); the demo is one pull request at one commit, so at most one demo job
  can be queued or running at any instant, enforced by Postgres. Pressing the
  button in a loop earns 409s, not a job flood. This matters specifically
  because the rate limiter fails open (gap 2): on an anonymous endpoint,
  failing open would otherwise mean unlimited job creation exactly when Redis
  is already unhealthy. The per-IP limit on the rerun is fair sharing layered
  on top of that floor, not the floor itself.
- Finished demo reviews are pruned to the newest few, so anonymous reruns
  cannot grow the database without bound (`app/demo/service.py`).
- The demo runs the offline replay provider, never a live model, so no volume
  of demo traffic can spend the operator's AI budget.

### Elevation of privilege

There are no roles to escalate into. The meaningful version of this threat is
"attacker-authored content gets executed", and the answer is that nothing in a
reviewed repository is ever run:

- Analysis is static. No install step, no test run, no build.
- ruff runs with `--isolated` and ESLint with `--no-config-lookup`, so the
  reviewed repository's own configuration is never loaded. This matters more
  for ESLint than it looks: an ESLint config can require a plugin, and a
  plugin is JavaScript that the linter executes. A pull request that adds
  `eslint.config.mjs` is an attempt to run code inside the reviewer.
- **Both linters are invoked with `--` before the file list.** A file NAME is
  an argv element and argv cannot tell a name from a flag, so without the
  separator a pull request containing a file literally called
  `--config=evil.js` supplies ESLint's `--config` and the flag above is
  bypassed entirely, taking the plugin execution with it. The ruff invocation
  had the same shape: a file called `--ignore=S105.py` chose which rules ran.
  Both were found by an adversarial review of this document's own claims,
  which is the argument for writing the claims down. Pinned by
  `test_a_filename_that_looks_like_a_flag_cannot_disarm_the_linter` and
  `test_a_filename_that_looks_like_a_flag_cannot_disarm_ruff`.
- The analysis package has no database handle and no network access by
  construction.

## Prompt injection

The AI reviewer reads a diff written by whoever opened the pull request. Treat
every line of it as an attempt to give the model instructions.

- The system prompt is static and versioned. It states that all repository
  content and pull request text is untrusted data under review, never
  instructions, and that embedded instructions must be **reported as a
  security finding rather than followed**.
- All untrusted content is wrapped in delimiters tagged with a per-request
  nonce (`secrets.token_hex`), so content cannot forge a closing tag and
  escape its own block.
- Model output is not trusted either. Every cited file and line is
  re-validated against the reviewed snapshot and discarded if it does not
  exist or falls outside the diff, so a model persuaded to invent a finding
  about `~/.ssh/id_rsa` produces nothing.
- The model has no tools and no write path. The worst outcome of a successful
  injection is a wrong or missing finding, never an action.
- Degradation is visible. A refusal, unusable output, truncation, an oversized
  diff, a mock provider, or the demo's recorded reviewer each append an honest
  sentence to the summary and set a flag on the review, so a suppressed AI
  stage cannot pass for a clean one. That failure mode was real: production
  ran with no AI for a day and looked healthy doing it.
- The public demo has no live model to inject into: it replays a recorded
  response. The recorded candidates still go through the full validation
  chain rather than around it, so the demo exercises the defense rather than
  bypassing it, and a sample edit that stranded a candidate on a line the
  diff no longer touches would be discarded exactly like a hallucination.
  `tests/analysis/test_demo_sample.py` asserts the discard counters are zero,
  so that drift fails the build instead of quietly emptying the demo.

## Supply chain

- Dependabot watches four manifests weekly, grouped so the pull requests stay
  readable (`.github/dependabot.yml`). Two of them are lockfile-pinned (`web/`
  and `api/eslint-runtime/`); the Python API is range-pinned in
  `pyproject.toml` with no committed lockfile, so its builds are reproducible
  only to the range, not to the byte. That gap is listed below.
- Secret scanning and push protection belong in Settings > Code security.
  They are repository settings rather than files, so nothing committed here
  can assert that they are on. Both were confirmed enabled on 2026-08-20 via
  `gh api repos/AnirudIye/difflens`, which is the way to re-check them:

      gh api repos/AnirudIye/difflens \
        --jq '.security_and_analysis'
- The reviewed repository's tooling is never installed or loaded, as above.
- CI runs lint, format, types, and the full test suite on every push,
  including the authorization sweep and the full-loop integration test.

## Accepted gaps

Deliberate, with reasons. This is the honest half.

1. **No Content-Security-Policy.** Next's App Router needs either
   `unsafe-inline` for its own bootstrap scripts or a per-request nonce, and
   the nonce route forces every page to render dynamically, which costs the
   static prerendering this deployment runs on. A policy containing
   `unsafe-inline` would be theatre. `X-Frame-Options`, `nosniff`,
   `Referrer-Policy`, and `Permissions-Policy` are set (`web/next.config.ts`).
2. **The rate limit is a fixed window and fails open.** A caller who times a
   burst across a window boundary briefly gets twice the limit, and if Redis
   is unreachable the limiter allows everything and logs that it did. Redis is
   a doorbell in this design and never the source of truth; taking reviews
   down because the doorbell is asleep would be a worse failure than the one
   being prevented.
3. **No CSRF token.** State-changing endpoints are POST, PUT, and DELETE with
   `Content-Type: application/json`, which an HTML form cannot produce
   cross-site, and the session cookie is `SameSite=Lax`, which withholds it
   from cross-site requests using those methods. The API is same-origin behind
   the Next rewrite, so there is no CORS allowance to abuse either. A token
   would add a third layer over two that already hold.
4. **Limited per-IP limiting on unauthenticated endpoints.**
   `GET /auth/github/login` can be hammered anonymously; it sets a cookie and
   redirects, touching no database, so the cost is Render's bandwidth rather
   than ours. The demo rerun is per-IP limited, with the caveat below.
5. **Secrets live in platform environment variables**, not a secrets manager.
   Free tier, and a manager would be one more service to hold credentials for.
6. **Key rotation is manual** and there is no rotation schedule. Credentials
   that have transited a chat window are owed a rotation and that is tracked
   outside this repository.
7. **The token encryption key is derived by SHA-256 from a passphrase**, not
   held in a KMS or HSM. Rotating it makes stored tokens and AI keys
   undecryptable; the code detects that and asks the user for the key again
   rather than pretending the row is live.
8. **Logs are free-tier container logs.** No retention policy, no alerting, no
   tamper-evident audit trail. Repudiation is addressed only to the extent
   that request ids make support possible.
9. **No account deletion or data export.** A user who signs in has rows in
   this database and no self-service way to remove them.
10. **AI providers see diff content.** Reviewed repositories are public, so
    the content is already public, but it does leave for a third party and
    that third party's retention terms apply. Bring-your-own-key exists partly
    so a user can choose whose terms they accept.
11. **No automated dependency vulnerability gate in CI.** Dependabot opens
    pull requests; nothing fails a build on a known advisory yet.
12. **The Python API has no committed lockfile.** `uv sync` resolves the
    ranges in `pyproject.toml` fresh on every CI run, so two builds a week
    apart can install different versions of the same declared dependency. The
    web app and the ESLint runtime do commit lockfiles. Generating `uv.lock`
    would close this, and is one command.
13. **The demo's per-IP limit trusts a spoofable header.** Behind Vercel's
    rewrite proxy the caller's address reaches the API only in
    `X-Forwarded-For`, and any client can put whatever it likes at the front
    of that header, so the identity the limiter counts against is not
    trustworthy. It does not need to be for the demo to be safe: the
    live-review index caps the demo at one job at a time in Postgres
    regardless of what the limiter believes. Making the identity trustworthy
    would mean pinning the proxy's own address, which couples the API to
    Vercel's egress ranges for no gain against the thing that actually costs
    money.

    What spoofing used to buy was Redis keys. Each distinct value is its own
    fixed-window bucket, so rotating the header both evaded the counter and
    left a key per value for the length of the window; taken verbatim, one
    unauthenticated request could pin megabytes of Redis for an hour.
    `client_ip` now accepts a value only if it parses as an address, so
    anything else collapses into a single `unknown` bucket that is rate
    limited like any other. Measured after the change: 13 spoofed requests
    produced one key of 38 bytes and started answering 429, against 23 keys
    of up to 30KB and no 429 at all before it.

    **Both** the header and `request.client` are validated, which is the part
    that is easy to get wrong. uvicorn runs its proxy-headers middleware by
    default and, for a connection from an address it treats as a trusted
    proxy, replaces `request.client` with whatever `X-Forwarded-For` said. A
    fallback to the "socket peer" therefore hands back the very value the
    header check just rejected. The first version of this fix did exactly
    that and still wrote 30KB keys while passing its own unit test, because
    the test supplied a peer the middleware had not rewritten.

    Rotating real addresses still costs small keys, and that residual is
    accepted. Redis is a doorbell here and the limiter fails open, so the
    worst case degrades the worker to its Postgres sweep rather than breaking
    the product.
14. **Private repositories are out of scope**, which is a security decision as
    much as a scope one: the OAuth `repo` scope grants write access, and a
    review tool that can write to your code is the wrong posture. Private
    repository support means a GitHub App with granular permissions.

## What would change this model

Any of these reopens it rather than amends it:

- Posting review comments back to GitHub, which needs write access.
- Executing repository code (running tests, installing dependencies), which
  turns static analysis into arbitrary code execution and needs a sandbox.
- Private repositories via a GitHub App.
- Organization accounts and shared reviews, which introduce the roles this
  model currently does not have.
