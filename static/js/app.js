// ── Auth modal ────────────────────────────────────────────────────
const loginBtn = document.getElementById('loginBtn');
const loginModal = document.getElementById('loginModal');
const cancelLogin = document.getElementById('cancelLogin');
const loginError = document.getElementById('loginError');
const loginForm = document.getElementById('loginForm');

if (loginBtn) {
    loginBtn.addEventListener('click', () => {
        loginModal.classList.remove('hidden');
        setTimeout(() => document.getElementById('userInput').focus(), 50);
    });
}
if (cancelLogin) {
    cancelLogin.addEventListener('click', () => {
        loginModal.classList.add('hidden');
        loginError.classList.add('hidden');
        document.getElementById('passInput').value = '';
    });
}
loginModal && loginModal.addEventListener('click', e => { if (e.target === loginModal) cancelLogin.click(); });

loginForm && loginForm.addEventListener('submit', async e => {
    e.preventDefault();
    const user = document.getElementById('userInput').value;
    const pass = document.getElementById('passInput').value;
    try {
        const res = await fetch('/login', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ user, pass })
        });
        if (res.ok) location.reload(); else loginError.classList.remove('hidden');
    } catch (err) { loginError.textContent = 'Connection error'; loginError.classList.remove('hidden'); }
});

// Logout
document.getElementById('logoutBtn') && document.getElementById('logoutBtn').addEventListener('click', async () => {
    await csrfFetch('/logout', { method: 'POST' }); location.reload();
});

// ── Toggle on status-main click (admin only) ──────────────────────
const list = document.getElementById('statusList');
const STATUS_CYCLE = ['green', 'degraded', 'red'];
const STATUS_LABELS = { green: 'Operational', degraded: 'Degraded', red: 'Outage' };

list && list.addEventListener('click', async e => {
    const main = e.target.closest('.status-main');
    if (!main || !document.body.classList.contains('admin')) return;
    // Don't toggle if clicking the drag handle, delete, clear-history, or history buttons
    if (e.target.closest('.drag-handle') || e.target.closest('.btn-delete') || e.target.closest('.btn-history') || e.target.closest('.btn-history-clear')) return;

    const row = main.closest('.status-row');
    const id = row.dataset.id;
    const dot = main.querySelector('.status-dot');
    const label = main.querySelector('.status-label');
    const current = STATUS_CYCLE.find(s => dot.classList.contains(s)) || 'green';
    const next = STATUS_CYCLE[(STATUS_CYCLE.indexOf(current) + 1) % STATUS_CYCLE.length];
    dot.className = 'status-dot ' + next;
    label.className = 'status-label ' + next;
    label.textContent = STATUS_LABELS[next];
    row.classList.toggle('show-notes', next !== 'green');
    try { await csrfFetch('/api/toggle/' + id, { method: 'POST' }); } catch (err) { location.reload(); }
    updateBadge();
});

// ── Notes auto-save on blur (admin only), debounce 800ms ─────────
var timers = {};
if (list) {
    // Delegated auto-grow on input
    list.addEventListener('input', e => {
        if (e.target.matches('textarea.notes-input')) {
            e.target.style.height = 'auto';
            e.target.style.height = e.target.scrollHeight + 'px';
        }
    });

    // Delegated auto-save on focusout (blur)
    list.addEventListener('focusout', e => {
        if (!document.body.classList.contains('admin')) return;
        if (e.target.matches('textarea.notes-input')) {
            const ta = e.target;
            const id = ta.dataset.id;
            if (timers[id]) clearTimeout(timers[id]);
            timers[id] = setTimeout(async () => {
                try {
                    await csrfFetch('/api/notes/' + id, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ notes: ta.value })
                    });
                } catch (_) {}
            }, 800);
        }
    });
}

// Initial height adjustment for existing textareas on load
document.querySelectorAll('textarea.notes-input').forEach(ta => {
    ta.style.height = 'auto';
    ta.style.height = ta.scrollHeight + 'px';
});

// ── Delete item (admin only) ─────────────────────────────────────
list && list.addEventListener('click', async e => {
    const btn = e.target.closest('.btn-delete');
    if (!btn || !document.body.classList.contains('admin')) return;
    e.stopPropagation();
    const row = btn.closest('.status-row');
    const id = btn.dataset.id;
    const name = row.querySelector('.status-name').textContent;

    try {
        const res = await csrfFetch('/api/delete/' + id, { method: 'POST' });
        if (!res.ok) throw new Error(await res.text());
        row.style.transition = 'opacity 0.3s, transform 0.3s';
        row.style.opacity = '0';
        row.style.transform = 'translateX(-20px)';
        setTimeout(() => { row.remove(); updateBadge(); }, 300);
    } catch (err) { alert('Failed to delete: ' + err.message); }
});

// ── Add item (admin only) ────────────────────────────────────────
const addItemForm = document.getElementById('addItemForm');
if (addItemForm) {
    addItemForm.addEventListener('submit', async e => {
        e.preventDefault();
        const input = document.getElementById('newItemName');
        const name = input.value.trim();
        if (!name) return;

        try {
            const res = await csrfFetch('/api/add', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name })
            });
            if (!res.ok) throw new Error((await res.json()).error || 'Failed');

            // Appending DOM row client-side is instant.
            const item = (await res.json()).item;
            const row = document.createElement('div');
            row.className = 'status-row';
            row.dataset.id = item.id;
            row.draggable = true;

            // Build row via DOM to avoid any double-encoding risk from escHtml
            row.innerHTML = `
                <div class="drag-handle" title="Drag to reorder">⠿</div>
                <div class="status-main">
                    <span class="status-dot green"></span>
                    <span class="status-name"></span>
                    <span class="status-label green">Operational</span>
                </div>
                <textarea class="notes-input" placeholder="Add status notes…" data-id="${item.id}"></textarea>
                <button class="btn-history" title="View history" data-id="${item.id}">🕙</button>
                <button class="btn-history-clear" title="Clear history for this service (admin)" data-id="${item.id}">🧹</button>
                <button class="btn-delete" title="Delete this item" data-id="${item.id}">✕</button>
            `;
            row.querySelector('.status-name').textContent = item.name;
            row.querySelector('.notes-input').textContent = item.notes || '';
            list.appendChild(row);

            input.value = '';
            updateBadge();
        } catch (err) { alert(err.message); }
    });
}

// ── Drag-and-drop reorder (admin only) ───────────────────────────
let dragSourceRow = null;

list && list.addEventListener('dragstart', e => {
    if (!document.body.classList.contains('admin')) return;
    const handle = e.target.closest('.drag-handle');
    const row = e.target.closest('.status-row');
    if (!row || !handle) return; // only allow drag from handle

    dragSourceRow = row;
    setTimeout(() => { dragSourceRow.classList.add('dragging'); }, 0);
    e.dataTransfer.effectAllowed = 'move';
});

list && list.addEventListener('dragend', e => {
    if (!dragSourceRow) return;
    dragSourceRow.classList.remove('dragging');
    // Clear all drop indicators
    list.querySelectorAll('.status-row').forEach(r => {
        r.classList.remove('drag-over-top', 'drag-over-bottom');
    });
    dragSourceRow = null;

    // Persist new order if rows moved
    sendReorder();
});

list && list.addEventListener('dragover', e => {
    if (!dragSourceRow || !document.body.classList.contains('admin')) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';

    const targetRow = e.target.closest('.status-row');
    if (!targetRow || targetRow === dragSourceRow) return;

    // Clear previous indicators
    list.querySelectorAll('.status-row').forEach(r => {
        r.classList.remove('drag-over-top', 'drag-over-bottom');
    });

    const rect = targetRow.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
        targetRow.classList.add('drag-over-top');
    } else {
        targetRow.classList.add('drag-over-bottom');
    }
});

list && list.addEventListener('drop', e => {
    if (!dragSourceRow || !document.body.classList.contains('admin')) return;
    e.preventDefault();

    const targetRow = e.target.closest('.status-row');
    if (!targetRow || targetRow === dragSourceRow) return;

    // Clear indicators
    list.querySelectorAll('.status-row').forEach(r => {
        r.classList.remove('drag-over-top', 'drag-over-bottom');
    });

    const rect = targetRow.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
        // Insert before target
        list.insertBefore(dragSourceRow, targetRow);
    } else {
        // Insert after target
        list.insertBefore(dragSourceRow, targetRow.nextSibling);
    }

    dragSourceRow = null;
});

// ── Helpers ───────────────────────────────────────────────
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

/** CSRF-protected fetch — adds X-CSRF-Token header; rotates token on success. */
async function csrfFetch(url, options = {}) {
    const token = _csrfToken();
    if (!options.headers) options.headers = {};
    if (token) options.headers['X-CSRF-Token'] = token;
    const res = await fetch(url, options);

    // 403 means the session is invalid — expired after 5 min idle, logged
    // out, or CSRF mismatch. Reload to drop back to the login UI.
    if (res.status === 403) {
        location.reload();
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

function sendReorder() {
    if (!document.body.classList.contains('admin')) return;
    const order = {};
    list.querySelectorAll('.status-row').forEach((row, i) => {
        order[row.dataset.id] = i;
    });
    csrfFetch('/api/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order })
    }).catch(() => location.reload());
}

// ── Overall badge ─────────────────────────────────────────────────
function updateBadge() {
    var total = document.querySelectorAll('.status-row').length;
    var reds = document.querySelectorAll('.status-dot.red').length;
    var degraded = document.querySelectorAll('.status-dot.degraded').length;
    var b = document.getElementById('overallBadge');
    if (!b) return;
    if (reds === 0 && degraded === 0) {
        b.textContent = 'All Systems Operational \u2014 ' + total + ' services';
        b.className = 'overall-badge';
    } else if (reds > 0) {
        b.textContent = reds + ' outage(s), ' + degraded + ' degraded of ' + total + ' services';
        b.className = 'overall-badge red';
    } else {
        b.textContent = degraded + ' service(s) degraded of ' + total + ' services';
        b.className = 'overall-badge degraded';
    }
}
updateBadge();

// ── History modal (always visible — public read) ───────────────
const historyModal = document.getElementById('historyModal');
const historyTitle = document.getElementById('historyTitle');
const historyTimeline = document.getElementById('historyTimeline');
const closeHistory = document.getElementById('closeHistory');

async function openHistory(itemId) {
    try {
        const res = await fetch('/api/history/' + itemId);
        if (!res.ok) return;
        const data = await res.json();
        historyModalItemId = itemId;

        historyTitle.textContent = data.service + ' — History';
        historyTimeline.innerHTML = '';

        if (data.entries.length === 0) {
            historyTimeline.innerHTML = '<div class="history-empty">No history yet</div>';
        } else {
            data.entries.forEach(entry => {
                const el = document.createElement('div');
                el.className = 'history-entry';

                // Icon element (server-controlled, safe to innerHTML)
                const iconHtml = entry.event_type === 'status'
                    ? '<span class="history-icon status">&#x25cf;</span>'
                    : '<span class="history-icon notes">&#xE74B;</span>';
                el.innerHTML = iconHtml;

                // Details container (created via DOM to ensure textContent for user-controlled values)
                const details = document.createElement('div');
                details.className = 'history-details';

                const labelSpan = document.createElement('span');
                labelSpan.className = 'history-label';
                if (entry.event_type === 'status') {
                    labelSpan.textContent = entry.old_value + ' \u2192 ' + entry.new_value;
                    details.appendChild(labelSpan);
                } else {
                    labelSpan.textContent = 'Notes updated';
                    details.appendChild(labelSpan);

                    const notesSpan = document.createElement('span');
                    notesSpan.className = 'history-notes-text';
                    if (entry.new_value) notesSpan.textContent = entry.new_value;  // textContent auto-escapes
                    details.appendChild(notesSpan);
                }

                el.appendChild(details);

                // Time element (timestamp from server, but still sanitize for display)
                const timeEl = document.createElement('time');
                timeEl.className = 'history-time';
                const d = new Date(entry.occurred + (entry.occurred.endsWith('Z') ? '' : 'Z'));
                timeEl.textContent = d.toLocaleString(undefined, {
                    year: 'numeric', month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit'
                });
                el.appendChild(timeEl);

                historyTimeline.appendChild(el);
            });
        }

        historyModal.classList.remove('hidden');
    } catch (err) {
        console.error('Failed to load history:', err);
    }
}

function closeHistoryModal() {
    historyModal.classList.add('hidden');
    historyTimeline.innerHTML = '';
    historyModalItemId = null;
}

// Close on button, backdrop click, Escape key
closeHistory && closeHistory.addEventListener('click', closeHistoryModal);
historyModal && historyModal.addEventListener('click', e => {
    if (e.target === historyModal) closeHistoryModal();
});
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !historyModal.classList.contains('hidden')) {
        closeHistoryModal();
    }
});

// "Clear history" button in the modal header (admin only)
const clearHistoryBtn = document.getElementById('clearHistory');
clearHistoryBtn && document.body.classList.contains('admin') && clearHistoryBtn.classList.remove('hidden');
clearHistoryBtn && clearHistoryBtn.addEventListener('click', async () => {
    if (!historyModalItemId) return;
    const ok = await clearHistoryData(historyModalItemId, null);
    if (ok) {
        const btnEl = list.querySelector('.btn-history-clear[data-id="' + historyModalItemId + '"]');
        if (btnEl) btnEl.title = 'Cleared \u00B7 click again to clear more';
    }
});

// Delegate click on history button via the status list
list && list.addEventListener('click', async e => {
    const btn = e.target.closest('.btn-history');
    if (!btn) return;
    e.stopPropagation();
    const id = btn.dataset.id;
    openHistory(id);
});

// ── Clear history (admin only) ──────────────────────────────────
let historyModalItemId = null;  // service whose timeline the modal currently shows

// Shared action: wipe a service's timeline, refresh the modal if it's open
async function clearHistoryData(id, btn) {
    const name = (btn.closest('.status-row')?.querySelector('.status-name')?.textContent) || ('#' + id);
    if (!confirm('Clear all history for \u201C' + name + '\u201D?\nThis cannot be undone.')) return false;
    if (btn) btn.disabled = true;
    try {
        const res = await csrfFetch('/api/history/' + id + '/clear', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to clear history');
        if (historyModalItemId === id) await openHistory(id);  // refresh open modal
        if (btn) btn.title = 'Cleared ' + (data.removed || 0) + ' entr' + ((data.removed || 0) === 1 ? 'y' : 'ies') + ' \u00B7 click again to clear more';
        return true;
    } catch (err) {
        alert('Failed to clear history: ' + err.message);
        return false;
    } finally {
        if (btn) btn.disabled = false;
    }
}

// Per-row clear button (admin rows)
list && list.addEventListener('click', e => {
    const btn = e.target.closest('.btn-history-clear');
    if (!btn || !document.body.classList.contains('admin')) return;
    e.stopPropagation();
    clearHistoryData(btn.dataset.id, btn);
});


// ── Show notes for non-green rows on page load ───────────────────
document.querySelectorAll('.status-row').forEach(function(row) {
    var dot = row.querySelector('.status-dot');
    if (dot && (dot.classList.contains('degraded') || dot.classList.contains('red'))) {
        row.classList.add('show-notes');
    }
});

// ── Theme toggle (user-selectable light / dark mode) ─────────────
// Preference persists per-browser in localStorage under 'theme'.
// The inline script in index.html applies it before first paint so
// returning users never see the default theme flash.
function currentTheme() {
    return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

function syncThemeUI() {
    if (!themeToggle) return;
    var isLight = currentTheme() === 'light';
    themeToggle.textContent = isLight ? '🌙 Dark mode' : '☀️ Light mode';
    themeToggle.setAttribute('aria-label', isLight ? 'Switch to dark mode' : 'Switch to light mode');
}

function setTheme(theme) {
    var root = document.documentElement;
    if (theme === 'light') root.setAttribute('data-theme', 'light');
    else root.removeAttribute('data-theme');
    try {
        if (theme === 'light') localStorage.setItem('theme', 'light');
        else localStorage.removeItem('theme');
    } catch (e) { /* localStorage unavailable — theme is view-only in that case */ }
    syncThemeUI();
}

const themeToggle = document.getElementById('themeToggle');
themeToggle && themeToggle.addEventListener('click', () => {
    setTheme(currentTheme() === 'light' ? 'dark' : 'light');
});
// Sync button label with the theme already applied by the inline boot script
syncThemeUI();

// ── Settings: history button on/off (admin only) ──────────────────
const historyEnabled = document.getElementById('historyEnabled');
const historyState = document.getElementById('historyState');

historyEnabled && historyEnabled.addEventListener('change', async () => {
    if (!document.body.classList.contains('admin')) return;
    const target = historyEnabled.checked;
    if (historyState) historyState.textContent = 'Saving…';
    historyEnabled.disabled = true;
    try {
        const res = await csrfFetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ history_enabled: target })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'unknown');
        // Apply locally: swap rows' history buttons, no full reload needed.
        list && list.querySelectorAll('.status-row').forEach(row => {
            const id = row.dataset.id;
            const existing = row.querySelector('.btn-history');
            if (target) {
                if (!existing) {
                    const btn = document.createElement('button');
                    btn.className = 'btn-history';
                    btn.title = 'View history';
                    btn.dataset.id = id;
                    btn.textContent = '🕙';
                    const del = row.querySelector('.btn-delete');
                    del ? row.insertBefore(btn, del) : row.appendChild(btn);
                }
            } else if (existing) {
                existing.remove();
            }
        });
        // Also hide the history modal if it's open against a removed button.
        closeHistoryModal();
        if (historyState) historyState.textContent = target ? 'Enabled' : 'Disabled';
    } catch (err) {
        if (historyState) historyState.textContent = 'Error: ' + err.message;
        historyEnabled.checked = !target;  // revert to the previous state
    } finally {
        historyEnabled.disabled = false;
    }
});

// ── Settings: healthchecks on/off (admin only) ──────────────────
const healthchecksEnabled = document.getElementById('healthchecksEnabled');
const healthchecksState = document.getElementById('healthchecksState');

healthchecksEnabled && healthchecksEnabled.addEventListener('change', async () => {
    if (!document.body.classList.contains('admin')) return;
    const target = healthchecksEnabled.checked;
    if (healthchecksState) healthchecksState.textContent = 'Saving…';
    healthchecksEnabled.disabled = true;
    try {
        const res = await csrfFetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ healthchecks_enabled: target })
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'unknown');
        if (healthchecksState) healthchecksState.textContent = target ? 'Enabled' : 'Disabled';
    } catch (err) {
        if (healthchecksState) healthchecksState.textContent = 'Error: ' + err.message;
        healthchecksEnabled.checked = !target;
    } finally {
        healthchecksEnabled.disabled = false;
    }
});
