# Code Walkthrough — Hardening Pass (ea92476..100fefa)

**Scope:** 4 commits — `df32360` (origin: XSS fix, secret-key persistence, config locking), `b288d6e` (local wip: CSRF dedup, rate-limit persistence, input-filter retiering, install/start rework), `51847e7` (merge), `100fefa` (review fixes).
~850 insertions across 30 files. Theme: **security hardening + multi-worker correctness.**

Suggested time: ~45 min. Each section lists the file/function, the problem it fixes,
and where to look.

---

## 1. Startup & Secrets (`app.py`)

### `_resolve_secret_key()` (app.py:103)
- **Before:** `app.secret_key = os.environ.get(SECRET_ENV) or secrets.token_hex(32)` —
  with 2 gunicorn workers, each process generated its *own* random key → sessions
  randomly broke (cookies signed by worker A rejected by worker B).
- **After:** env var wins; otherwise a key file at `instance/.secret_key`, created
  once with `os.open(..., O_CREAT|O_EXCL, 0o600)`. The `O_EXCL` matters: two workers
  starting simultaneously race to create the file; the loser gets `FileExistsError`
  and re-reads the winner's key instead of writing over it.
- Fallback chain: env → file → read-existing-file → ephemeral (dev only).

### Fail-fast DB init (app.py ~494)
- If the DB is absent and `init_db()` fails, the app now exits(1) loudly instead of
  serving an empty page. "A half-working status page is worse than a clearly-down one."

### `init_rate_limit_db()` (app.py:506)
- Rehydrates login-failure / mutation-rate / CSRF-failure counters from the shared
  SQLite `rate_limits` table so lockouts survive gunicorn reloads. See §3 for the
  expiry caveat this introduced (and its fix).

---

## 2. Static Export XSS + Logo Resolution (`statuspage/routes.py`, `config.py`)

### `generate_static_html()` (routes.py:150)
- The exported HTML is built with f-strings — **Jinja autoescaping does not apply**.
  Admin-controlled `name` and `notes` were interpolated raw. Now wrapped in
  `html.escape()` (routes.py ~207, 218). This was a stored-XSS vector against
  anyone viewing the exported file.
- Logo embedding switched from hardcoded dark/light PNG pair to
  `get_logo_local_path()` inlined as a data URI.

### Logo path guards (`statuspage/config.py`)
- `_resolve_logo_rel()` (config.py:156) is the single guard: strips `/static/`,
  rejects empty values and any `..` path part.
- `get_logo_url()` (public URL) and `get_logo_local_path()` (filesystem, adds
  resolve()-based containment check + non-empty file check) both derive from it —
  previously duplicated logic that could drift.

---

## 3. Rate-Limit Persistence & Lockout Expiry (`statuspage/auth.py`) ⚠️ most subtle section

The design: three in-memory dicts (`_failed_logins`, `_mutation_rates`,
`_csrf_failures`) remain the fast authoritative path. `_persist_rate_state()`
(auth.py:115) snapshots them to a shared SQLite table (best-effort, never raises);
`init_rate_limit_db()` (auth.py:164) hydrates on startup so limits survive restarts.

**The bug this initially shipped with:** `is_locked()` only counted entries.
A snapshot persisted before a restart would immediately re-lock the IP on boot,
and since nothing pruned those entries, the lockout **never expired** — permanent
DoS per IP.

**Fix (commit 100fefa):**
- `is_locked()` (auth.py:93) prunes timestamps older than `LOCKOUT_SECONDS` before
  counting, and deletes the key when empty.
- `init_rate_limit_db()` drops stale timestamps during hydration too.

Also note:
- `require_admin` (auth.py ~230) returns uniform 403s but sets an `X-Auth-Error`
  header (`not-logged-in` / `csrf` / `rate-limited`) — see §6 for why.
- Known trade-off: each worker persists only its own dict (last-writer-wins);
  acceptable for a side-channel snapshot, documented in the docstring.

---

## 4. Concurrency: Healthcheck Config Writes (`statuspage/config.py`, `routes.py`)

- `HEALTHCHECKS_CFG_LOCK = threading.Lock()` (config.py:273) guards the
  load-modify-save sequences on the healthchecks section of config.yaml.
- All three admin routes take it: create (routes.py:634), update (656),
  delete (811). Update logic refactored into `_apply_healthchecks_update()`
  with the contract "caller MUST hold the lock" in its docstring.
- Without this, two concurrent admin edits could interleave read-modify-write
  and silently lose one update.

---

## 5. Healthcheck Engine (`healthcheck.py`, `constants.py`)

### Billion-laughs defense
- `feed_treats_as_unfetchable()` (healthcheck.py:44) regex-rejects any RSS body
  containing a DOCTYPE with internal `<!ENTITY` declarations **before**
  `ET.fromstring()`. Rejected as fetch failure — never as green.

### One-shot runs are bounded
- `run_healthchecks_once()` backs `POST /api/healthcheck/run`, which blocks an
  HTTP request. Two bounds from constants.py:
  - per-check timeout capped at `HEALTHCHECK_ONE_SHOT_TIMEOUT_CAP` (15 s)
  - overall wall clock `HEALTHCHECK_RUN_HARD_TIMEOUT` (20 s); checks past the
    deadline report `"timed_out": true` instead of stalling.
- The background worker deliberately keeps full configured timeouts — the cap
  applies only to the request-path run.

### Severity rule extracted
- `severity_from_failures()` (healthcheck.py:51): `>=retries` failures → degraded,
  `>=3×retries` → red. Raises ValueError below threshold (contract was previously
  misleading in its own docstring).

---

## 6. Frontend CSRF (`static/js/csrf.js`, `templates/index.html`)

- `csrfFetch`/`_csrfToken`/`_setCsrfToken` were **duplicated** in app.js and
  healthchecks.js and shadowed each other at load time. Now one canonical
  implementation in csrf.js, loaded first (index.html:299).
- Behavior change on 403: the server's `X-Auth-Error` header disambiguates:
  - `not-logged-in` / `csrf` → reload (session or token genuinely stale)
  - `rate-limited` → `alert()` + retry instead of reload (reload discarded the
    user's in-flight edit and fixed nothing)

---

## 7. Input Filtering Retiered (`input_filter.py`)

- SQLi patterns split: **compound** (UNION SELECT-style constructs — applied to
  names/notes via new `check_sqli_compound`) vs **aggressive** (bare `--`, `;`,
  0x hex — only in `sanitize_text()` for shell-proximate fields).
- Rationale: all SQL is parameterized, so rejecting legitimate notes like
  "rollback; retry at noon" bought zero security. Compound tier still blocks
  real attack syntax.

---

## 8. API Correctness (`statuspage/services.py`, `routes.py`)

- `toggle_item` returns `None` and `update_notes` returns `False` for missing ids;
  routes map these to 404. Previously toggle on a bad id returned "green" —
  indistinguishable from success (this change exposed a hardcoded-id test, fixed
  in test_mc_dc.py).
- `GET /api/healthchecks` redacts internals for non-admins via
  `_redact_healthcheck()` (routes.py:115): keeps type/interval/timeout/retries,
  strips url/host/port/keywords (internal topology). Admin session gets full config.

---

## 9. Deployment Scripts (`install.sh`, `start.sh`, `cleanup.sh`)

- **install.sh:** prompts credentials *before* seeding (init needs the hash);
  refuses empty passwords; existing `.env.local` is kept unless
  `SP_INSTALL_OVERRIDE_ENV=1` (which now actually replaces it — was a no-op);
  backups/env chmod 0600; systemd unit binds 127.0.0.1.
- **start.sh:** matches systemd (gunicorn, `127.0.0.1:8920`); sources env with
  `set -a` (the old `export $(xargs)` broke on values containing spaces/quotes).
- **cleanup.sh `show`:** rejects path traversal in archive filenames.

---

## 10. Tests

- Suite: **465 passed** (was 3 failed / 135 errors pre-fix).
- New coverage: healthcheck one-shot bounds, RSS DTD rejection, severity tiers,
  logo traversal, MC/DC updates for the retiered SQLi checks.
- Note for reviewers: conftest sets `STATUS_ADMIN_PASS_HASH` before importing app;
  a stray `.env.local` in the working dir can shadow it (dotenv loads only when
  the env var is unset — app.py:24).

---

## Review-status summary

| Area | Verdict |
|---|---|
| Lockout persistence | fixed (permanent-lockout bug closed) |
| Secret key | fixed (O_EXCL race) |
| Static export XSS | fixed |
| Config concurrency | fixed |
| Known trade-offs | per-worker rate-state snapshots; dotenv/.env.local precedence |
