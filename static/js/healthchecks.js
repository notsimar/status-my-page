// ── Healthcheck Admin UI ───────────────────────────────────────────
// Handles the healthcheck admin panel: list, create, edit, delete, run

// CSRF token helpers (shared with app.js)
function _csrfToken() {
    const el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') || '' : '';
}

function _setCsrfToken(token) {
    let el = document.querySelector('meta[name="csrf-token"]');
    if (!el) {
        el = document.createElement('meta');
        el.name = 'csrf-token';
        document.head.appendChild(el);
    }
    el.setAttribute('content', token);
}

async function csrfFetch(url, options = {}) {
    const token = _csrfToken();
    if (!options.headers) options.headers = {};
    if (token) options.headers['X-CSRF-Token'] = token;
    const res = await fetch(url, options);

    if (res.status === 403) {
        location.reload();
    }

    if (res.ok && res.status < 300) {
        try {
            const tokRes = await fetch('/api/csrf-token');
            if (tokRes.ok) {
                const data = await tokRes.json();
                if (data.token) _setCsrfToken(data.token);
            }
        } catch (_) { }
    }

    return res;
}

// ── DOM Elements ──────────────────────────────────────────────────
const healthcheckAdmin = document.getElementById('healthcheckAdmin');
const addHealthcheckBtn = document.getElementById('addHealthcheckBtn');
const healthcheckTable = document.getElementById('healthcheckTable');
const healthcheckTbody = document.getElementById('healthcheckTbody');
const healthcheckEmpty = document.getElementById('healthcheckEmpty');
const healthcheckModal = document.getElementById('healthcheckModal');
const closeHealthcheckModal = document.getElementById('closeHealthcheckModal');
const cancelHealthcheck = document.getElementById('cancelHealthcheck');
const healthcheckForm = document.getElementById('healthcheckForm');
const healthcheckError = document.getElementById('healthcheckError');
const healthcheckModalTitle = document.getElementById('healthcheckModalTitle');

// Form fields
const hcMode = document.getElementById('healthcheckMode');
const hcOriginalName = document.getElementById('healthcheckOriginalName');
const hcName = document.getElementById('hcName');
const hcType = document.getElementById('hcType');
const hcUrl = document.getElementById('hcUrl');
const hcHealthyCodes = document.getElementById('hcHealthyCodes');
const hcSoapAction = document.getElementById('hcSoapAction');
const hcBody = document.getElementById('hcBody');
const hcExpectedString = document.getElementById('hcExpectedString');
const hcFailureKeyword = document.getElementById('hcFailureKeyword');
const hcDegradedKeyword = document.getElementById('hcDegradedKeyword');
const hcHost = document.getElementById('hcHost');
const hcPort = document.getElementById('hcPort');
const hcRssKeywordsRed = document.getElementById('hcRssKeywordsRed');
const hcRssKeywordsDeg = document.getElementById('hcRssKeywordsDeg');
const hcInterval = document.getElementById('hcInterval');
const hcTimeout = document.getElementById('hcTimeout');
const hcRetries = document.getElementById('hcRetries');

// Type-specific field groups
const typeFieldGroups = {
    curl: ['hcUrlRow', 'hcHealthyCodesRow', 'hcFailureKeywordRow', 'hcDegradedKeywordRow'],
    soap: ['hcUrlRow', 'hcHealthyCodesRow', 'hcSoapActionRow', 'hcBodyRow', 'hcExpectedStringRow', 'hcFailureKeywordRow', 'hcDegradedKeywordRow'],
    ping: ['hcHostRow'],
    tcp: ['hcHostRow', 'hcPortRow'],
    rss: ['hcUrlRow', 'hcRssKeywordsRow', 'hcRssKeywordsDegRow'],
};

// ── Helpers ───────────────────────────────────────────────────────
function showError(message) {
    healthcheckError.textContent = message;
    healthcheckError.classList.remove('hidden');
}

function hideError() {
    healthcheckError.classList.add('hidden');
    healthcheckError.textContent = '';
}

function showFieldGroups(type) {
    // Hide all type-specific fields
    const allFields = ['hcUrlRow', 'hcHealthyCodesRow', 'hcSoapActionRow', 'hcBodyRow', 'hcExpectedStringRow', 'hcFailureKeywordRow', 'hcDegradedKeywordRow', 'hcHostRow', 'hcPortRow', 'hcRssKeywordsRow', 'hcRssKeywordsDegRow'];
    allFields.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.add('hidden');
    });

    // Show fields for this type
    const fields = typeFieldGroups[type] || [];
    fields.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.classList.remove('hidden');
    });

    // Update required attributes
    const urlInput = document.getElementById('hcUrl');
    const hostInput = document.getElementById('hcHost');
    const portInput = document.getElementById('hcPort');

    if (type === 'curl' || type === 'soap' || type === 'rss') {
        urlInput.required = true;
        hostInput.required = false;
        portInput.required = false;
    } else if (type === 'ping') {
        urlInput.required = false;
        hostInput.required = true;
        portInput.required = false;
    } else if (type === 'tcp') {
        urlInput.required = false;
        hostInput.required = true;
        portInput.required = true;
    } else {
        urlInput.required = false;
        hostInput.required = false;
        portInput.required = false;
    }
}

function _splitCsv(text) {
    return (text || '').split(',').map(s => s.trim()).filter(Boolean);
}

function _joinCsv(list) {
    return (list || []).join(', ');
}

function resetForm() {
    healthcheckForm.reset();
    hcMode.value = 'create';
    hcOriginalName.value = '';
    healthcheckModalTitle.textContent = 'Add Healthcheck';
    document.getElementById('submitHealthcheck').textContent = 'Save';
    hideError();
    showFieldGroups('');
}

function openModal(mode, data = null) {
    resetForm();
    if (mode === 'edit' && data) {
        hcMode.value = 'edit';
        hcOriginalName.value = data.name;
        healthcheckModalTitle.textContent = 'Edit Healthcheck';
        document.getElementById('submitHealthcheck').textContent = 'Update';

        hcName.value = data.name;
        hcType.value = data.type || '';

        if (data.type === 'curl' || data.type === 'soap' || data.type === 'rss') {
            hcUrl.value = data.url || '';
            if (data.type === 'rss') {
                hcRssKeywordsRed.value = _joinCsv((data.keywords || {}).red);
                hcRssKeywordsDeg.value = _joinCsv((data.keywords || {}).degraded);
            } else {
                hcHealthyCodes.value = (data.healthy_codes || []).join(', ');
                hcFailureKeyword.value = data.failure_keyword || '';
                hcDegradedKeyword.value = data.degraded_keyword || '';
            }
            if (data.type === 'soap') {
                hcSoapAction.value = data.soap_action || '';
                hcBody.value = data.body || '';
                hcExpectedString.value = data.expected_string || '';
            }
        } else if (data.type === 'ping') {
            hcHost.value = data.host || '';
        } else if (data.type === 'tcp') {
            hcHost.value = data.host || '';
            hcPort.value = data.port || '';
        }

        hcInterval.value = data.interval || 60;
        hcTimeout.value = data.timeout || 10;
        hcRetries.value = data.retries || 2;
    }
    showFieldGroups(hcType.value);
    healthcheckModal.classList.remove('hidden');
    setTimeout(() => hcName.focus(), 50);
}

function closeModal() {
    healthcheckModal.classList.add('hidden');
    resetForm();
}

function collectFormData() {
    const type = hcType.value;
    const data = {
        name: hcName.value.trim(),
        type: type,
    };

    if (type === 'curl' || type === 'soap' || type === 'rss') {
        data.url = hcUrl.value.trim();
        if (type === 'rss') {
            const red = _splitCsv(hcRssKeywordsRed.value);
            const deg = _splitCsv(hcRssKeywordsDeg.value);
            if (red.length > 0 || deg.length > 0) {
                data.keywords = {};
                if (red.length > 0) data.keywords.red = red;
                if (deg.length > 0) data.keywords.degraded = deg;
            }
        } else {
            const codes = hcHealthyCodes.value.split(',').map(c => parseInt(c.trim(), 10)).filter(c => !isNaN(c));
            if (codes.length > 0) data.healthy_codes = codes;
            const fk = hcFailureKeyword.value.trim();
            if (fk) data.failure_keyword = fk;
            const dk = hcDegradedKeyword.value.trim();
            if (dk) data.degraded_keyword = dk;
        }
        if (type === 'soap') {
            const sa = hcSoapAction.value.trim();
            if (sa) data.soap_action = sa;
            const body = hcBody.value.trim();
            if (body) data.body = body;
            const es = hcExpectedString.value.trim();
            if (es) data.expected_string = es;
        }
    } else if (type === 'ping') {
        data.host = hcHost.value.trim();
    } else if (type === 'tcp') {
        data.host = hcHost.value.trim();
        data.port = parseInt(hcPort.value, 10);
    }

    data.interval = parseInt(hcInterval.value, 10);
    data.timeout = parseInt(hcTimeout.value, 10);
    data.retries = parseInt(hcRetries.value, 10);

    return data;
}

// ── Load & Render ─────────────────────────────────────────────────
async function loadHealthchecks() {
    try {
        const res = await fetch('/api/healthchecks');
        if (!res.ok) throw new Error('Failed to load');
        const data = await res.json();
        renderHealthchecks(data);
    } catch (err) {
        console.error('Failed to load healthchecks:', err);
        healthcheckTbody.innerHTML = '<tr><td colspan="7" class="error">Failed to load healthchecks</td></tr>';
    }
}

function renderHealthchecks(checks) {
    healthcheckTbody.innerHTML = '';
    const names = Object.keys(checks).sort();

    if (names.length === 0) {
        healthcheckEmpty.classList.remove('hidden');
        healthcheckTable.classList.add('hidden');
        return;
    }

    healthcheckEmpty.classList.add('hidden');
    healthcheckTable.classList.remove('hidden');

    names.forEach(name => {
        const hc = checks[name];
        const row = document.createElement('tr');
        row.dataset.name = name;

        // Determine target display
        let target = '';
        if (hc.type === 'curl' || hc.type === 'soap' || hc.type === 'rss') {
            target = hc.url || '';
        } else if (hc.type === 'ping') {
            target = hc.host || '';
        } else if (hc.type === 'tcp') {
            target = `${hc.host || ''}:${hc.port || ''}`;
        }

        // Type badge
        const typeBadge = `<span class="type-badge type-${hc.type}">${hc.type.toUpperCase()}</span>`;

        row.innerHTML = `
            <td class="hc-name">${escapeHtml(name)}</td>
            <td>${typeBadge}</td>
            <td class="hc-target">${escapeHtml(target)}</td>
            <td>${hc.interval || 60}s</td>
            <td>${hc.timeout || 10}s</td>
            <td>${hc.retries || 2}</td>
            <td class="hc-actions">
                <button class="btn-sm btn-edit" data-name="${escapeHtml(name)}" title="Edit">✎</button>
                <button class="btn-sm btn-run" data-name="${escapeHtml(name)}" title="Run Now">▶</button>
                <button class="btn-sm btn-delete" data-name="${escapeHtml(name)}" title="Delete">🗑</button>
            </td>
        `;
        healthcheckTbody.appendChild(row);
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ── Event Handlers ────────────────────────────────────────────────
if (addHealthcheckBtn) {
    addHealthcheckBtn.addEventListener('click', () => openModal('create'));
}

if (closeHealthcheckModal) {
    closeHealthcheckModal.addEventListener('click', closeModal);
}

if (cancelHealthcheck) {
    cancelHealthcheck.addEventListener('click', closeModal);
}

healthcheckModal && healthcheckModal.addEventListener('click', e => {
    if (e.target === healthcheckModal) closeModal();
});

document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !healthcheckModal.classList.contains('hidden')) {
        closeModal();
    }
});

// Type selector change
if (hcType) {
    hcType.addEventListener('change', () => showFieldGroups(hcType.value));
}

// Form submit
if (healthcheckForm) {
    healthcheckForm.addEventListener('submit', async e => {
        e.preventDefault();
        hideError();

        const data = collectFormData();
        if (!data.name) { showError('Name is required'); return; }
        if (!data.type) { showError('Type is required'); return; }

        // Type-specific validation
        if (data.type === 'curl' || data.type === 'soap' || data.type === 'rss') {
            if (!data.url) { showError('URL is required'); return; }
            try { new URL(data.url); } catch { showError('Invalid URL'); return; }
        } else if (data.type === 'ping') {
            if (!data.host) { showError('Host is required'); return; }
        } else if (data.type === 'tcp') {
            if (!data.host) { showError('Host is required'); return; }
            if (!data.port || data.port < 1 || data.port > 65535) { showError('Valid port (1-65535) required'); return; }
        }

        const mode = hcMode.value;
        const originalName = hcOriginalName.value;
        const url = mode === 'create' ? '/api/healthchecks' : `/api/healthchecks/${encodeURIComponent(originalName)}`;
        const method = mode === 'create' ? 'POST' : 'PUT';

        try {
            const res = await csrfFetch(url, {
                method: method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            });

            const result = await res.json();
            if (!res.ok) {
                showError(result.error || 'Failed to save');
                return;
            }

            closeModal();
            await loadHealthchecks();
        } catch (err) {
            showError('Connection error: ' + err.message);
        }
    });
}

// Table actions (edit, run, delete)
if (healthcheckTbody) {
    healthcheckTbody.addEventListener('click', async e => {
        const editBtn = e.target.closest('.btn-edit');
        const runBtn = e.target.closest('.btn-run');
        const deleteBtn = e.target.closest('.btn-delete');

        if (editBtn) {
            const name = editBtn.dataset.name;
            try {
                const res = await fetch('/api/healthchecks');
                const data = await res.json();
                if (data[name]) openModal('edit', data[name]);
            } catch (err) {
                alert('Failed to load healthcheck: ' + err.message);
            }
            return;
        }

        if (runBtn) {
            const name = runBtn.dataset.name;
            runBtn.disabled = true;
            runBtn.textContent = '⟳';
            try {
                const res = await csrfFetch('/api/healthcheck/run', { method: 'POST' });
                const result = await res.json();
                if (res.ok) {
                    alert(`Healthcheck run complete for ${name}`);
                } else {
                    alert('Run failed: ' + (result.error || 'Unknown error'));
                }
            } catch (err) {
                alert('Connection error: ' + err.message);
            } finally {
                runBtn.disabled = false;
                runBtn.textContent = '▶';
            }
            return;
        }

        if (deleteBtn) {
            const name = deleteBtn.dataset.name;
            if (!confirm(`Delete healthcheck "${name}"?`)) return;
            try {
                const res = await csrfFetch(`/api/healthchecks/${encodeURIComponent(name)}`, { method: 'DELETE' });
                const result = await res.json();
                if (!res.ok) {
                    alert('Delete failed: ' + (result.error || 'Unknown error'));
                    return;
                }
                await loadHealthchecks();
            } catch (err) {
                alert('Connection error: ' + err.message);
            }
            return;
        }
    });
}

// ── Initialize ────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    if (healthcheckAdmin) {
        loadHealthchecks();
    }
});