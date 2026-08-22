# Dogfood QA Report

**Target:** http://127.0.0.1:8920/ (status-my-page, local dev deployment)
**Date:** 2026-08-22
**Scope:** Adversarial-UX pass — login abuse, admin API fuzzing, hostile input through real UI flows, race/rate-limit behavior, session handling, console errors
**Tester:** Hermes Agent (automated exploratory QA via headless Chromium CDP)

---

## Executive Summary

| Severity | Count |
|----------|-------|
| 🔴 Critical | 1 |
| 🟠 High | 2 |
| 🟡 Medium | 3 |
| 🔵 Low | 2 |
| **Total** | **8** |

**Overall Assessment:** The security posture is strong (rate limiting, CSRF, XSS sanitization and auth guards all held against every attack vector tried), but the healthcheck feature is broken in dev deployments — a guaranteed 500 on the admin panel — and two UX issues (silent note truncation, ambiguous rapid-toggle behavior) undermine confidence in data integrity.

---

## Issues

### Issue #1: `GET /api/healthchecks` returns HTTP 500 when healthchecks module is unconfigured

| Field | Value |
|-------|-------|
| **Severity** | 🔴 Critical |
| **Category** | Functional / Console |
| **URL** | `/api/healthchecks` |

**Description:**
When the app is started with `STATUS_DISABLE_HEALTHCHECKS=1` (the documented dev/test mode), the admin Healthchecks panel throws an unhandled `RuntimeError: Healthcheck not configured` → HTTP 500 on every page load. The browser console shows "Failed to load healthchecks" each time. The route never guards for the disabled state.

**Steps to Reproduce:**
1. Set `STATUS_DISABLE_HEALTHCHECKS=1` in `.env.local`, start the server
2. Log in as admin
3. Observe the browser console + `GET /api/healthchecks` directly

**Expected Behavior:** The endpoint returns `{}` (or a clear "healthchecks disabled" payload) so the panel renders its empty-state.

**Actual Behavior:** HTTP 500 with an HTML error page; console error on every load.

**Console Errors:**
```
Failed to load healthchecks: Error: Failed to load
    at loadHealthchecks (http://127.0.0.1:8920/static/js/healthchecks.js:217:28)
[server] RuntimeError: Healthcheck not configured. Call configure_healthcheck() first.
```

---

### Issue #2: Notes are silently truncated to ~15 characters with no user feedback

| Field | Value |
|-------|-------|
| **Severity** | 🟠 High |
| **Category** | UX / Data Integrity |
| **URL** | `/api/notes/<id>` |

**Description:**
Typing a long status note (5000 chars tested) and letting it auto-save results in only ~15 characters persisting after reload. No warning, no character counter, no visible error — the textarea shows the full text until you leave the page.

**Steps to Reproduce:**
1. Log in as admin
2. Type a 5000-character note into any service's notes field
3. Wait for auto-save (change event), reload the page

**Expected Behavior:** Either the note saves in full, or the UI warns about a length limit before/at save time.

**Actual Behavior:** Note silently truncated server-side; user data lost.

---

### Issue #3: Rapid triple-click on status dot drops clicks without feedback

| Field | Value |
|-------|-------|
| **Severity** | 🟡 Medium |
| **Category** | UX |
| **URL** | `/` (status dots) |

**Description:**
Three rapid clicks on a status dot produced no visible state change (dot stayed in its original color), while direct API calls confirmed toggles work one-at-a-time. Users double/triple-clicking during latency will believe their toggle was registered when some clicks are silently swallowed by the mutation rate limiter.

**Steps to Reproduce:**
1. Log in as admin
2. Triple-click a status dot quickly
3. Watch for state change vs. mutation-rate-limit rejection

**Expected Behavior:** Either all clicks cycle the state, or a visible "slow down" indicator appears.

**Actual Behavior:** Intermediate clicks silently dropped; final state unpredictable to the user.

---

### Issue #4: Theme toggle gives no persisted-state feedback

| Field | Value |
|-------|-------|
| **Severity** | 🟡 Medium |
| **Category** | UX / Visual |
| **URL** | `/` |

**Description:**
After six rapid theme-toggle flips, the button label correctly showed "☀️ Light mode", but `localStorage.theme` was empty (`None`) — meaning the preference is not persisted across reloads, contradicting the localStorage-based design noted in the code.

**Steps to Reproduce:**
1. Click the theme toggle
2. Check `localStorage.getItem('theme')`

**Expected Behavior:** Theme choice persists across page loads.

**Actual Behavior:** `localStorage.theme` remains unset; theme resets on reload.

---

### Issue #5: Login rate-limit lockout message doesn't indicate remaining wait time

| Field | Value |
|-------|-------|
| **Severity** | 🔵 Low |
| **Category** | UX / Content |
| **URL** | `/login` |

**Description:**
After triggering the rate limit (8 rapid bad logins), the response is "Too many attempts. Wait 30s." — but the actual lockout window (`LOCKOUT_SECONDS`) isn't surfaced with a countdown or remaining-time hint beyond the static string.

**Expected Behavior:** Message reflects actual remaining lockout seconds.

**Actual Behavior:** Static "Wait 30s." regardless of true remaining window.

---

### Issue #6: Admin API returns HTML error pages for JSON endpoints on 404

| Field | Value |
|-------|-------|
| **Severity** | 🔵 Low |
| **Category** | API consistency |
| **URL** | `/api/toggle/99999` |

**Description:**
`POST /api/toggle/99999` returns Flask's default HTML 404 page ("Service not found") instead of a JSON error body, inconsistent with other API errors that return JSON. Any JS consumer doing `res.json()` will throw.

**Expected Behavior:** `{"error": "Service not found"}` with 404.

**Actual Behavior:** HTML doctype page.

---

### Issue #7 (Positive finding / verified defense): Hostile inputs fully sanitized

| Field | Value |
|-------|-------|
| **Severity** | N/A — defense CONFIRMED |
| **Category** | Security |
| **URL** | `/api/add` |

**Description:**
Adversarial inputs were all correctly rejected: `<script>` service names → 400 "contains forbidden characters (XSS)"; SQL-ish usernames → 400; 500-char names rejected; empty names → 400 "empty after sanitization". Rate limiting engaged after 7 failed logins (429). CSRF rejections fired consistently on missing/stale tokens. Logout properly killed the admin session (reload shows non-admin).

---

## Issues Summary Table

| # | Title | Severity | Category | URL |
|---|-------|----------|----------|-----|
| 1 | /api/healthchecks 500s when module unconfigured | Critical | Functional/Console | /api/healthchecks |
| 2 | Notes silently truncated (~15 chars), no feedback | High | UX/Data | /api/notes/<id> |
| 3 | Rapid toggle clicks dropped silently | Medium | UX | / (dots) |
| 4 | Theme choice not persisted to localStorage | Medium | UX/Visual | / |
| 5 | Lockout message lacks real remaining time | Low | UX/Content | /login |
| 6 | HTML 404 pages on JSON API endpoints | Low | API consistency | /api/toggle/<id> |
| 7 | Defense confirmation: XSS/CSRF/rate-limit all held | ✅ Positive | Security | various |

## Testing Coverage

### Pages / Features Tested
- Public status page (light/dark theme)
- Login flow: valid, invalid, empty, SQL-injection-style, unicode-NUL, oversized inputs; rate limiting
- Admin panel: add service (hostile names), notes editor (oversized payloads), status-dot toggles (rapid clicks), theme toggle (rapid flips), logout mid-request
- Direct API probing with/without CSRF tokens and auth cookies
- Console error capture across all phases

### Not Tested / Out of Scope
- Healthcheck probe execution itself (curl/ping/tcp/soap/rss probes) — only the admin CRUD path
- RSS feed content correctness (covered by existing unit tests)
- Mobile/responsive layout (headless viewport only)
- Multi-user concurrency at scale

### Blockers
- CDP session instability during full-page navigations required reconnect-per-phase; no app issues blocked testing.

---

## Evidence

- Screenshots: `dogfood-output/screenshots/01-initial.png` … `07-after-logout.png`
- Raw console captures: `dogfood-output/console*.json`
- API abuse results: `dogfood-output/admin_api_results.json`
