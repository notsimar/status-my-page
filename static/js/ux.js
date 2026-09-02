// Shared UX layer: toasts, confirm dialogs, relative time, session
// expiry warning, and auto-refresh polling for status-my-page.
//
// Loaded after csrf.js so it can hook auth-expired responses. Exposes:
//   showToast(msg, type)      — non-blocking notification (info/success/error)
//   uxConfirm(message)        — styled confirm modal, resolves true/false
//   timeAgo(iso)              — "3 min ago" formatting
//   startAutoRefresh()        — public status polling (non-admin pages)
//   armSessionWatch()         — warns before idle logout, offers stay-logged-in
(function () {
    'use strict';

    // ── Toast system ────────────────────────────────────────────────
    let toastHost = null;

    function ensureToastHost() {
        if (toastHost && document.body.contains(toastHost)) return toastHost;
        toastHost = document.createElement('div');
        toastHost.id = 'ux-toast-host';
        toastHost.setAttribute('role', 'status');
        toastHost.setAttribute('aria-live', 'polite');
        toastHost.style.cssText =
            'position:fixed;bottom:16px;right:16px;z-index:10000;' +
            'display:flex;flex-direction:column;gap:8px;max-width:340px;';
        document.body.appendChild(toastHost);
        return toastHost;
    }

    function showToast(msg, type) {
        type = type || 'info';
        const colors = {
            info: '#4a90d9', success: '#27ae60',
            error: '#c0392b', warn: '#e67e22'
        };
        const host = ensureToastHost();
        const t = document.createElement('div');
        t.className = 'ux-toast ux-toast-' + type;
        t.style.cssText =
            'background:#1e1e2e;color:#eee;padding:10px 14px;border-radius:8px;' +
            'border-left:4px solid ' + (colors[type] || colors.info) + ';' +
            'box-shadow:0 4px 12px rgba(0,0,0,.35);font-size:0.85rem;' +
            'opacity:0;transform:translateX(12px);transition:opacity .25s,transform .25s;';
        t.textContent = msg;
        host.appendChild(t);
        requestAnimationFrame(() => {
            t.style.opacity = '1'; t.style.transform = 'none';
        });
        setTimeout(() => {
            t.style.opacity = '0'; t.style.transform = 'translateX(12px)';
            setTimeout(() => t.remove(), 300);
        }, 4200);
    }
    window.showToast = showToast;

    // Escape text that lands in innerHTML (okLabel is the only dynamic part
    // of the dialog markup — names are whitelisted server-side, but escape
    // here so the dialog can never be used as an injection sink).
    function _escHtml(s) {
        return String(s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;')
            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // ── Confirm modal (styled replacement for window.confirm) ───────
    function uxConfirm(message, opts) {
        opts = opts || {};
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'ux-confirm-overlay';
            overlay.style.cssText =
                'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;' +
                'display:flex;align-items:center;justify-content:center;';
            const danger = opts.danger ? 'background:#c0392b;color:#fff;' : '';
            overlay.innerHTML =
                '<div role="dialog" aria-modal="true" aria-label="Confirm" ' +
                'style="background:#1e1e2e;color:#eee;border-radius:10px;' +
                'padding:20px 24px;max-width:400px;width:90%;box-shadow:0 8px 30px rgba(0,0,0,.5);">' +
                '<p style="margin:0 0 18px;font-size:0.92rem;line-height:1.45;"></p>' +
                '<div style="display:flex;gap:8px;justify-content:flex-end;">' +
                '<button data-act="cancel" style="padding:7px 16px;border-radius:6px;' +
                'border:1px solid #555;background:transparent;color:#eee;cursor:pointer;">Cancel</button>' +
                '<button data-act="ok" style="padding:7px 16px;border-radius:6px;border:none;' +
                'cursor:pointer;' + (danger ||
                    'background:#4a90d9;color:#fff;') + '">' +
                (opts.okLabel ? _escHtml(opts.okLabel) : 'OK') + '</button></div></div>';
            overlay.querySelector('p').textContent = message;
            const done = (val) => { overlay.remove(); resolve(val); };
            overlay.querySelector('[data-act=cancel]').onclick = () => done(false);
            overlay.querySelector('[data-act=ok]').onclick = () => done(true);
            overlay.addEventListener('click', e => {
                if (e.target === overlay) done(false);
            });
            overlay.addEventListener('keydown', e => {
                if (e.key === 'Escape') done(false);
                if (e.key === 'Enter') done(true);
            });
            document.body.appendChild(overlay);
            const okBtn = overlay.querySelector('[data-act=ok]');
            okBtn.focus();
            // basic focus trap
            overlay.addEventListener('keydown', e => {
                if (e.key !== 'Tab') return;
                const f = overlay.querySelectorAll('button');
                const first = f[0], last = f[f.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                    e.preventDefault(); last.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                    e.preventDefault(); first.focus();
                }
            });
        });
    }
    window.uxConfirm = uxConfirm;

    // ── Relative time ───────────────────────────────────────────────
    function timeAgo(iso) {
        const then = new Date(iso).getTime();
        if (isNaN(then)) return iso;
        const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
        if (s < 45) return 'just now';
        const m = Math.floor(s / 60);
        if (m < 45) return m + ' min ago';
        const h = Math.floor(m / 60);
        if (h < 24) return h + ' hr ago';
        const d = Math.floor(h / 24);
        if (d < 30) return d + ' day' + (d > 1 ? 's' : '') + ' ago';
        return new Date(iso).toLocaleDateString();
    }
    window.timeAgo = timeAgo;

    // ── Auto-refresh polling (public visitors) ──────────────────────
    // Polls lightweight state endpoint and updates dots/labels in place.
    const REFRESH_MS = 30000;
    let lastUpdatedEl = null;

    function ensureUpdatedIndicator() {
        if (lastUpdatedEl && document.body.contains(lastUpdatedEl)) return lastUpdatedEl;
        const badge = document.getElementById('overallBadge');
        if (!badge) return null;
        lastUpdatedEl = document.createElement('div');
        lastUpdatedEl.id = 'last-updated';
        lastUpdatedEl.style.cssText =
            'font-size:0.72rem;color:var(--text-muted,#888);margin-top:4px;text-align:center;';
        badge.insertAdjacentElement('afterend', lastUpdatedEl);
        return lastUpdatedEl;
    }

    function markUpdated() {
        const el = ensureUpdatedIndicator();
        if (el) el.textContent = 'Updated just now';
    }

    function tickAgoLabel() {
        const el = lastUpdatedEl;
        if (!el || !el.dataset.ts) return;
        const s = Math.floor((Date.now() - Number(el.dataset.ts)) / 1000);
        el.textContent = s < 5 ? 'Updated just now'
            : 'Updated ' + (s < 60 ? s + 's' : Math.floor(s / 60) + 'm') + ' ago';
    }

    async function pollStatus() {
        try {
            const r = await fetch('/api/status', { cache: 'no-store' });
            if (!r.ok) return;
            const items = await r.json();
            if (!Array.isArray(items)) return;
            let changed = false;
            for (const it of items) {
                const row = document.querySelector(
                    '.status-row[data-id="' + it.id + '"]');
                if (!row) continue;
                const dot = row.querySelector('.status-dot');
                const label = row.querySelector('.status-label');
                if (dot && !dot.classList.contains(it.status)) {
                    dot.classList.remove('green', 'degraded', 'red');
                    dot.classList.add(it.status);
                    label.classList.remove('green', 'degraded', 'red');
                    label.classList.add(it.status);
                    label.textContent = ({
                        green: 'Operational', degraded: 'Degraded', red: 'Outage'
                    })[it.status] || it.status;
                    changed = true;
                }
                const ta = row.querySelector('.notes-input');
                if (ta && typeof it.notes === 'string' &&
                    ta.value !== it.notes && document.activeElement !== ta) {
                    ta.value = it.notes;
                }
            }
            if (changed) window.showToast('Statuses updated from server', 'info');
            const el = ensureUpdatedIndicator();
            if (el) { el.dataset.ts = Date.now(); tickAgoLabel(); }
        } catch (_) { /* offline — retry next cycle */ }
    }

    function startAutoRefresh() {
        if (document.body.classList.contains('admin')) return; // admin edits live
        markUpdated();
        setInterval(pollStatus, REFRESH_MS);
        setInterval(tickAgoLabel, 15000);
        window.addEventListener('focus', () => {
            if (Date.now() - (Number(lastUpdatedEl?.dataset.ts) || 0) >
                REFRESH_MS) pollStatus();
        });
    }

    // ── Session expiry warning ──────────────────────────────────────
    // Server auto-logs the admin out after 5 min idle. Warn at ~4 min with a
    // stay-logged-in button that pings /auth-check (resets the server timer).
    const IDLE_WARN_AT = 4 * 60 * 1000;
    let idleTimer = null;

    function armSessionWatch() {
        if (!document.body.classList.contains('admin')) return;
        resetIdleTimer();

        ['click', 'keydown', 'mousemove', 'touchstart'].forEach(ev =>
            document.addEventListener(ev, resetIdleTimer, { passive: true }));
        window.addEventListener('focus', resetIdleTimer);
    }

    function resetIdleTimer() {
        clearTimeout(idleTimer);
        idleTimer = setTimeout(showIdleWarning, IDLE_WARN_AT);
    }

    function showIdleWarning() {
        if (document.getElementById('idle-warning')) return;
        const bar = document.createElement('div');
        bar.id = 'idle-warning';
        bar.setAttribute('role', 'alertdialog');
        bar.setAttribute('aria-label', 'Session expiring soon');
        bar.style.cssText =
            'position:fixed;top:0;left:0;right:0;z-index:9998;display:flex;' +
            'align-items:center;justify-content:center;gap:14px;padding:10px;' +
            'background:#e67e22;color:#fff;font-size:0.88rem;';
        const msg = document.createElement('span');
        msg.textContent = 'You will be logged out in about 1 minute due to inactivity.';
        const btn = document.createElement('button');
        btn.textContent = 'Stay logged in';
        btn.style.cssText = 'padding:5px 14px;border-radius:6px;border:none;' +
            'cursor:pointer;font-weight:600;';
        btn.onclick = async () => {
            try { await fetch('/auth-check'); } catch (_) {}
            bar.remove();
            resetIdleTimer();
            window.showToast('Session refreshed', 'success');
        };
        bar.append(msg, btn);
        document.body.appendChild(bar);
        btn.focus();
    }

    // ── Collapsible admin sections (persist open state) ───────
    function initCollapsibleSections() {
        document.querySelectorAll('.admin-section').forEach(section => {
            const id = section.id;
            if (!id) return;
            const heading = section.querySelector('.admin-section-title');
            if (!heading || heading.dataset.collapsible === '1') return;
            heading.dataset.collapsible = '1';
            heading.style.cursor = 'pointer';
            heading.setAttribute('role', 'button');
            heading.setAttribute('tabindex', '0');
            heading.setAttribute('aria-expanded',
                localStorage.getItem('section-' + id) !== 'closed');

            const body = Array.from(section.children)
                .filter(el => el !== heading);
            const setOpen = (open) => {
                body.forEach(el => { el.style.display = open ? '' : 'none'; });
                heading.setAttribute('aria-expanded', String(open));
                try {
                    localStorage.setItem('section-' + id,
                        open ? 'open' : 'closed');
                } catch (_) {}
            };

            const stored = localStorage.getItem('section-' + id);
            const initialOpen = stored !== 'closed';
            // apply once synchronously to avoid flash
            if (!initialOpen) body.forEach(el => { el.style.display = 'none'; });

            const toggle = () =>
                setOpen(heading.getAttribute('aria-expanded') !== 'true');
            heading.addEventListener('click', toggle);
            heading.addEventListener('keydown', e => {
                if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggle();
                }
            });
        });
    }

    // ── Notes-changed indicator on green rows ───────────────────────
    function initNotesIndicators() {
        document.querySelectorAll('.status-row').forEach(row => {
            refreshNotesDot(row);
        });
        // keep in sync while typing
        document.addEventListener('input', e => {
            if (e.target.matches('textarea.notes-input'))
                refreshNotesDot(e.target.closest('.status-row'));
        });
    }

    function refreshNotesDot(row) {
        if (!row) return;
        const ta = row.querySelector('.notes-input');
        const dot = row.querySelector('.status-dot');
        if (!ta || !dot) return;
        const isGreen = dot.classList.contains('green');
        const hasText = ta.value.trim().length > 0;
        let ind = row.querySelector('.notes-indicator');
        if (isGreen && hasText) {
            if (!ind) {
                ind = document.createElement('span');
                ind.className = 'notes-indicator';
                ind.title = 'This service has notes';
                ind.setAttribute('aria-label', 'Has notes');
                ind.style.cssText = 'cursor:pointer;margin-left:6px;';
                ind.textContent = '📝';
                ind.onclick = () => row.classList.toggle('show-notes');
                row.querySelector('.status-main').appendChild(ind);
            }
            ind.style.display = '';
        } else if (ind) {
            ind.remove();
        }
    }

    // ── Boot ────────────────────────────────────────────────────────
    document.addEventListener('DOMContentLoaded', () => {
        initCollapsibleSections();
        initNotesIndicators();
        startAutoRefresh();
        armSessionWatch();
    });

    // Re-run indicator scan when admin adds/removes rows
    window.refreshNotesIndicators = () =>
        document.querySelectorAll('.status-row').forEach(refreshNotesDot);
})();
