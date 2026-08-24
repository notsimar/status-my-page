// ── Auth modal ────────────────────────────────────────────────────
const loginBtn = document.getElementById('loginBtn');
const loginModal = document.getElementById('loginModal');
const cancelLogin = document.getElementById('cancelLogin');
const loginError = document.getElementById('loginError');
const loginForm = document.getElementById('loginForm');

if (loginBtn) {
    loginBtn.addEventListener('click', () => {
    setTimeout(() => document.getElementById('userInput')?.focus(), 50);
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


// ── Keyboard reorder (Alt+ArrowUp/Down on a focused row) ─────────
function moveRow(row, dir) {
    if (!row || !document.body.classList.contains('admin')) return;
    const sibling = dir === 'up' ? row.previousElementSibling
                                 : row.nextElementSibling;
    if (!sibling) return;
    row.parentNode.insertBefore(dir === 'up' ? row : sibling,
                                dir === 'up' ? sibling : row.nextElementSibling);
    row.focus();
    scheduleKeyboardPersist();
}

// ── Animated re-sort after a status change (admin only) ──────────
// Server policy: red items first, then degraded, then green; position
// within each group otherwise. After a dot click moves an item between
// groups, slide it to its new spot so users see it push others around.
const STATUS_RANK = { red: 0, degraded: 1, green: 2 };

function resortRowsAnimated(movedRow) {
    if (!list || !document.body.classList.contains('admin')) return;
    const rows = [...list.querySelectorAll('.status-row')];
    // Renumber positions by current DOM order so ties stay stable.
    rows.forEach((r, i) => { r.dataset.serverPos = i; });
    // A status change moves the item to the EDGE of its new group:
    // red/degraded -> front of that group; green -> end of the greens.
    // This mirrors the server-side toggle repositioning.
    const movedStatus = movedRow
        ? (STATUS_CYCLE.find(s => movedRow.querySelector('.status-dot').classList.contains(s)) || 'green')
        : null;
    const key = (row) => {
        const status = STATUS_CYCLE.find(s => row.querySelector('.status-dot').classList.contains(s)) || 'green';
        let rank = STATUS_RANK[status];
        let pos = parseInt(row.dataset.serverPos, 10) || 0;
        if (movedRow && row === movedRow && movedStatus === 'green') {
            pos = Number.MAX_SAFE_INTEGER;   // recovering: bottom of the greens
        }
        return [rank, pos];
    };
    const sorted = [...rows].sort((a, b) => {
        const ka = key(a), kb = key(b);
        return ka[0] - kb[0] || ka[1] - kb[1];
    });
    if (movedRow && movedStatus && movedStatus !== 'green') {
        // incident: place the moved row at the FRONT of its new group
        const group = sorted.filter(r => key(r)[0] === STATUS_RANK[movedStatus]);
        const idx = sorted.indexOf(movedRow);
        const firstOfGroup = sorted.indexOf(group[0]);
        if (idx !== -1 && firstOfGroup !== -1 && idx !== firstOfGroup) {
            sorted.splice(idx, 1);
            sorted.splice(firstOfGroup, 0, movedRow);
        }
    }
    if (sorted.every((r, i) => r === rows[i])) return;  // order unchanged

    // FLIP animation: measure first positions, reorder DOM, then play transforms.
    const first = new Map(rows.map(r => [r, r.getBoundingClientRect().top]));
    rows.forEach(r => r.parentNode.appendChild(r));  // detach-free baseline reset
    sorted.forEach(r => list.appendChild(r));
    sorted.forEach(r => {
        const dy = (first.get(r) || 0) - r.getBoundingClientRect().top;
        if (!dy) return;
        r.style.transition = 'none';
        r.style.transform = 'translateY(' + dy + 'px)';
        requestAnimationFrame(() => {
            r.style.transition = 'transform 300ms ease';
            r.style.transform = '';
            setTimeout(() => { r.style.transition = ''; }, 350);
        });
    });
}

let kbTimer = null;
function scheduleKeyboardPersist() {
    clearTimeout(kbTimer);
    kbTimer = setTimeout(() => {
        const rows = [...document.querySelectorAll('.status-row')];
        const order = {};
        rows.forEach((r, i) => { order[r.dataset.id] = i; });
        csrfFetch('/api/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order })
        }).catch(() => window.showToast && showToast('Reorder failed', 'error'));
    }, 500);
}

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

    // Debounce guard: ignore clicks while a previous toggle is still in
    // flight — rapid clicking previously fired overlapping requests whose
    // 409/429 rejections were invisible, making the final state ambiguous.
    if (main.dataset.toggleInflight === '1') {
        // Revert the optimistic paint and tell the user why.
        dot.className = 'status-dot ' + current;
        label.className = 'status-label ' + current;
        label.textContent = STATUS_LABELS[current];
        row.classList.remove('show-notes', next !== 'green');
        showToggleNotice(row, 'Previous change still saving — one click at a time.');
        return;
    }
    main.dataset.toggleInflight = '1';

    try {
        const res = await csrfFetch('/api/toggle/' + id, { method: 'POST' });
        if (res.status === 409 || res.status === 429) {
            // Rate-limited: revert optimistic paint with an explanation.
            dot.className = 'status-dot ' + current;
            label.className = 'status-label ' + current;
            label.textContent = STATUS_LABELS[current];
            row.classList.remove('show-notes', next !== 'green');
            showToggleNotice(row, 'Too many changes too fast — wait a moment and retry.');
        } else if (next === 'green') {
            // Recovered: drop any stale incident note so it never resurfaces
            // when the item degrades again. The notes field is hidden for
            // green rows, so the admin would have no way to clear it.
            const notesTa = row.querySelector('.notes-input');
            if (notesTa) notesTa.value = '';
            csrfFetch('/api/notes/' + id, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ notes: '' })
            }).catch(() => {});
        } else {
            // Status group changed: slide the row to its new position so the
            // user sees it push other rows up/down (server policy: red →
            // degraded → green, then drag order within each group).
            resortRowsAnimated(row);
        }
    } catch (err) {
        location.reload();
    } finally {
        delete main.dataset.toggleInflight;
    }
    updateBadge();
});

// Transient inline notice under a status row (auto-dismisses).
function showToggleNotice(row, text) {
    let n = row.querySelector('.toggle-notice');
    if (!n) {
        n = document.createElement('div');
        n.className = 'toggle-notice';
        n.style.cssText = 'color:#c0392b;font-size:0.78rem;margin-top:2px;';
        row.appendChild(n);
    }
    n.textContent = text;
    setTimeout(() => n.remove(), 5000);
}

list.addEventListener('keydown', e => {
    if (!e.altKey) return;
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    const main = e.target.closest('.status-main');
    if (!main) return;
    const row = main.closest('.status-row');
    e.preventDefault();
    moveRow(row, e.key === 'ArrowUp' ? 'up' : 'down');
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
            // Surface server-side length cap before save: trim + warn once.
            const MAX_NOTES = 2000;
            if (ta.value.length > MAX_NOTES) {
                ta.value = ta.value.slice(0, MAX_NOTES);
                let warn = ta.parentElement.querySelector('.notes-length-warn');
                if (!warn) {
                    warn = document.createElement('div');
                    warn.className = 'notes-length-warn';
                    warn.style.cssText = 'color:#c0392b;font-size:0.78rem;margin-top:2px;';
                    ta.insertAdjacentElement('afterend', warn);
                }
                warn.textContent = 'Note limited to ' + MAX_NOTES + ' characters — extra text removed.';
                setTimeout(() => warn.remove(), 6000);
            }
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
document.querySelectorAll('.status-row').forEach(r => {
    r.setAttribute('tabindex', '0');
});

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

    const confirmed = await uxConfirm(
        'Delete service \u201C' + name + '\u201D?\n' +
        'Its status history will be removed too. This cannot be undone.',
        { okLabel: 'Delete', danger: true });
    if (!confirmed) return;

    try {
        const res = await csrfFetch('/api/delete/' + id, { method: 'POST' });
        if (!res.ok) throw new Error(await res.text());
        row.style.transition = 'opacity 0.3s, transform 0.3s';
        row.style.opacity = '0';
        row.style.transform = 'translateX(-20px)';
        setTimeout(() => {
            row.remove();
            updateBadge();
            window.refreshNotesIndicators &&
                window.refreshNotesIndicators();
            window.showToast('Deleted \u201C' + name + '\u201D', 'success');
        }, 300);
    } catch (err) {
        window.showToast('Failed to delete: ' + err.message, 'error');
    }
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
            window.showToast && window.showToast('Added "' + name + '"', 'success');
            window.refreshNotesIndicators && window.refreshNotesIndicators();

            // Appending DOM row client-side is instant.
            const item = (await res.json()).item;
            const row = document.createElement('div');
            row.className = 'status-row';
            row.dataset.id = item.id;

            // Build row via DOM to avoid any double-encoding risk from escHtml
            row.innerHTML = `
                <div class="drag-handle" title="Drag to reorder">⠿</div>
                <div class="status-main">
                    <span class="status-dot green"></span>
                    <span class="status-name"></span>
                    <span class="status-label green">Operational</span>
                </div>
                <textarea class="notes-input" placeholder="Add status notes…" maxlength="2000" data-id="${item.id}"></textarea>
                <button class="btn-history" title="View history" data-id="${item.id}">🕙</button>
                <button class="btn-history-clear" title="Clear history for this service (admin)" data-id="${item.id}">🧹</button>
                <button class="btn-delete" title="Delete this item" data-id="${item.id}">✕</button>
            `;
            row.querySelector('.status-name').textContent = item.name;
            row.querySelector('.notes-input').textContent = item.notes || '';
            list.appendChild(row);

            input.value = '';
            updateBadge();
        } catch (err) { window.showToast(err.message, 'error'); }
    });
}

// ── Drag-to-reorder (admin only, pointer events) ─────────────────
// Uses pointer events instead of HTML5 drag-and-drop: HTML5 DnD is
// unreliable (doesn't initiate on some touchpads/browsers/touchscreens
// and can't be driven reliably). Pointer-based dragging behaves the same
// everywhere and lets us displace rows live while dragging.
let dragRow = null;        // row being dragged
let dragStartY = 0;        // pointer Y at grab
let didReorder = false;

function pointerReorderAt(clientY) {
    // Find the row under the pointer and swap positions when its midpoint
    // is crossed. The displaced row slides via CSS transition.
    const rows = [...list.querySelectorAll('.status-row')];
    for (const other of rows) {
        if (other === dragRow) continue;
        const rect = other.getBoundingClientRect();
        if (clientY >= rect.top && clientY <= rect.bottom) {
            const before = other.getBoundingClientRect().top;
            if (clientY < rect.top + rect.height / 2) {
                list.insertBefore(dragRow, other);             // push target down
            } else {
                list.insertBefore(dragRow, other.nextSibling); // pull target up
            }
            if (other.previousElementSibling === dragRow || other.nextElementSibling === dragRow) {
                didReorder = true;
            }
            const dy = before - other.getBoundingClientRect().top;
            if (dy) {
                other.style.transition = 'none';
                other.style.transform = 'translateY(' + dy + 'px)';
                requestAnimationFrame(() => {
                    other.style.transition = 'transform 150ms ease';
                    other.style.transform = '';
                    setTimeout(() => { other.style.transition = ''; }, 200);
                });
            }
            break;
        }
    }
}

list && list.addEventListener('pointerdown', e => {
    if (!document.body.classList.contains('admin')) return;
    if (!e.isPrimary) return;
    const handle = e.target.closest('.drag-handle');
    const row = e.target.closest('.status-row');
    if (!row || !handle) return;

    // A previous interrupted drag may have left a stale .dragging class.
    list.querySelectorAll('.status-row.dragging').forEach(r => {
        if (r !== row) r.classList.remove('dragging');
    });

    dragRow = row;
    dragStartY = e.clientY;
    didReorder = false;
    handle.setPointerCapture(e.pointerId);
});

list && list.addEventListener('pointermove', e => {
    if (!dragRow) return;
    // Small threshold so a plain click doesn't count as a drag.
    if (!dragRow.classList.contains('dragging')) {
        if (Math.abs(e.clientY - dragStartY) < 4) return;
        dragRow.classList.add('dragging');
    }
    pointerReorderAt(e.clientY);
});

function endPointerDrag(e) {
    if (!dragRow) return;
    dragRow.classList.remove('dragging');
    list.querySelectorAll('.status-row').forEach(r => r.classList.remove('drag-over-top', 'drag-over-bottom'));
    if (didReorder) sendReorder();
    dragRow = null;
    didReorder = false;
}
list && list.addEventListener('pointerup', endPointerDrag);
list && list.addEventListener('pointercancel', endPointerDrag);

// ── Helpers ───────────────────────────────────────────────
// CSRF token + csrfFetch live in static/js/csrf.js (shared + disambiguated)

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
                timeEl.textContent = window.timeAgo
                    ? timeAgo(d.toISOString())
                    : d.toLocaleString();
                timeEl.title = d.toLocaleString(undefined, {
                    year: 'numeric', month: 'short', day: 'numeric',
                    hour: '2-digit', minute: '2-digit'
                });   // absolute on hover for incident timelines
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
    const name = btn
        ? (btn.closest('.status-row')?.querySelector('.status-name')?.textContent || ('#' + id))
        : ((historyTitle?.textContent || '').replace(' \u2014 History', '') || ('#' + id));
    const confirmed = await uxConfirm(
        'Clear all history for \u201C' + name + '\u201D? This cannot be undone.',
        { okLabel: 'Clear history', danger: true });
    if (!confirmed) return false;
    if (btn) btn.disabled = true;
    try {
        const res = await csrfFetch('/api/history/' + id + '/clear', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || 'Failed to clear history');
        if (historyModalItemId === id) await openHistory(id);  // refresh open modal
        if (btn) btn.title = 'Cleared ' + (data.removed || 0) + ' entr' + ((data.removed || 0) === 1 ? 'y' : 'ies') + ' \u00B7 click again to clear more';
        return true;
    } catch (err) {
        window.showToast('Failed to clear history: ' + err.message, 'error');
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
        // Persist BOTH choices explicitly so the saved value always mirrors
        // what's on screen ('dark' previously relied on removeItem + default,
        // which made the stored state ambiguous during rapid toggles).
        localStorage.setItem('theme', theme);
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
