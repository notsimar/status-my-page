// ── Shared CSRF + auth-error helpers ──────────────────────────────
// Loaded BEFORE app.js / healthchecks.js / rss.js. Single canonical
// implementation (previously duplicated across app.js and
// healthchecks.js, which shadowed each other at load time).

/** Read CSRF token from <meta> tag (never stored in JS globals). */
function _csrfToken() {
    const el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') || '' : '';
}

/** Update CSRF token in <meta> tag after rotation. */
function _setCsrfToken(token) {
    let el = document.querySelector('meta[name="csrf-token"]');
    if (!el) {
        el = document.createElement('meta');
        el.name = 'csrf-token';
        document.head.appendChild(el);
    }
    el.setAttribute('content', token);
}

/** CSRF-protected fetch — adds X-CSRF-Token header; rotates token on success.

    403 is disambiguated via the X-Auth-Error response header (set by the
    server for each guard):
      not-logged-in → session expired/wiped: reload back to the login UI
      csrf          → stale token: a reload re-injects a fresh token
      rate-limited  → kept deliberately separate: the guard tripped because
                      too many changes happened recently, NOT because the
                      session is bad. Reloading would silently discard the
                      in-flight change and fix nothing, so we surface an
                      inline error and let the user retry instead.
*/
async function csrfFetch(url, options = {}) {
    const token = _csrfToken();
    if (!options.headers) options.headers = {};
    if (token) options.headers['X-CSRF-Token'] = token;
    const res = await fetch(url, options);

    if (res.status === 403) {
        const reason = res.headers.get('X-Auth-Error') || 'not-logged-in';
        if (reason === 'rate-limited') {
            window.showToast && showToast(
                'Change not applied — too many recent actions. Wait a few seconds and try again.',
                'warn');
        } else {
            location.reload();
        }
    }

    // On success (+ 2xx), rotate the token by fetching a fresh one.
    if (res.ok && res.status < 300) {
        try {
            const tokRes = await fetch('/api/csrf-token');
            if (tokRes.ok) {
                const data = await tokRes.json();
                if (data.token) _setCsrfToken(data.token);
            }
        } catch (_) { /* non-critical — token will refresh on next page load */ }
    }

    return res;
}
