// ── Status Feed (RSS) UI ──────────────────────────────────────────
// Public: surfaces the feed link when the feed is enabled.
// Admin:  toggle the feed on/off (POST /api/rss).
// Depends on csrfFetch() from healthchecks.js (loaded before this file).

(function () {
    const rssRow = document.getElementById('rssRow');
    const rssLink = document.getElementById('rssLink');
    const rssUrl = document.getElementById('rssUrl');
    const rssAdmin = document.getElementById('rssAdmin');
    const rssEnabled = document.getElementById('rssEnabled');
    const rssState = document.getElementById('rssState');
    const rssAdminUrl = document.getElementById('rssAdminUrl');

    let currentUrl = '/feed.xml';

    function applyState(data, isAdmin) {
        currentUrl = (data && data.url) || '/feed.xml';
        const on = !!(data && data.enabled);

        // Public feed row — only visible when the feed is published.
        if (rssRow) {
            rssRow.classList.toggle('hidden', !on);
            if (rssUrl) rssUrl.textContent = currentUrl;
            if (rssLink) rssLink.href = currentUrl;
        }

        // Admin toggle panel — reflect current state regardless of on/off.
        if (isAdmin && rssEnabled) {
            const wasDisabled = rssEnabled.disabled;
            rssEnabled.disabled = false;
            rssEnabled.checked = on;
            if (rssState) rssState.textContent = on ? 'Enabled' : 'Disabled';
            if (rssAdminUrl) rssAdminUrl.textContent = currentUrl;
            if (wasDisabled) rssEnabled.disabled = true;
        }
    }

    async function loadStatus() {
        try {
            const res = await fetch('/api/rss');
            if (!res.ok) return;
            const data = await res.json();
            const isAdmin = document.body.classList.contains('admin');
            applyState(data, isAdmin);
        } catch (_) {
            // feed endpoint unavailable — leave UI in default hidden state
        }
    }

    async function toggle(event) {
        if (event) event.preventDefault();
        if (!rssEnabled) return;
        const target = rssEnabled.checked;
        rssEnabled.disabled = true;
        if (rssState) rssState.textContent = 'Saving…';
        try {
            // csrfFetch (from healthchecks.js) adds the X-CSRF-Token header and
            // refreshes the rotated token on success.
            const res = await csrfFetch('/api/rss', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: target }),
            });
            const data = await res.json();
            if (!res.ok) {
                if (rssState) rssState.textContent = 'Error: ' + (data.error || 'unknown');
                rssEnabled.checked = !target;
                return;
            }
            // The server is the source of truth — re-load to sync URL/state.
            await loadStatus();
        } catch (err) {
            if (rssState) rssState.textContent = 'Error: ' + err.message;
            rssEnabled.checked = !target;
        } finally {
            if (rssEnabled) rssEnabled.disabled = false;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        loadStatus();
        if (rssEnabled && rssAdmin) {
            rssEnabled.addEventListener('change', toggle);
        }
    });
})();
