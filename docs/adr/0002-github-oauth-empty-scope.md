# 0002. GitHub OAuth with an empty scope

Status: Accepted
Date: 2026-08-21

Written retrospectively on Day 10. The decision was taken and shipped at gate 2 (`docs/SCOPE.md`).

## Context

Every object in the product is a GitHub object, so the only identity worth having is GitHub's.
`docs/THREAT_MODEL.md` lists the GitHub access token first in "What is worth stealing": whatever
that token can do is what a database leak or a compromised Render environment can do.

## Decision

GitHub OAuth is the only way in, and the authorize URL carries no scope at all. `github_login` in
`api/app/routers/auth.py` sends exactly `client_id`, `redirect_uri` and `state`. GitHub treats a
missing scope as the empty scope, so the token reads, as the user, their public profile and their
public repositories via `/user/repos` with `affiliation=owner`
(`api/app/services/github_client.py`), at the authenticated 5,000 requests/hour. It cannot read
private repository contents and cannot write anything.

`encrypt_token` in `api/app/security.py` Fernet-encrypts it at rest, and it is never returned:
`GET /auth/me` exposes only id, login, name, avatar_url and `github_connected`. Sessions are opaque
server-side rows storing `sha256(token)`, and the OAuth `state` is HMAC-signed with a 600 second
TTL. Cookie attributes and the same-origin proxy are ADR 0003.

Requesting an empty scope does not guarantee an empty-scope token. For an OAuth App the user has
already authorized, GitHub returns a token carrying the scopes previously granted, so a user who
once authorized this client with `repo` gets a `repo` token back from a scope-less URL.
`github_callback` stores the returned scope string in `provider_connections.scopes` and nothing
reads it. Rejecting a non-empty scope there is a few lines, and is not done; that is recorded here
rather than claimed as handled. `test_login_redirects_to_github_without_scope`
(`api/tests/test_auth.py`) pins the outbound URL only, asserting `"scope" not in params`; nothing
asserts anything about the token that comes back.

## Alternatives considered

**Store no GitHub token at all**, using GitHub for identity and reading public objects anonymously
or through one server token. It deletes the asset the threat model ranks first, and it loses on
quota: unauthenticated GitHub is 60 requests/hour per IP, while one review spends a compare plus a
contents call per changed file, up to `COMPARE_FILE_CAP = 300` (`api/worker/runner.py`). One shared
token pools every user into a single 5,000/hour bucket, where one heavy user starves the rest.

**The `repo` scope.** Read and write: it can push, force-push and delete branches, for a tool that
only reads. `docs/SCOPE.md` and gap 14 of `docs/THREAT_MODEL.md` put private repos out of scope on
this ground. `public_repo` is the same objection with a smaller blast radius.

**A GitHub App** with `Contents: read` and `Metadata: read`. In its strongest form this is a
tighter posture than what shipped, not a looser one: per-repository grants, expiring tokens, and
user-to-server tokens through the same web flow, so the identity model here survives unchanged. It
loses on build cost inside a 10-day sprint, not on security: an installation flow, JWT signing, and
installation-token minting and refresh. It is post-sprint in `docs/SCOPE.md` and
`docs/architecture.md`.

## Consequences

- The Fernet key from `_get_fernet()` also protects `user_ai_keys.key_enc`, which
  `docs/THREAT_MODEL.md` calls directly billable. The empty scope makes a stolen GitHub token
  cheap, not the store holding it: a compromise yielding one yields the other.
- That key is `sha256(passphrase)`, not a KDF, so a weak `TOKEN_ENCRYPTION_KEY` is brute-forceable
  offline against one leaked ciphertext. It defaults to `""` in `api/app/config.py`, and nothing
  refuses to boot without it: `_get_fernet` logs a warning and generates an ephemeral key, on first
  use rather than at startup, so the deploy looks healthy and every token stored before the next
  restart becomes undecryptable after it.
- Rotating that key breaks GitHub tokens loudly, not gracefully. `decrypt_token` is called bare in
  `api/app/deps.py` and `api/worker/runner.py`, so `InvalidToken` surfaces as a 500. The
  detect-and-ask-again behaviour gap 7 describes exists only for AI keys (`api/app/ai/factory.py`,
  `api/app/routers/ai_settings.py`); `token_invalid` is set only from a GitHub 401.
- Revoking at GitHub is not a sign-out. GitHub calls start returning 401, which
  `mark_token_invalid` turns into a reconnect prompt, but the DiffLens session is an independent
  row and lives out its 7 day TTL.
- The empty scope is a request, not a guarantee. An OAuth App authorization is the union of every
  scope a user has granted the client, so GitHub can return a token carrying more than the
  authorize URL asked for. `provider_connections.scopes` recorded that and was read by nothing
  until Day 10, when a non-empty value started emitting a
  `github_token_carries_unrequested_scopes` warning. It is still only a warning: the connection is
  accepted, because refusing would lock a user out of the account the check protects.

Revisit when the first user asks for a private repository, or an organization wants an
install-level grant. Either moves this to the GitHub App.
