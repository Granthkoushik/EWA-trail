/**
 * EWA (Enhanced Wearable Avatar) Console Frontend Script
 * Real-time SSE integration, audio monitor, command execution, and live logs.
 */

let autoScroll = true;
let currentLogFilter = 'ALL';
let allLogs = [];
let uptimeSeconds = 0;
let sseConnection = null;

document.addEventListener('DOMContentLoaded', () => {
    initSSE();
    fetchInitialData();
    setInterval(updateUptimeTimer, 1000);
    setInterval(pollStatusFallback, 4000); // Polling backup

    // Settings modal toggle
    document.getElementById('btn-settings-toggle').addEventListener('click', openSettings);
});

// ============================================================
// SERVER-SENT EVENTS (SSE) REAL-TIME TELEMETRY
// ============================================================
function initSSE() {
    const sseBadge = document.getElementById('sse-badge');
    
    if (sseConnection) {
        sseConnection.close();
    }

    try {
        sseConnection = new EventSource('/events');

        sseConnection.onopen = () => {
            sseBadge.className = 'badge badge-success';
            sseBadge.textContent = 'LIVE SSE';
        };

        sseConnection.onerror = () => {
            sseBadge.className = 'badge badge-idle';
            sseBadge.textContent = 'RECONNECTING';
        };

        // Custom event listeners
        sseConnection.addEventListener('init', (e) => {
            const data = JSON.parse(e.data);
            handleInitEvent(data);
        });

        sseConnection.addEventListener('log', (e) => {
            const entry = JSON.parse(e.data);
            appendLogEntry(entry);
        });

        sseConnection.addEventListener('xiao_status', (e) => {
            const data = JSON.parse(e.data);
            updateXiaoUI(data);
        });

        sseConnection.addEventListener('audio_received', (e) => {
            const data = JSON.parse(e.data);
            handleAudioReceived(data);
        });

        sseConnection.addEventListener('command_executed', (e) => {
            const data = JSON.parse(e.data);
            handleCommandExecuted(data);
        });

        sseConnection.addEventListener('history', (e) => {
            const entry = JSON.parse(e.data);
            prependHistoryRow(entry);
        });

    } catch (err) {
        console.error('SSE initialization error:', err);
    }
}

function handleInitEvent(data) {
    if (data.laptop_ip) {
        document.getElementById('top-laptop-ip').textContent = data.laptop_ip;
        document.getElementById('val-laptop-ip').textContent = data.laptop_ip;
        document.getElementById('set-server-ip').textContent = data.laptop_ip;
    }
    if (data.xiao) {
        updateXiaoUI(data.xiao);
    }
}

// ============================================================
// STATUS & TELEMETRY UPDATES
// ============================================================
async function fetchInitialData() {
    try {
        const [resStatus, resLogs, resHistory] = await Promise.all([
            fetch('/status').then(r => r.json()),
            fetch('/logs').then(r => r.json()),
            fetch('/history').then(r => r.json())
        ]);

        applyStatusData(resStatus);
        
        allLogs = resLogs;
        renderLogs();

        renderHistory(resHistory);
    } catch (err) {
        console.warn('Initial data load warning:', err);
    }
}

async function pollStatusFallback() {
    try {
        const res = await fetch('/status');
        if (res.ok) {
            const data = await res.json();
            applyStatusData(data);
        }
    } catch (e) {
        // Server might be restarting
    }
}

function applyStatusData(data) {
    // Laptop IP
    if (data.laptop_ip) {
        document.getElementById('top-laptop-ip').textContent = data.laptop_ip;
        document.getElementById('val-laptop-ip').textContent = data.laptop_ip;
        document.getElementById('set-server-ip').textContent = data.laptop_ip;
    }

    // WiFi
    if (data.wifi_ssid) {
        document.getElementById('top-wifi-ssid').textContent = data.wifi_ssid;
        document.getElementById('set-wifi-ssid').textContent = data.wifi_ssid;
    }

    // Server Port
    if (data.server_port) {
        document.getElementById('set-server-port').textContent = data.server_port;
        document.getElementById('ewa-server-status').textContent = `PORT ${data.server_port}`;
    }

    // EWA Status
    document.getElementById('pill-ewa').className = 'node-state-pill online';
    document.getElementById('pill-ewa').textContent = 'ONLINE';

    // Gemini Status
    const geminiPill = document.getElementById('pill-gemini');
    const geminiSub = document.getElementById('gemini-status-sub');
    const setGemini = document.getElementById('set-gemini-status');
    const setApiKey = document.getElementById('set-api-key-status');

    if (data.gemini_status === 'ONLINE') {
        geminiPill.className = 'node-state-pill online';
        geminiPill.textContent = 'ONLINE';
        geminiSub.textContent = 'Gemini 2.5 Flash';
        setGemini.textContent = 'ONLINE (Ready)';
        setGemini.style.color = '#34d399';
    } else {
        geminiPill.className = 'node-state-pill offline';
        geminiPill.textContent = 'OFFLINE';
        geminiSub.textContent = 'Key Missing';
        setGemini.textContent = 'OFFLINE (No API Key)';
        setGemini.style.color = '#f87171';
    }

    setApiKey.textContent = data.api_key_configured ? 'Configured (●●●●●●)' : 'Not Configured';
    setApiKey.style.color = data.api_key_configured ? '#34d399' : '#f87171';

    // Xiao Status
    if (data.xiao) {
        updateXiaoUI(data.xiao);
    }

    // Voice metadata
    if (data.voice) {
        if (data.voice.last_audio_time) {
            document.getElementById('val-last-audio').textContent = data.voice.last_audio_time;
        }
        if (data.voice.last_duration) {
            document.getElementById('val-voice-duration').textContent = `${data.voice.last_duration}s`;
        }
        if (data.voice.last_bytes) {
            document.getElementById('val-voice-bytes').textContent = `${Math.round(data.voice.last_bytes / 1024)} KB`;
        }
        if (data.voice.last_transcript) {
            document.getElementById('val-last-transcript').textContent = `"${data.voice.last_transcript}"`;
        }
    }

    if (data.command_count !== undefined) {
        document.getElementById('val-voice-count').textContent = data.command_count;
    }

    if (data.uptime_seconds) {
        uptimeSeconds = data.uptime_seconds;
    }
}

function updateXiaoUI(xiao) {
    const pill = document.getElementById('pill-xiao');
    const sub = document.getElementById('xiao-heartbeat-sub');
    const valIp = document.getElementById('val-xiao-ip');
    const topIp = document.getElementById('top-xiao-ip');
    const valRssi = document.getElementById('val-xiao-rssi');
    const valHeartbeat = document.getElementById('val-last-heartbeat');

    if (xiao.ip) {
        valIp.textContent = xiao.ip;
        topIp.textContent = xiao.ip;
    }

    if (xiao.rssi) {
        valRssi.textContent = xiao.rssi;
    }

    if (xiao.online) {
        pill.className = 'node-state-pill online';
        pill.textContent = 'ONLINE';
        sub.textContent = xiao.last_seen_sec_ago !== null ? `${xiao.last_seen_sec_ago}s ago` : 'Connected';
    } else {
        pill.className = 'node-state-pill offline';
        pill.textContent = 'OFFLINE';
        sub.textContent = xiao.last_seen_sec_ago !== null ? `${Math.round(xiao.last_seen_sec_ago)}s ago` : 'Disconnected';
    }

    if (xiao.last_seen_str) {
        valHeartbeat.textContent = xiao.last_seen_str;
    }
}

// ============================================================
// AUDIO RECEPTION & WAVEFORM ANIMATION
// ============================================================
function handleAudioReceived(data) {
    const recBadge = document.getElementById('recording-badge');
    const waveform = document.getElementById('waveform');

    recBadge.className = 'badge badge-active';
    recBadge.textContent = 'RECEIVING AUDIO';
    waveform.classList.add('waveform-active');

    document.getElementById('val-last-audio').textContent = data.timestamp || 'Just now';
    document.getElementById('val-voice-duration').textContent = `${data.duration}s`;
    document.getElementById('val-voice-bytes').textContent = `${Math.round(data.bytes / 1024)} KB`;

    // Refresh audio player with latest WAV
    const player = document.getElementById('audio-player');
    const filenameLabel = document.getElementById('audio-filename-label');
    filenameLabel.textContent = data.filename || 'latest.wav';

    player.src = `/audio/latest?t=${Date.now()}`;
    player.load();

    setTimeout(() => {
        recBadge.className = 'badge badge-idle';
        recBadge.textContent = 'STANDBY';
        waveform.classList.remove('waveform-active');
    }, 4500);
}

// ============================================================
// COMMAND EXECUTION
// ============================================================
async function submitCommand(event) {
    event.preventDefault();
    const input = document.getElementById('cmd-input');
    const cmd = input.value.trim();
    if (!cmd) return;

    input.value = '';
    const btn = document.getElementById('btn-send-cmd');
    btn.disabled = true;

    try {
        const res = await fetch('/command', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ command: cmd })
        });
        const result = await res.json();
        if (result.interpretation) {
            displayInterpretation(
                result.interpretation.intent,
                result.interpretation.target || result.interpretation.value || '-',
                result.execution,
                result.success
            );
        }
    } catch (err) {
        console.error('Command submission error:', err);
    } finally {
        btn.disabled = false;
        input.focus();
    }
}

function applyExample(text) {
    const input = document.getElementById('cmd-input');
    input.value = text;
    document.getElementById('command-form').dispatchEvent(new Event('submit'));
}

async function triggerSystemAction(actionName) {
    try {
        const res = await fetch('/system-action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: actionName })
        });
        const data = await res.json();
        displayInterpretation(actionName, '-', data.execution, data.success);
    } catch (e) {
        console.error('System action failed:', e);
    }
}

function handleCommandExecuted(data) {
    displayInterpretation(data.intent, data.target || '-', data.execution, true);
    if (data.transcript) {
        document.getElementById('val-last-transcript').textContent = `"${data.transcript}"`;
    }
}

function displayInterpretation(intent, target, result, success) {
    document.getElementById('interp-intent').textContent = intent || '--';
    document.getElementById('interp-target').textContent = target || '--';
    
    const statusPill = document.getElementById('interp-status');
    statusPill.innerHTML = success 
        ? '<span class="badge badge-success">Success</span>' 
        : '<span class="badge badge-active">Error</span>';

    document.getElementById('interp-result').textContent = result || 'Executed.';
}

// ============================================================
// LIVE LOGS
// ============================================================
function appendLogEntry(entry) {
    allLogs.push(entry);
    if (allLogs.length > 200) allLogs.shift();

    if (currentLogFilter === 'ALL' || entry.source === currentLogFilter) {
        renderSingleLog(entry);
    }
}

function renderLogs() {
    const container = document.getElementById('log-container');
    container.innerHTML = '';

    const filtered = currentLogFilter === 'ALL' 
        ? allLogs 
        : allLogs.filter(l => l.source === currentLogFilter);

    filtered.forEach(entry => renderSingleLog(entry));
}

function renderSingleLog(entry) {
    const container = document.getElementById('log-container');
    const div = document.createElement('div');
    div.className = `log-entry ${entry.level === 'ERROR' ? 'log-level-error' : entry.level === 'WARNING' ? 'log-level-warning' : ''}`;

    const srcClass = `src-${entry.source.toLowerCase()}`;
    div.innerHTML = `
        <span class="log-time">[${entry.time}]</span>
        <span class="log-src ${srcClass}">${entry.source}</span>
        <span class="log-msg">${escapeHtml(entry.message)}</span>
    `;

    container.appendChild(div);

    if (autoScroll) {
        container.scrollTop = container.scrollHeight;
    }
}

function filterLogs(filterName) {
    currentLogFilter = filterName;
    document.querySelectorAll('.log-tab').forEach(t => {
        t.classList.toggle('active', t.textContent.trim() === filterName);
    });
    renderLogs();
}

function toggleAutoScroll() {
    autoScroll = !autoScroll;
    const btn = document.getElementById('btn-autoscroll');
    btn.textContent = `Auto-scroll: ${autoScroll ? 'ON' : 'OFF'}`;
}

function clearLiveLogs() {
    allLogs = [];
    document.getElementById('log-container').innerHTML = '';
}

// ============================================================
// HISTORY TABLE
// ============================================================
async function fetchHistory() {
    try {
        const res = await fetch('/history');
        if (res.ok) {
            const list = await res.json();
            renderHistory(list);
        }
    } catch (e) {
        console.error('Fetch history failed:', e);
    }
}

function renderHistory(historyList) {
    const tbody = document.getElementById('history-table-body');
    if (!historyList || historyList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">No commands recorded yet. Send a command or speak into XIAO.</td></tr>';
        return;
    }

    tbody.innerHTML = '';
    historyList.forEach(item => {
        const tr = createHistoryRow(item);
        tbody.appendChild(tr);
    });
}

function prependHistoryRow(item) {
    const tbody = document.getElementById('history-table-body');
    if (tbody.children.length === 1 && tbody.children[0].innerText.includes('No commands recorded')) {
        tbody.innerHTML = '';
    }
    const tr = createHistoryRow(item);
    tbody.insertBefore(tr, tbody.firstChild);
}

function createHistoryRow(item) {
    const tr = document.createElement('tr');
    const isCompleted = item.status === 'Completed';
    tr.innerHTML = `
        <td class="font-mono">${escapeHtml(item.time)}</td>
        <td>${escapeHtml(item.input)}</td>
        <td class="font-mono"><em>"${escapeHtml(item.transcript)}"</em></td>
        <td><span class="badge badge-accent">${escapeHtml(item.intent)}</span></td>
        <td class="font-mono">${escapeHtml(item.action)}</td>
        <td>${escapeHtml(item.result)}</td>
        <td><span class="status-badge ${isCompleted ? 'completed' : 'failed'}">${escapeHtml(item.status)}</span></td>
    `;
    return tr;
}

// ============================================================
// SETTINGS MODAL
// ============================================================
function openSettings() {
    document.getElementById('settings-modal').style.display = 'flex';
}

function closeSettings() {
    document.getElementById('settings-modal').style.display = 'none';
}

// ============================================================
// HELPERS
// ============================================================
function updateUptimeTimer() {
    uptimeSeconds++;
    const h = String(Math.floor(uptimeSeconds / 3600)).padStart(2, '0');
    const m = String(Math.floor((uptimeSeconds % 3600) / 60)).padStart(2, '0');
    const s = String(uptimeSeconds % 60).padStart(2, '0');
    document.getElementById('top-uptime').textContent = `${h}:${m}:${s}`;
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
