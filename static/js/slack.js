// ── Slack Notifications UI ────────────────────────────────────────
// Admin: toggle Slack on/off, set the webhook URL and channel
// (GET/POST /api/slack). Depends on csrfFetch() from csrf.js.

(function () {
    const slackAdmin = document.getElementById('slackAdmin');
    const slackEnabled = document.getElementById('slackEnabled');
    const slackState = document.getElementById('slackState');
    const slackWebhook = document.getElementById('slackWebhook');
    const webhookInput = document.getElementById('slackWebhookInput');
    const channelInput = document.getElementById('slackChannelInput');
    const saveBtn = document.getElementById('slackWebhookSave');
    const queuedEl = document.getElementById('slackQueued');

    if (!slackAdmin) return; // panel absent (non-admin or template change)

    function isAdmin() {
        return document.body.classList.contains('admin');
    }

    function applyState(data) {
        if (!data) return;
        if (slackEnabled) {
            slackEnabled.disabled = !isAdmin();
            slackEnabled.checked = !!data.enabled;
        }
        if (slackState) {
            if (!data.configured) {
                slackState.textContent = 'No webhook';
            } else {
                slackState.textContent = data.enabled ? 'Enabled' : 'Disabled';
            }
        }
        if (slackWebhook) {
            slackWebhook.textContent = data.webhook_masked || 'no webhook';
        }
        if (channelInput && document.activeElement !== channelInput) {
            channelInput.value = data.channel || '';
        }
        if (queuedEl) {
            queuedEl.textContent = data.queued > 0
                ? `${data.queued} queued` : '';
        }
    }

    async function loadStatus() {
        try {
            const res = await fetch('/api/slack');
            if (!res.ok) return;
            applyState(await res.json());
        } catch (_) { /* endpoint unavailable — leave defaults */ }
    }

    async function post(body) {
        const res = await csrfFetch('/api/slack', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        applyState(data);
    }

    async function toggle() {
        if (!slackEnabled) return;
        const target = slackEnabled.checked;
        slackEnabled.disabled = true;
        if (slackState) slackState.textContent = 'Saving…';
        try {
            await post({ enabled: target });
        } catch (err) {
            slackEnabled.checked = !target;
            if (slackState) slackState.textContent = 'Error: ' + err.message;
        } finally {
            slackEnabled.disabled = false;
            loadStatus();
        }
    }

    async function saveConfig() {
        if (!saveBtn) return;
        saveBtn.disabled = true;
        const orig = saveBtn.textContent;
        saveBtn.textContent = 'Saving…';
        try {
            const body = {};
            // Only send fields the user actually touched.
            if (webhookInput && webhookInput.value.trim()) {
                body.webhook_url = webhookInput.value.trim();
                webhookInput.value = '';
            }
            if (channelInput) body.channel = channelInput.value.trim();
            await post(body);
        } catch (err) {
            if (slackState) slackState.textContent = 'Error: ' + err.message;
        } finally {
            saveBtn.disabled = false;
            saveBtn.textContent = orig;
            loadStatus();
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        loadStatus();
        if (slackEnabled && isAdmin()) {
            slackEnabled.addEventListener('change', toggle);
        }
        if (saveBtn && isAdmin()) {
            saveBtn.addEventListener('click', saveConfig);
            if (webhookInput) {
                webhookInput.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') { e.preventDefault(); saveConfig(); }
                });
            }
        }
    });

    // Re-check state after login/logout transitions refresh the page.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) loadStatus();
    });
})();
