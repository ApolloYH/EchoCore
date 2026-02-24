/**
 * EchoCore - 主应用逻辑
 */

// 全局状态
const AppState = {
    currentMeeting: null,
    isRecording: false,
    transcript: [],
    mode: '2pass', // online, offline, 2pass
    computeDevice: localStorage.getItem('compute_device') || 'gpu', // gpu, cpu
    connectionStatus: 'disconnected',
    timeline: {
        topic: '',
        items: []
    },
    // 用户认证状态
    user: null,
    token: localStorage.getItem('access_token'),
    // 离线上传状态
    offline: {
        file: null,
        uploadId: null,
        jobId: null,
        chunkSize: 8 * 1024 * 1024, // 8MB default
        totalBytes: 0,
        uploadedBytes: 0,
        uploadPercent: 0,
        recognizePercent: 0,
        uploading: false,
        recognizing: false,
        abortController: null
    }
};

function normalizeSegmentText(text) {
    const cleaned = String(text || '').replace(/\s+/g, ' ').trim();
    if (!cleaned) return '';
    return cleaned
        .replace(/^[，。！？!?、；;：:,.·~…—\-]+/u, '')
        .trim();
}

// 认证客户端
class AuthClient {
    constructor() {
        this.apiBase = '/api/auth';
    }

    async login(username, password) {
        const response = await fetch(`${this.apiBase}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password })
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || '登录失败');
        }

        return response.json();
    }

    async register(username, password, email = null) {
        const response = await fetch(`${this.apiBase}/register`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username, password, email })
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || '注册失败');
        }

        return response.json();
    }

    async getCurrentUser() {
        if (!AppState.token) return null;

        const response = await fetch(`${this.apiBase}/me`, {
            headers: {
                'Authorization': `Bearer ${AppState.token}`
            }
        });

        if (!response.ok) {
            // 401 错误，清除无效的会话
            if (response.status === 401) {
                this.clearSession();
                updateUserUI();
            }
            return null;
        }

        return response.json();
    }

    setSession(data) {
        AppState.token = data.access_token;
        AppState.user = data.user;
        localStorage.setItem('access_token', data.access_token);
    }

    clearSession() {
        AppState.token = null;
        AppState.user = null;
        localStorage.removeItem('access_token');
    }
}

// 录音管理
class AudioManager {
    constructor() {
        this.recorder = null;
        this.stream = null;
        this.audioContext = null;
        this.processor = null;
    }

    async init() {
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    sampleRate: 16000,
                    channelCount: 1,
                    echoCancellation: true,
                    noiseSuppression: true
                }
            });

            this.audioContext = new (window.AudioContext || window.webkitAudioContext)({
                sampleRate: 16000
            });

            const source = this.audioContext.createMediaStreamSource(this.stream);
            this.processor = this.audioContext.createScriptProcessor(4096, 1, 1);

            source.connect(this.processor);
            this.processor.connect(this.audioContext.destination);

            return true;
        } catch (error) {
            console.error('Failed to initialize audio:', error);
            return false;
        }
    }

    start(onData) {
        if (!this.processor) {
            console.error('Audio not initialized');
            return;
        }

        this.processor.onaudioprocess = (event) => {
            if (this.recording) {
                const inputData = event.inputBuffer.getChannelData(0);
                onData(inputData);
            }
        };
        this.recording = true;
    }

    stop() {
        this.recording = false;
        if (this.processor) {
            this.processor.onaudioprocess = null;
        }
        if (this.stream) {
            this.stream.getTracks().forEach(track => track.stop());
        }
        if (this.audioContext) {
            this.audioContext.close();
        }
    }
}

// ASR WebSocket客户端
class ASRClient {
    constructor() {
        this.ws = null;
        this.reconnectAttempts = 0;
        this.maxReconnectAttempts = 5;
        this.lastMessageAt = 0;
        this.messageCount = 0;
    }

    connect(url, mode, meetingId, hotwords = {}, computeDevice = 'gpu') {
        return new Promise((resolve, reject) => {
            try {
                this.ws = new WebSocket(url, ['binary']);

                this.ws.onopen = () => {
                    console.log(`ASR WebSocket connected: ${url}`);
                    this.reconnectAttempts = 0;
                    this.lastMessageAt = Date.now();
                    this.messageCount = 0;
                    AppState.connectionStatus = 'connected';
                    window.dispatchEvent(new CustomEvent('ws-status', { detail: { status: 'connected' } }));

                    // 发送配置
                    this.sendConfig(mode, meetingId, hotwords, computeDevice);
                    resolve();
                };

                this.ws.onmessage = (event) => {
                    this.lastMessageAt = Date.now();
                    this.messageCount += 1;
                    this.handleMessage(event.data);
                };

                this.ws.onclose = () => {
                    console.log(`ASR WebSocket closed: ${url}`);
                    AppState.connectionStatus = 'disconnected';
                    window.dispatchEvent(new CustomEvent('ws-status', { detail: { status: 'disconnected' } }));
                };

                this.ws.onerror = (error) => {
                    AppState.connectionStatus = 'error';
                    window.dispatchEvent(new CustomEvent('ws-status', { detail: { status: 'error' } }));
                    console.error(`ASR WebSocket error: ${url}`, error);
                    const wsError = new Error(`WebSocket connect failed: ${url}`);
                    wsError.cause = error;
                    reject(wsError);
                };
            } catch (error) {
                reject(error);
            }
        });
    }

    sendConfig(mode, meetingId, hotwords, computeDevice = 'gpu') {
        // 非 online 模式增大 chunk_interval 减少碎片化断句
        const chunkInterval = mode === 'online' ? 10 : 18;
        const normalizedDevice = String(computeDevice || 'gpu').toLowerCase() === 'cpu' ? 'cpu' : 'gpu';

        const config = {
            chunk_size: [5, 10, 5],
            wav_name: meetingId,
            is_speaking: true,
            chunk_interval: chunkInterval,
            mode: mode,
            itn: true,
            // 透传给 ASR 服务；若服务端支持则按此设备推理，不支持则会忽略
            compute_device: normalizedDevice,
            use_gpu: normalizedDevice === 'gpu'
        };

        if (hotwords && Object.keys(hotwords).length > 0) {
            config.hotwords = hotwords;
        }

        this.ws.send(JSON.stringify(config));
    }

    sendAudio(audioData) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            // 转换Float32Array到Int16Array
            const int16Data = this.float32ToInt16(audioData);
            this.ws.send(int16Data.buffer);
        }
    }

    float32ToInt16(float32Array) {
        const int16Array = new Int16Array(float32Array.length);
        for (let i = 0; i < float32Array.length; i++) {
            const s = Math.max(-1, Math.min(1, float32Array[i]));
            int16Array[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }
        return int16Array;
    }

    sendStop() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ is_speaking: false }));
        }
    }

    handleMessage(data) {
        if (typeof data === 'string') {
            try {
                const result = JSON.parse(data);
                this.processResult(result);
            } catch (error) {
                console.warn('Failed to parse ASR message:', data, error);
            }
        }
    }

    processResult(result) {
        // 先更新UI，保证统计监听拿到最新的DOM状态
        updateTranscriptDisplay(result);

        // 触发自定义事件
        window.dispatchEvent(new CustomEvent('asr-result', { detail: result }));
    }

    async waitForFinalResults(maxWaitMs = 12000, minWaitMs = 3500, idleMs = 1500) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            return;
        }

        const start = Date.now();
        const baselineCount = this.messageCount;

        while (Date.now() - start < maxWaitMs) {
            if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
                return;
            }

            const elapsed = Date.now() - start;
            const hasNewMessage = this.messageCount > baselineCount;

            // 离线模式通常在停止后才返回最终结果，先保证最小等待时间
            if (!hasNewMessage && elapsed < minWaitMs) {
                await new Promise(resolve => setTimeout(resolve, 100));
                continue;
            }

            if (hasNewMessage) {
                const idle = Date.now() - this.lastMessageAt;
                if (idle >= idleMs) {
                    return;
                }
            }

            await new Promise(resolve => setTimeout(resolve, 100));
        }
    }

    disconnect() {
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }
}

// LLM客户端
class LLMClient {
    constructor(apiBase = '/api') {
        this.apiBase = apiBase;
    }

    async summarize(text, options = {}) {
        const response = await fetch(`${this.apiBase}/llm/summarize`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                text: text,
                options: options
            })
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || `Summarize failed (${response.status})`);
        }

        return response.json();
    }

    async status() {
        const response = await fetch(`${this.apiBase}/llm/status`);
        if (!response.ok) {
            throw new Error('Failed to check LLM status');
        }
        return response.json();
    }

    async extractTodos(text) {
        const response = await fetch(`${this.apiBase}/llm/extract-todos`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ text })
        });

        if (!response.ok) {
            throw new Error('Extract todos failed');
        }

        return response.json();
    }

    async extractDecisions(text) {
        const response = await fetch(`${this.apiBase}/llm/extract-decisions`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({ text })
        });

        if (!response.ok) {
            throw new Error('Extract decisions failed');
        }

        return response.json();
    }
}

// 会议管理
class MeetingManager {
    constructor() {
        this.currentMeeting = null;
    }

    buildAuthHeaders(extraHeaders = {}, token = AppState.token) {
        const headers = { ...extraHeaders };
        if (token) {
            headers.Authorization = `Bearer ${token}`;
        }
        return headers;
    }

    async create(name, mode) {
        const response = await fetch('/api/meetings', {
            method: 'POST',
            headers: this.buildAuthHeaders({
                'Content-Type': 'application/json'
            }),
            body: JSON.stringify({ name, mode })
        });

        if (response.status === 401) {
            throw new Error('请先登录后再创建会议');
        }

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || '创建会议失败');
        }

        this.currentMeeting = await response.json();
        return this.currentMeeting;
    }

    async end(meetingId) {
        const response = await fetch(`/api/meetings/${meetingId}/end`, {
            method: 'POST'
        });

        if (!response.ok) {
            throw new Error('Failed to end meeting');
        }

        return response.json();
    }

    async list(limit = 20) {
        const response = await fetch(`/api/meetings?limit=${limit}`, {
            headers: this.buildAuthHeaders()
        });
        if (!response.ok) {
            if (response.status === 401) {
                // 返回空列表而不是抛出错误，让调用方区分处理
                return [];
            }
            throw new Error('Failed to list meetings');
        }
        return response.json();
    }

    async getTranscript(meetingId) {
        const response = await fetch(`/api/meetings/${meetingId}/transcript`);
        if (!response.ok) {
            throw new Error('Failed to get transcript');
        }
        return response.json();
    }

    async getSummary(meetingId) {
        const response = await fetch(`/api/meetings/${meetingId}/summary`);
        if (!response.ok) {
            throw new Error('Failed to get summary');
        }
        return response.json();
    }

    async delete(meetingId) {
        const response = await fetch(`/api/meetings/${meetingId}`, {
            method: 'DELETE',
            headers: this.buildAuthHeaders()
        });

        if (!response.ok) {
            const err = await response.json().catch(() => ({}));
            throw new Error(err.detail || '删除会议失败');
        }

        return response.json();
    }
}

// 离线上传客户端（REST，和实时WS链路隔离）
class OfflineUploadClient {
    constructor(apiBase = '/api/offline') {
        this.apiBase = apiBase;
    }

    async initSession(payload) {
        const resp = await fetch(`${this.apiBase}/uploads/init`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || '初始化上传会话失败');
        }
        return resp.json();
    }

    async uploadChunk(uploadId, chunkIndex, blob, { signal, onProgress } = {}) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('PUT', `${this.apiBase}/uploads/${uploadId}/chunks/${chunkIndex}`);
            xhr.setRequestHeader('Content-Type', 'application/octet-stream');

            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable && typeof onProgress === 'function') {
                    onProgress(e.loaded, e.total);
                }
            };
            xhr.onerror = () => reject(new Error(`分片上传失败: ${chunkIndex}`));
            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(JSON.parse(xhr.responseText || '{}'));
                } else {
                    try {
                        const err = JSON.parse(xhr.responseText || '{}');
                        reject(new Error(err.detail || `分片上传失败(${xhr.status})`));
                    } catch {
                        reject(new Error(`分片上传失败(${xhr.status}): ${chunkIndex}`));
                    }
                }
            };

            if (signal) {
                signal.addEventListener('abort', () => {
                    xhr.abort();
                    reject(new DOMException('Upload aborted', 'AbortError'));
                }, { once: true });
            }
            xhr.send(blob);
        });
    }

    async completeUpload(uploadId, payload) {
        const resp = await fetch(`${this.apiBase}/uploads/${uploadId}/complete`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload || {})
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || '上传合并失败');
        }
        return resp.json();
    }

    async getJob(jobId) {
        const resp = await fetch(`${this.apiBase}/jobs/${jobId}`);
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || '获取离线任务状态失败');
        }
        return resp.json();
    }

    async cancelJob(jobId) {
        const resp = await fetch(`${this.apiBase}/jobs/${jobId}/cancel`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || '取消离线任务失败');
        }
        return resp.json();
    }
}

// UI管理器
class UIManager {
    constructor() {
        this.elements = {};
        this.initElements();
    }

    initElements() {
        this.elements = {
            // Header
            startBtn: document.getElementById('startBtn'),
            stopBtn: document.getElementById('stopBtn'),
            recordingIndicator: document.getElementById('recordingIndicator'),
            timer: document.getElementById('timer'),

            // Mode selector
            modeBtns: document.querySelectorAll('.mode-btn'),

            // Result area
            transcriptContainer: document.getElementById('transcriptContainer'),

            // Summary
            summaryPanel: document.getElementById('summaryPanel'),
            summaryContent: document.getElementById('summaryContent'),
            summarizeBtn: document.getElementById('summarizeBtn'),

            // Meeting list
            meetingList: document.getElementById('meetingList'),

            // Settings
            hotwordInput: document.getElementById('hotwordInput'),
            meetingNameInput: document.getElementById('meetingNameInput'),
            computeDeviceSelect: document.getElementById('computeDeviceSelect')
        };
    }

    updateRecordingState(isRecording) {
        AppState.isRecording = isRecording;

        if (isRecording) {
            this.elements.startBtn?.classList.add('hidden');
            this.elements.stopBtn?.classList.remove('hidden');
            this.elements.recordingIndicator?.classList.remove('hidden');
        } else {
            this.elements.startBtn?.classList.remove('hidden');
            this.elements.stopBtn?.classList.add('hidden');
            this.elements.recordingIndicator?.classList.add('hidden');
        }
    }

    updateTimer(seconds) {
        if (this.elements.timer) {
            const mins = Math.floor(seconds / 60);
            const secs = seconds % 60;
            this.elements.timer.textContent = `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
    }

    addTranscriptSegment(text, isFinal = false, timestamp = null) {
        if (!this.elements.transcriptContainer) return;
        const normalizedText = normalizeSegmentText(text);
        if (!normalizedText) return;
        const emptyState = document.getElementById('emptyState');
        if (emptyState) emptyState.style.display = 'none';

        const segment = document.createElement('div');
        segment.className = `result-segment ${isFinal ? 'final' : ''}`;

        const timeStr = timestamp
            ? `${(timestamp[0] / 1000).toFixed(1)}s - ${(timestamp[1] / 1000).toFixed(1)}s`
            : '';

        segment.innerHTML = `
            <div class="seg-meta"><span class="seg-ts">${timeStr}</span></div>
            <div class="seg-text">${this.escapeHtml(normalizedText)}</div>
        `;

        this.elements.transcriptContainer.appendChild(segment);
        this.elements.transcriptContainer.scrollTop = this.elements.transcriptContainer.scrollHeight;

        AppState.transcript.push({ text: normalizedText, isFinal, timestamp });
        updateStageLayoutState();
    }

    escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    clearTranscript() {
        if (this.elements.transcriptContainer) {
            this.elements.transcriptContainer.innerHTML = `
                <div class="empty-state" id="emptyState">
                    <svg class="icon" style="width:2.5rem;height:2.5rem"><use href="#i-msg"></use></svg>
                    <h3 style="font-size:1rem;font-weight:600;margin:0">EchoCore</h3>
                    <p>点击右侧按钮开始会议</p>
                </div>
            `;
        }
        AppState.transcript = [];
        liveSegmentEl = null;
        updateStageLayoutState();
    }

    setMode(mode) {
        AppState.mode = mode;
        this.elements.modeBtns?.forEach(btn => {
            btn.classList.toggle('active', btn.dataset.mode === mode);
        });
        // Sync mode select and offline FAB in new layout
        const modeSelect = document.getElementById('recognitionModeSelect');
        if (modeSelect && modeSelect.value !== mode) modeSelect.value = mode;
        const offlineFab = document.getElementById('offlineFab');
        if (offlineFab && mode !== 'offline') offlineFab.classList.add('hidden');
        if (mode !== 'offline') {
            document.getElementById('offlineFabPanel')?.classList.add('hidden');
        }
    }

    async updateMeetingList(meetings) {
        if (!this.elements.meetingList) return;

        this.elements.meetingList.innerHTML = meetings.map(m => `
            <div class="meeting-item" data-id="${m.id}">
                <div class="meeting-icon">
                    <svg width="20" height="20" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/>
                    </svg>
                </div>
                <div class="meeting-info">
                    <div class="meeting-name">${this.escapeHtml(m.name)}</div>
                    <div class="meeting-time">${new Date(m.created_at).toLocaleString()}</div>
                </div>
                <button class="meeting-delete-btn" type="button" data-action="delete" data-id="${m.id}" title="删除会议">
                    <svg class="icon" style="width:13px;height:13px"><use href="#i-trash"></use></svg>
                </button>
            </div>
        `).join('');
    }

    showSummary(summary) {
        if (!this.elements.summaryPanel) return;
        const summaryText = summary?.summary || summary?.content || '';
        const keyPoints = Array.isArray(summary?.key_points)
            ? summary.key_points
            : (Array.isArray(summary?.keypoints) ? summary.keypoints : []);
        const todos = Array.isArray(summary?.todos)
            ? summary.todos
            : (Array.isArray(summary?.todo_items) ? summary.todo_items : []);

        // Populate summary tab
        const summaryEl = document.getElementById('summaryContent');
        if (summaryEl) {
            summaryEl.innerHTML = summaryText
                ? `<div class="summary-notes">${this.escapeHtml(summaryText)}</div>`
                : '<div class="summary-notes" style="opacity:0.6">暂无总结</div>';
        }

        // Populate key points tab
        const keypointsEl = document.getElementById('keypointsContent');
        if (keypointsEl) {
            if (keyPoints.length > 0) {
                keypointsEl.innerHTML = `<ul class="summary-notes" style="padding-left:1.25rem">${keyPoints.map(p => `<li>${this.escapeHtml(p)}</li>`).join('')}</ul>`;
            } else {
                keypointsEl.innerHTML = '<div class="summary-notes" style="opacity:0.6">暂无要点</div>';
            }
        }

        // Populate todos tab
        const todosEl = document.getElementById('todosContent');
        if (todosEl) {
            if (todos.length > 0) {
                todosEl.innerHTML = `<ul class="summary-notes" style="padding-left:1.25rem">${todos.map(t => `<li>${this.escapeHtml(t.content || JSON.stringify(t))}</li>`).join('')}</ul>`;
            } else {
                todosEl.innerHTML = '<div class="summary-notes" style="opacity:0.6">暂无待办</div>';
            }
        }
    }

    hideSummary() {
        // Summary panel is always visible in new layout; just reset content
        const summaryEl = document.getElementById('summaryContent');
        if (summaryEl) summaryEl.innerHTML = '<div class="summary-notes" style="opacity:0.6">点击下方按钮生成会议纪要</div>';
        const keypointsEl = document.getElementById('keypointsContent');
        if (keypointsEl) keypointsEl.innerHTML = '<div class="summary-notes" style="opacity:0.6">暂无要点</div>';
        const todosEl = document.getElementById('todosContent');
        if (todosEl) todosEl.innerHTML = '<div class="summary-notes" style="opacity:0.6">暂无待办</div>';
    }

    showToast(message, type = 'success') {
        let container = document.querySelector('.toast-container');
        if (!container) {
            container = document.createElement('div');
            container.className = 'toast-container';
            document.body.appendChild(container);
        }

        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = `
            <span>${this.escapeHtml(message)}</span>
        `;

        container.appendChild(toast);

        setTimeout(() => {
            toast.style.opacity = '0';
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }
}

// 全局实例
let audioManager;
let asrClient;
let llmClient;
let meetingManager;
let uiManager;
let offlineUploadClient;
let authClient;
let recordingTimer;
let recordingSeconds = 0;
let liveSegmentEl = null; // 当前正在实时刷新的临时段落节点
const TIMELINE_MAX_ITEMS = 12;
const TOPIC_DISCOVERY_MIN_SEGMENTS = 5;
const TOPIC_DISCOVERY_MIN_SECONDS = 35;
const timelineSeenSet = new Set();
let timelineFinalCount = 0;
let timelinePendingTopic = '';
const TIMELINE_AI_DEBOUNCE_MS = 2400;
const TIMELINE_AI_BATCH_MAX_LINES = 12;
const TIMELINE_AI_BATCH_MAX_CHARS = 1200;
let timelineAiBuffer = [];
let timelineAiTimer = null;
let timelineAiInFlight = false;
let timelineAiPreviousSummary = '';
let timelineAiFailureCount = 0;
let timelineAiDisabled = false;
let timelineAiBootstrapping = false;

// 初始化
async function initApp() {
    console.log('Initializing app...');

    // 初始化各模块
    audioManager = new AudioManager();
    asrClient = new ASRClient();
    llmClient = new LLMClient();
    meetingManager = new MeetingManager();
    uiManager = new UIManager();
    offlineUploadClient = new OfflineUploadClient('/api/offline');
    authClient = new AuthClient();
    AppState.computeDevice = AppState.computeDevice === 'cpu' ? 'cpu' : 'gpu';
    if (uiManager.elements.computeDeviceSelect) {
        uiManager.elements.computeDeviceSelect.value = AppState.computeDevice;
    }

    // 检查登录状态
    if (AppState.token) {
        try {
            const user = await authClient.getCurrentUser();
            if (user) {
                AppState.user = user;
                updateUserUI();
            } else {
                AppState.token = null;
                localStorage.removeItem('access_token');
            }
        } catch (e) {
            console.warn('获取用户信息失败:', e);
            AppState.token = null;
            localStorage.removeItem('access_token');
        }
    }

    // 绑定事件
    bindEvents();
    resetTimelineState();
    updateStageLayoutState();

    // 加载会议列表
    await loadMeetings();

    console.log('App initialized');
}

function updateStageLayoutState() {
    const stageLayout = document.querySelector('.stage-layout');
    const transcriptCard = document.getElementById('transcriptCard');
    if (!stageLayout || !transcriptCard) return;

    const segmentCount = AppState.transcript.filter(item => item?.text).length;
    const hasTranscript = segmentCount > 0;
    const active = AppState.isRecording || hasTranscript;

    stageLayout.classList.toggle('compact', !active);

    let growLevel = 0;
    if (segmentCount >= 1) growLevel = 1;
    if (segmentCount >= 4) growLevel = 2;
    if (segmentCount >= 8) growLevel = 3;
    transcriptCard.dataset.grow = String(growLevel);
}

// 绑定事件
function bindEvents() {
    // 开始/停止录音
    document.getElementById('startBtn')?.addEventListener('click', startRecording);
    document.getElementById('stopBtn')?.addEventListener('click', stopRecording);

    // 模式选择
    document.querySelectorAll('.mode-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            uiManager.setMode(btn.dataset.mode);
        });
    });
    uiManager.elements.computeDeviceSelect?.addEventListener('change', (e) => {
        const selected = String(e.target?.value || '').toLowerCase();
        AppState.computeDevice = selected === 'cpu' ? 'cpu' : 'gpu';
        localStorage.setItem('compute_device', AppState.computeDevice);
    });

    // 会议列表点击
    document.getElementById('meetingList')?.addEventListener('click', async (e) => {
        const deleteBtn = e.target.closest('[data-action="delete"]');
        if (deleteBtn) {
            e.stopPropagation();
            const meetingId = deleteBtn.dataset.id || deleteBtn.closest('.meeting-item')?.dataset.id;
            if (meetingId) {
                await handleDeleteMeeting(meetingId);
            }
            return;
        }

        const item = e.target.closest('.meeting-item');
        if (item) {
            document.querySelectorAll('.meeting-item.active').forEach(el => el.classList.remove('active'));
            item.classList.add('active');
            await loadMeetingDetail(item.dataset.id);
        }
    });

    // 新会议入口
    document.getElementById('newMeetingBtn')?.addEventListener('click', startNewMeetingSession);

    // 总结按钮
    document.getElementById('summarizeBtn')?.addEventListener('click', generateSummary);

    // 离线上传
    initOfflineDropzone();
    document.getElementById('offlineFileInput')?.addEventListener('change', onOfflineFileInputChange);
    document.getElementById('offlineStartBtn')?.addEventListener('click', onOfflineStartClick);
    document.getElementById('offlineCancelBtn')?.addEventListener('click', cancelOfflineTask);

    // 用户头像菜单
    const userInfo = document.getElementById('userInfo');
    const userMenu = document.getElementById('userMenu');
    const userMenuTrigger = document.getElementById('userMenuTrigger');
    const logoutMenuItem = document.getElementById('logoutMenuItem');
    if (userMenu && userMenuTrigger) {
        userMenuTrigger.addEventListener('click', (e) => {
            e.stopPropagation();
            userMenu.classList.toggle('hidden');
        });
        logoutMenuItem?.addEventListener('click', () => {
            userMenu.classList.add('hidden');
            handleLogout();
        });
        document.addEventListener('click', (e) => {
            if (!userInfo?.contains(e.target)) {
                userMenu.classList.add('hidden');
            }
        });
    }
}

async function connectAsr(mode, meetingId, hotwords, computeDevice = 'gpu') {
    const host = window.location.hostname || 'localhost';
    const port = 10095;

    const sameOriginProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const sameOriginUrl = `${sameOriginProtocol}://${window.location.host}/ws/asr`;
    const directProtocols = window.location.protocol === 'https:' ? ['wss', 'ws'] : ['ws', 'wss'];

    const endpoints = [sameOriginUrl, ...directProtocols.map((protocol) => `${protocol}://${host}:${port}/`)];
    const seen = new Set();
    let lastError = null;

    for (const wsUrl of endpoints) {
        if (seen.has(wsUrl)) {
            continue;
        }
        seen.add(wsUrl);

        try {
            console.log(`Trying ASR endpoint: ${wsUrl}`);
            await asrClient.connect(wsUrl, mode, meetingId, hotwords, computeDevice);
            return;
        } catch (error) {
            lastError = error;
            console.warn(`ASR connect failed: ${wsUrl}`, error);
        }
    }

    const reason = lastError && lastError.message ? lastError.message : 'WebSocket connection failed';
    throw new Error(`${reason}. 请检查ASR服务是否启动，或通过 start.sh 重启服务。`);
}

// 开始录音
async function startRecording() {
    // 离线模式不应启动实时录音
    if (AppState.mode === 'offline') {
        document.getElementById('offlineFab')?.classList.add('hidden');
        document.getElementById('offlineFabPanel')?.classList.add('hidden');
        uiManager.showToast('当前为离线模式，请上传音频文件开始识别', 'info');
        return;
    }

    // 检查是否已登录
    if (!AppState.user) {
        uiManager.showToast('请先登录后再使用', 'warning');
        showAuthModal();
        return;
    }

    try {
        const meetingName = uiManager.elements.meetingNameInput?.value || `会议 ${new Date().toLocaleString()}`;

        // 创建会议
        await meetingManager.create(meetingName, AppState.mode);
        AppState.currentMeeting = meetingManager.currentMeeting;

        // 初始化音频
        const audioInit = await audioManager.init();
        if (!audioInit) {
            throw new Error('Failed to initialize audio');
        }

        // 获取热词
        const hotwordsText = uiManager.elements.hotwordInput?.value || '';
        const hotwords = parseHotwords(hotwordsText);
        const selectedComputeDevice = String(
            uiManager.elements.computeDeviceSelect?.value || AppState.computeDevice || 'gpu'
        ).toLowerCase();
        AppState.computeDevice = selectedComputeDevice === 'cpu' ? 'cpu' : 'gpu';
        localStorage.setItem('compute_device', AppState.computeDevice);

        // 连接ASR（自动兼容 ws/wss）
        await connectAsr(AppState.mode, AppState.currentMeeting.id, hotwords, AppState.computeDevice);

        // 开始录音
        audioManager.start((data) => {
            asrClient.sendAudio(data);
        });

        // 更新UI
        uiManager.clearTranscript();
        uiManager.hideSummary();
        resetTimelineState();
        uiManager.updateRecordingState(true);
        updateStageLayoutState();

        // 计时器
        recordingSeconds = 0;
        recordingTimer = setInterval(() => {
            recordingSeconds++;
            uiManager.updateTimer(recordingSeconds);
        }, 1000);

        uiManager.showToast('开始录音', 'success');

    } catch (error) {
        console.error('Failed to start recording:', error);
        uiManager.showToast(`开始录音失败: ${error.message}`, 'error');
    }
}

// 停止录音
async function stopRecording() {
    try {
        // 停止录音
        audioManager.stop();

        // 发送停止信号，并等待ASR返回尾段结果
        asrClient.sendStop();
        await asrClient.waitForFinalResults(12000, 3500, 1500);

        // 停止计时
        clearInterval(recordingTimer);

        // 更新UI
        uiManager.updateRecordingState(false);
        updateStageLayoutState();

        // 结束会议
        if (AppState.currentMeeting) {
            await meetingManager.end(AppState.currentMeeting.id);
            uiManager.showToast('录音已停止，会议已保存', 'success');
        }

        // 断开ASR连接
        asrClient.disconnect();

    } catch (error) {
        console.error('Failed to stop recording:', error);
        uiManager.showToast(`停止录音失败: ${error.message}`, 'error');
    }
}

function startNewMeetingSession() {
    if (AppState.isRecording) {
        uiManager.showToast('请先停止当前会议', 'warning');
        return;
    }

    AppState.currentMeeting = null;
    uiManager.clearTranscript();
    uiManager.hideSummary();
    resetTimelineState();
    document.querySelectorAll('.meeting-item.active').forEach(item => item.classList.remove('active'));

    const emptyState = document.getElementById('emptyState');
    if (emptyState) emptyState.style.display = '';

    const card = document.getElementById('transcriptCard');
    card?.classList.remove('expanded');

    if (uiManager.elements.meetingNameInput) {
        uiManager.elements.meetingNameInput.value = '新会议';
    }

    uiManager.showToast('已开启新会议', 'success');
    updateStageLayoutState();
}

async function handleDeleteMeeting(meetingId) {
    if (!AppState.user) {
        uiManager.showToast('请先登录后再删除会议', 'warning');
        showAuthModal();
        return;
    }

    const target = document.querySelector(`.meeting-item[data-id="${meetingId}"] .meeting-name`);
    const meetingName = target?.textContent?.trim() || '该会议';
    const confirmed = window.confirm(`确认删除「${meetingName}」吗？删除后无法恢复。`);
    if (!confirmed) return;

    try {
        await meetingManager.delete(meetingId);

        if (AppState.currentMeeting?.id === meetingId) {
            startNewMeetingSession();
        }

        await loadMeetings();
        uiManager.showToast('会议已删除', 'success');
    } catch (error) {
        console.error('Delete meeting failed:', error);
        uiManager.showToast(`删除失败: ${error.message}`, 'error');
    }
}

function normalizeTimestamp(timestamp) {
    if (!timestamp) return null;

    // 兼容 [start, end]
    if (Array.isArray(timestamp) && timestamp.length >= 2 && !Array.isArray(timestamp[0])) {
        const start = Number(timestamp[0]);
        const end = Number(timestamp[1]);
        if (Number.isFinite(start) && Number.isFinite(end)) {
            return [start, end];
        }
    }

    // 兼容 [[start, end], [start, end], ...]，取首尾
    if (Array.isArray(timestamp) && timestamp.length > 0 && Array.isArray(timestamp[0])) {
        const first = timestamp[0];
        const last = timestamp[timestamp.length - 1];
        const start = Number(first?.[0]);
        const end = Number(last?.[1] ?? last?.[0]);
        if (Number.isFinite(start) && Number.isFinite(end)) {
            return [start, end];
        }
    }

    // 兼容对象格式
    if (typeof timestamp === 'object') {
        const start = Number(timestamp.start ?? timestamp.start_time ?? timestamp.begin);
        const end = Number(timestamp.end ?? timestamp.end_time ?? timestamp.stop);
        if (Number.isFinite(start) && Number.isFinite(end)) {
            return [start, end];
        }
    }

    return null;
}

function detectFinalResult(result) {
    // 优先根据 mode 判断：2pass-offline / offline 表示最终结果
    // FunASR 某些版本在 mode=2pass-offline 时仍携带 is_final=false，需要优先处理
    const mode = String(result?.mode || '').toLowerCase();
    if (mode.endsWith('offline')) {
        return true;
    }

    if (typeof result?.is_final === 'boolean') {
        return result.is_final;
    }

    if (result?.is_final === 1 || result?.is_final === '1' || result?.is_final === 'true') {
        return true;
    }

    return false;
}

// 更新转写显示
function updateTranscriptDisplay(result) {
    if (!uiManager?.elements?.transcriptContainer) return;

    const text = normalizeSegmentText(result?.text || result?.result || '');
    const isFinal = detectFinalResult(result);
    const timestamp = normalizeTimestamp(result?.timestamp || result?.time_stamp);

    // 如果没有文本，跳过
    if (!text) return;
    const emptyState = document.getElementById('emptyState');
    if (emptyState) emptyState.style.display = 'none';

    // 生成时间戳字符串
    const timeStr = timestamp
        ? `${formatTimestamp(timestamp[0])} → ${formatTimestamp(timestamp[1])}`
        : '';

    const segmentHtml = `
        <div class="seg-meta"><span class="seg-ts">${timeStr}</span></div>
        <div class="seg-text">${uiManager.escapeHtml(text)}</div>
    `;

    // 查找现有的临时段
    let segment = uiManager.elements.transcriptContainer.querySelector('.result-segment[data-temp="true"]');

    if (isFinal) {
        // 最终结果：移除临时段，添加新的最终段
        if (segment) {
            segment.remove();
            segment = null;
        }

        // 创建新的最终段
        segment = document.createElement('div');
        segment.className = 'result-segment final animate-in';
        segment.innerHTML = segmentHtml;
        uiManager.elements.transcriptContainer.appendChild(segment);

        // 保存到状态：若末尾是临时结果，替换为最终结果
        const latest = AppState.transcript[AppState.transcript.length - 1];
        if (latest && !latest.isFinal) {
            AppState.transcript[AppState.transcript.length - 1] = { text, isFinal: true, timestamp };
        } else {
            AppState.transcript.push({ text, isFinal: true, timestamp });
        }

    } else {
        // 临时结果：更新现有临时段的内容（而不是创建新元素）
        if (segment) {
            segment.innerHTML = segmentHtml;
            // 更新状态中的临时条目
            const latest = AppState.transcript[AppState.transcript.length - 1];
            if (latest && !latest.isFinal) {
                latest.text = text;
                latest.timestamp = timestamp;
            }
        } else {
            // 创建新的临时段
            segment = document.createElement('div');
            segment.className = 'result-segment';
            segment.dataset.temp = 'true';
            segment.innerHTML = segmentHtml;
            uiManager.elements.transcriptContainer.appendChild(segment);

            // 添加到状态
            AppState.transcript.push({ text, isFinal: false, timestamp });
        }
    }

    // 最终结果写入流程时间线
    if (isFinal) {
        appendTimelineFromSegment({ text, timestamp });
    }

    // 向下滚动
    uiManager.elements.transcriptContainer.scrollTop =
        uiManager.elements.transcriptContainer.scrollHeight;
    updateStageLayoutState();
}

// 格式化时间戳（秒 -> MM:SS）
function formatTimestamp(ms) {
    const totalSeconds = Math.floor(ms / 1000);
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
}

function escapeHtmlText(text) {
    const div = document.createElement('div');
    div.textContent = String(text || '');
    return div.innerHTML;
}

function resetTimelineState() {
    AppState.timeline.topic = '';
    AppState.timeline.items = [];
    timelineSeenSet.clear();
    timelineFinalCount = 0;
    timelinePendingTopic = '';
    timelineAiBuffer = [];
    timelineAiPreviousSummary = '';
    timelineAiFailureCount = 0;
    timelineAiDisabled = false;
    timelineAiInFlight = false;
    timelineAiBootstrapping = false;
    if (timelineAiTimer) {
        clearTimeout(timelineAiTimer);
        timelineAiTimer = null;
    }
    renderTimeline();
}

function summarizeTimelineText(text, maxLen = null) {
    const cleaned = String(text || '').replace(/\s+/g, ' ').trim();
    if (!cleaned) return '';
    if (!Number.isFinite(maxLen) || maxLen <= 0) return cleaned;
    return cleaned.length > maxLen ? `${cleaned.slice(0, maxLen)}...` : cleaned;
}

function renderTimeline() {
    const flow = document.getElementById('summaryFlow');
    if (!flow) return;

    const topic = AppState.timeline.topic;
    const items = AppState.timeline.items || [];

    if (!topic && items.length === 0) {
        flow.innerHTML = '<div class="flow-empty">AI 将在识别中持续提炼关键会议转折点...</div>';
        return;
    }

    const topicHtml = topic
        ? `
        <div class="flow-topic-chip">会议主题</div>
        <div class="flow-topic-text">${escapeHtmlText(topic)}</div>
    `
        : '<div class="flow-empty">正在积累上下文，AI 稍后给出会议主题...</div>';

    if (items.length === 0) {
        flow.innerHTML = `${topicHtml}<div class="flow-empty">AI 正在分析最新发言...</div>`;
        return;
    }

    const itemsHtml = items.map(item => `
        <div class="flow-item ${item.type || 'milestone'}">
            <div class="flow-label">${escapeHtmlText(item.label)}</div>
            ${item.time ? `<div class="flow-time">${escapeHtmlText(item.time)}</div>` : ''}
        </div>
    `).join('');

    flow.innerHTML = `${topicHtml}<div class="flow-list">${itemsHtml}</div>`;
}

function appendTimelineFromSegment(segment) {
    if (!segment?.text) return;

    timelineFinalCount += 1;
    const text = normalizeSegmentText(segment.text);
    if (!text) return;
    if (!AppState.timeline.topic && timelinePendingTopic && canRevealMeetingTopic()) {
        AppState.timeline.topic = timelinePendingTopic;
    }

    renderTimeline();
    if (AppState.isRecording) {
        scheduleTimelineAiIncremental(text);
    }
}

function getCurrentTimelineTime() {
    if (!AppState.isRecording || recordingSeconds <= 0) return '';
    return `${Math.floor(recordingSeconds / 60).toString().padStart(2, '0')}:${(recordingSeconds % 60).toString().padStart(2, '0')}`;
}

function appendAiTimelineItem(rawLabel, type = 'milestone') {
    const label = summarizeTimelineText(rawLabel);
    if (!label) return;
    const key = `${type}:${label}`;
    if (timelineSeenSet.has(key)) return;
    timelineSeenSet.add(key);
    AppState.timeline.items.push({
        label,
        type,
        time: getCurrentTimelineTime()
    });
    if (AppState.timeline.items.length > TIMELINE_MAX_ITEMS) {
        AppState.timeline.items = AppState.timeline.items.slice(-TIMELINE_MAX_ITEMS);
    }
}

function applyAiTimelineResult(payload) {
    const incremental = normalizeSegmentText(payload?.incremental || '');
    const aiTopic = normalizeSegmentText(payload?.topic || '');
    if (aiTopic) {
        if (canRevealMeetingTopic()) {
            AppState.timeline.topic = aiTopic;
            timelinePendingTopic = '';
        } else {
            timelinePendingTopic = aiTopic;
        }
    }

    const turningPoints = Array.isArray(payload?.turning_points) ? payload.turning_points : [];
    turningPoints.forEach((point) => {
        if (!point) return;
        const pointLabel = typeof point === 'string'
            ? point
            : (point.label || point.content || point.summary || '');
        const pointType = typeof point === 'string'
            ? 'milestone'
            : normalizeTimelineType(point.type || point.kind || point.category || '');
        appendAiTimelineItem(String(pointLabel || ''), pointType);
    });

    const keyPoints = Array.isArray(payload?.key_points) ? payload.key_points : [];
    if (turningPoints.length === 0) {
        keyPoints.forEach((point) => appendAiTimelineItem(String(point || ''), 'milestone'));
    }

    const decisions = Array.isArray(payload?.decisions) ? payload.decisions : [];
    if (turningPoints.length === 0) {
        decisions.forEach((item) => {
            const text = typeof item === 'string' ? item : (item?.content || '');
            appendAiTimelineItem(String(text || ''), 'decision');
        });
    }

    if (incremental && turningPoints.length === 0 && keyPoints.length === 0 && decisions.length === 0) {
        appendAiTimelineItem(incremental, 'milestone');
    }

    if (incremental) {
        timelineAiPreviousSummary = String(payload?.context_summary || incremental);
    }

    if (!AppState.timeline.topic && timelinePendingTopic && canRevealMeetingTopic()) {
        AppState.timeline.topic = timelinePendingTopic;
    }

    renderTimeline();
}

function canRevealMeetingTopic() {
    return timelineFinalCount >= TOPIC_DISCOVERY_MIN_SEGMENTS
        || recordingSeconds >= TOPIC_DISCOVERY_MIN_SECONDS;
}

function normalizeTimelineType(type) {
    const key = String(type || '').toLowerCase();
    if (key.includes('decision') || key.includes('决策') || key.includes('决定')) return 'decision';
    if (key.includes('action') || key.includes('todo') || key.includes('待办')) return 'action';
    return 'milestone';
}

function scheduleTimelineAiIncremental(text = '') {
    if (timelineAiDisabled) return;
    if (text) timelineAiBuffer.push(text);
    if (timelineAiBuffer.length === 0) return;
    if (timelineAiTimer) clearTimeout(timelineAiTimer);
    timelineAiTimer = setTimeout(() => {
        timelineAiTimer = null;
        flushTimelineAiIncremental();
    }, TIMELINE_AI_DEBOUNCE_MS);
}

async function requestTimelineAiIncremental(text) {
    if (timelineAiDisabled) return false;
    const normalized = String(text || '').trim();
    if (!normalized) return false;
    timelineAiInFlight = true;
    try {
        const resp = await fetch('/api/realtime/summary', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                text: normalized,
                previous_summary: timelineAiPreviousSummary
            })
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            throw new Error(err.detail || `HTTP ${resp.status}`);
        }

        const payload = await resp.json();
        timelineAiFailureCount = 0;
        applyAiTimelineResult(payload);
    } catch (error) {
        timelineAiFailureCount += 1;
        console.warn('增量时间线AI总结失败，本轮不写入时间线:', error);
        if (timelineAiFailureCount >= 3) {
            timelineAiDisabled = true;
            console.warn('增量时间线AI总结已临时停用');
        }
        return false;
    } finally {
        timelineAiInFlight = false;
    }
    return true;
}

async function flushTimelineAiIncremental() {
    if (timelineAiDisabled || timelineAiInFlight || timelineAiBuffer.length === 0) return;
    const text = timelineAiBuffer.join('\n').trim();
    timelineAiBuffer = [];
    if (!text) return;

    await requestTimelineAiIncremental(text);
    if (!timelineAiDisabled && timelineAiBuffer.length > 0) {
        scheduleTimelineAiIncremental();
    }
}

function buildTimelineAiBatches(lines) {
    const batches = [];
    let current = [];
    let currentChars = 0;

    lines.forEach((line) => {
        const text = normalizeSegmentText(line);
        if (!text) return;

        const lineChars = text.length;
        const reachedLineLimit = current.length >= TIMELINE_AI_BATCH_MAX_LINES;
        const reachedCharLimit = currentChars > 0 && (currentChars + lineChars + 1) > TIMELINE_AI_BATCH_MAX_CHARS;

        if (reachedLineLimit || reachedCharLimit) {
            batches.push(current.join('\n'));
            current = [];
            currentChars = 0;
        }

        current.push(text);
        currentChars += lineChars + 1;
    });

    if (current.length > 0) {
        batches.push(current.join('\n'));
    }

    return batches;
}

async function bootstrapTimelineFromOfflineSegments(lines = []) {
    if (timelineAiDisabled || timelineAiBootstrapping) return;
    const batches = buildTimelineAiBatches(lines);
    if (batches.length === 0) return;

    timelineAiBootstrapping = true;
    renderTimeline();

    try {
        for (const batch of batches) {
            if (timelineAiDisabled) break;
            await requestTimelineAiIncremental(batch);
            await sleep(80);
        }
    } finally {
        timelineAiBootstrapping = false;
        renderTimeline();
    }
}

// 生成总结
async function generateSummary() {
    try {
        const fullText = AppState.transcript
            .filter(t => t?.text)
            .map(t => t.text)
            .join('\n');

        if (!fullText.trim()) {
            uiManager.showToast('没有可总结的内容', 'warning');
            return;
        }

        uiManager.showToast('正在生成纪要...', 'info');

        const llmStatus = await llmClient.status().catch(() => null);
        if (llmStatus && llmStatus.available === false) {
            throw new Error('AI摘要服务当前不可用，请检查LLM服务配置');
        }

        const summary = await llmClient.summarize(fullText, {
            extract_todos: true,
            extract_decisions: true,
            summary_length: 'detailed',
            allow_rule_fallback: false
        });

        uiManager.showSummary(summary);
        uiManager.showToast('纪要生成完成', 'success');

    } catch (error) {
        console.error('Failed to generate summary:', error);
        uiManager.showToast(`生成纪要失败: ${error.message}`, 'error');
    }
}

// 解析热词
function parseHotwords(text) {
    if (!text) return {};

    const lines = text.split('\n');
    const hotwords = {};

    lines.forEach(line => {
        const parts = line.trim().split(/\s+/);
        if (parts.length >= 2) {
            const word = parts.slice(0, -1).join(' ');
            const weight = parseInt(parts[parts.length - 1]) || 10;
            if (word) {
                hotwords[word] = weight;
            }
        }
    });

    return hotwords;
}

// 加载会议列表
async function loadMeetings() {
    try {
        if (!AppState.token) {
            uiManager.updateMeetingList([]);
            return;
        }
        const meetings = await meetingManager.list(20);
        // 空列表也是有效返回
        uiManager.updateMeetingList(meetings || []);
    } catch (error) {
        console.error('Failed to load meetings:', error);
    }
}

function extractSegmentTimestampMs(segment) {
    if (!segment || typeof segment !== 'object') return null;

    const startTime = Number(segment.start_time);
    const endTime = Number(segment.end_time);
    if (Number.isFinite(startTime) && Number.isFinite(endTime) && (startTime > 0 || endTime > 0)) {
        return [startTime * 1000, endTime * 1000];
    }

    const normalized = normalizeTimestamp(
        segment.timestamp
        || segment.time_stamp
        || (segment.start != null && segment.end != null ? [segment.start, segment.end] : null)
    );
    return normalized || null;
}

function normalizeHistorySegment(segment) {
    const text = normalizeSegmentText(segment?.text || segment?.result || '');
    if (!text) return null;
    const timestamp = extractSegmentTimestampMs(segment);
    return { text, timestamp };
}

async function renderTranscriptSegmentsBuffered(segments) {
    const container = uiManager?.elements?.transcriptContainer;
    if (!container) return;

    const emptyState = document.getElementById('emptyState');
    if (emptyState) emptyState.style.display = 'none';

    const CHUNK_SIZE = 120;
    AppState.transcript = [];

    for (let i = 0; i < segments.length; i += CHUNK_SIZE) {
        const fragment = document.createDocumentFragment();
        const chunk = segments.slice(i, i + CHUNK_SIZE);

        chunk.forEach((segment) => {
            const normalized = normalizeHistorySegment(segment);
            if (!normalized) return;

            const segEl = document.createElement('div');
            segEl.className = 'result-segment final';

            const timeStr = normalized.timestamp
                ? `${formatTimestamp(normalized.timestamp[0])} → ${formatTimestamp(normalized.timestamp[1])}`
                : '';

            segEl.innerHTML = `
                <div class="seg-meta"><span class="seg-ts">${timeStr}</span></div>
                <div class="seg-text">${uiManager.escapeHtml(normalized.text)}</div>
            `;

            fragment.appendChild(segEl);
            AppState.transcript.push({
                text: normalized.text,
                isFinal: true,
                timestamp: normalized.timestamp
            });
        });

        container.appendChild(fragment);
        if (i + CHUNK_SIZE < segments.length) {
            await new Promise(resolve => setTimeout(resolve, 0));
        }
    }

    container.scrollTop = container.scrollHeight;
    updateStageLayoutState();
    window.dispatchEvent(new CustomEvent('asr-result', { detail: { source: 'history' } }));
}

// 加载会议详情
async function loadMeetingDetail(meetingId) {
    try {
        uiManager.showToast('正在加载历史会议...', 'info');
        const [transcript, summary] = await Promise.all([
            meetingManager.getTranscript(meetingId),
            meetingManager.getSummary(meetingId)
        ]);
        AppState.currentMeeting = { id: meetingId };

        // 显示转写
        uiManager.clearTranscript();
        resetTimelineState();
        const segments = Array.isArray(transcript?.segments) ? transcript.segments : [];
        if (segments.length > 0) {
            await renderTranscriptSegmentsBuffered(segments);
        } else {
            // 历史数据可能只有 transcript 纯文本
            const fallbackText = String(transcript?.transcript || '').trim();
            if (fallbackText) {
                const fallbackSegments = fallbackText
                    .split(/\n+/)
                    .map(line => ({ text: line.trim() }))
                    .filter(seg => seg.text);
                await renderTranscriptSegmentsBuffered(fallbackSegments);
            }
        }
        document.getElementById('transcriptCard')?.classList.add('expanded');

        // 显示总结
        if (summary && (summary.content || summary.summary || summary.key_points || summary.todos)) {
            uiManager.showSummary({
                ...summary,
                summary: summary.summary || summary.content || ''
            });
        }

        uiManager.showToast('加载会议详情成功', 'success');

    } catch (error) {
        console.error('Failed to load meeting detail:', error);
        uiManager.showToast('加载会议详情失败', 'error');
    }
}

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', initApp);

// ==================== 离线上传功能 ====================

function initOfflineDropzone() {
    const zone = document.getElementById('offlineDropzone');
    if (!zone) return;

    ['dragenter', 'dragover'].forEach(evt => zone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add('drag-over');
    }));

    ['dragleave', 'drop'].forEach(evt => zone.addEventListener(evt, (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove('drag-over');
    }));

    zone.addEventListener('click', () => document.getElementById('offlineFileInput')?.click());
    zone.addEventListener('drop', (e) => {
        const file = e.dataTransfer?.files?.[0];
        if (file) setOfflineFile(file);
    });
}

function onOfflineFileInputChange(e) {
    const file = e.target.files?.[0];
    if (file) setOfflineFile(file);
}

function setOfflineFile(file) {
    const validTypes = ['audio/mpeg', 'audio/wav', 'audio/mp4', 'audio/aac', 'audio/flac', 'audio/ogg', 'audio/x-m4a'];
    const ext = file.name.split('.').pop()?.toLowerCase();
    const validExts = ['mp3', 'wav', 'm4a', 'aac', 'flac', 'ogg'];

    if (!validTypes.includes(file.type) && !validExts.includes(ext)) {
        uiManager.showToast('请选择有效的音频文件', 'error');
        return;
    }

    if (file.size > 2 * 1024 * 1024 * 1024) {
        uiManager.showToast('文件大小不能超过 2GB', 'error');
        return;
    }

    AppState.offline.file = file;
    AppState.offline.totalBytes = file.size;

    document.getElementById('offlineFileName').textContent = `${file.name} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
    const dz = document.getElementById('offlineDropzone');
    if (dz) { dz.querySelectorAll('.dropzone-text,.dropzone-hint,.icon').forEach(el => el.style.display = 'none'); }
    document.getElementById('offlineFileInfo')?.classList.remove('hidden');
    document.getElementById('offlineStartBtn')?.removeAttribute('disabled');
}

function clearOfflineFile(e) {
    e.stopPropagation();
    AppState.offline.file = null;
    AppState.offline.totalBytes = 0;

    document.getElementById('offlineFileInput').value = '';
    const dz = document.getElementById('offlineDropzone');
    if (dz) { dz.querySelectorAll('.dropzone-text,.dropzone-hint,.icon').forEach(el => el.style.display = ''); }
    document.getElementById('offlineFileInfo')?.classList.add('hidden');
    document.getElementById('offlineStartBtn')?.setAttribute('disabled', '');
}

async function onOfflineStartClick() {
    if (AppState.isRecording) {
        uiManager.showToast('请先停止实时录音', 'error');
        return;
    }
    if (!AppState.user) {
        uiManager.showToast('请先登录后再使用离线转写', 'warning');
        showAuthModal();
        return;
    }
    if (!AppState.offline.file) {
        uiManager.showToast('请先选择音频文件', 'error');
        return;
    }
    if (AppState.mode !== 'offline') {
        uiManager.showToast('请切换到离线模式后再上传', 'warning');
        uiManager.setMode('offline');
    }

    await startOfflineUploadFlow(AppState.offline.file);
}

function calcChunkSize(fileSize) {
    if (fileSize > 1024 * 1024 * 1024) return 16 * 1024 * 1024; // >1GB
    return 8 * 1024 * 1024; // default
}

async function startOfflineUploadFlow(file) {
    const meetingName = uiManager.elements.meetingNameInput?.value || `离线会议 ${new Date().toLocaleString()}`;
    const selectedComputeDevice = String(
        uiManager.elements.computeDeviceSelect?.value || AppState.computeDevice || 'gpu'
    ).toLowerCase();
    const computeDevice = ['gpu', 'cpu', 'auto'].includes(selectedComputeDevice) ? selectedComputeDevice : 'gpu';

    try {
        // 创建会议
        await meetingManager.create(meetingName, 'offline');
        AppState.currentMeeting = meetingManager.currentMeeting;
        AppState.computeDevice = computeDevice === 'cpu' ? 'cpu' : 'gpu';
        localStorage.setItem('compute_device', AppState.computeDevice);

        // 获取热词
        const hotwords = parseHotwords(uiManager.elements.hotwordInput?.value || '');

        // 重置状态
        AppState.offline.abortController = new AbortController();
        AppState.offline.uploading = true;
        AppState.offline.recognizing = false;
        AppState.offline.uploadedBytes = 0;
        AppState.offline.jobId = null;

        // 更新UI
        uiManager.clearTranscript();
        uiManager.hideSummary();
        document.getElementById('offlineUploadProgress')?.classList.remove('hidden');
        document.getElementById('offlineRecognizeProgress')?.classList.add('hidden');
        document.getElementById('offlineStartBtn')?.classList.add('hidden');
        document.getElementById('offlineCancelBtn')?.classList.remove('hidden');
        document.getElementById('offlineDropzone')?.classList.add('hidden');
        document.getElementById('offlineStatusText').textContent = '正在初始化上传...';

        updateUploadProgressUI(0, '准备上传...');

        // 初始化上传会话
        const session = await offlineUploadClient.initSession({
            meeting_id: AppState.currentMeeting.id,
            file_name: file.name,
            file_size: file.size,
            file_type: file.type || 'audio/mpeg',
            chunk_size: calcChunkSize(file.size),
            mode: 'offline',
            compute_device: computeDevice,
            hotwords
        });

        AppState.offline.uploadId = session.upload_id;
        AppState.offline.chunkSize = session.chunk_size;

        // 上传文件分片
        await uploadFileInChunks(file, session);

        // 完成上传
        document.getElementById('offlineStatusText').textContent = '正在合并文件...';
        const completeResp = await offlineUploadClient.completeUpload(session.upload_id, {
            meeting_id: AppState.currentMeeting.id
        });

        AppState.offline.jobId = completeResp.job_id;
        AppState.offline.uploading = false;
        AppState.offline.recognizing = true;

        // 隐藏上传进度，显示识别进度
        document.getElementById('offlineUploadProgress')?.classList.add('hidden');
        document.getElementById('offlineRecognizeProgress')?.classList.remove('hidden');
        updateUploadProgressUI(100, '上传完成');
        updateRecognitionProgressUI(0, '等待识别...');

        // 轮询任务状态
        await pollOfflineJob(completeResp.job_id);

    } catch (error) {
        console.error('Offline upload failed:', error);
        uiManager.showToast(`上传失败: ${error.message}`, 'error');
        resetOfflineUI();
    }
}

async function uploadFileInChunks(file, session) {
    const uploadedSet = new Set(session.uploaded_chunks || []);
    const totalChunks = Math.ceil(file.size / session.chunk_size);
    const queue = [];

    for (let i = 0; i < totalChunks; i++) {
        if (!uploadedSet.has(i)) queue.push(i);
    }

    const inFlightLoaded = new Map();
    const refreshProgress = () => {
        const inFlight = Array.from(inFlightLoaded.values()).reduce((a, b) => a + b, 0);
        const current = Math.min(file.size, AppState.offline.uploadedBytes + inFlight);
        const percent = Math.floor((current / file.size) * 100);
        updateUploadProgressUI(percent, `上传中 ${formatBytes(current)} / ${formatBytes(file.size)}`);
    };

    const workerCount = Math.max(1, Math.min(session.max_parallel || 3, 3));

    const workers = Array.from({ length: workerCount }).map(async () => {
        while (queue.length > 0) {
            const chunkIndex = queue.shift();
            const start = chunkIndex * session.chunk_size;
            const end = Math.min(start + session.chunk_size, file.size);
            const blob = file.slice(start, end);

            try {
                await uploadChunkWithRetry({
                    uploadId: session.upload_id,
                    chunkIndex,
                    blob,
                    signal: AppState.offline.abortController.signal,
                    onProgress: (loaded) => { inFlightLoaded.set(chunkIndex, loaded); refreshProgress(); }
                });

                inFlightLoaded.delete(chunkIndex);
                AppState.offline.uploadedBytes += blob.size;
                refreshProgress();
            } catch (err) {
                if (err.name === 'AbortError') throw err;
                console.error(`Chunk ${chunkIndex} upload failed:`, err);
                throw err;
            }
        }
    });

    await Promise.all(workers);
}

async function uploadChunkWithRetry({ uploadId, chunkIndex, blob, signal, onProgress }, retries = 3) {
    let attempt = 0;

    while (attempt < retries) {
        try {
            return await offlineUploadClient.uploadChunk(uploadId, chunkIndex, blob, { signal, onProgress });
        } catch (err) {
            attempt += 1;
            if (attempt >= retries) throw err;
            await sleep(400 * (2 ** (attempt - 1)));
        }
    }
}

async function pollOfflineJob(jobId) {
    const maxRetries = 5;
    let retryCount = 0;

    while (true) {
        if (AppState.offline.abortController?.signal.aborted) {
            throw new DOMException('Task cancelled', 'AbortError');
        }

        try {
            const job = await offlineUploadClient.getJob(jobId);
            retryCount = 0; // 重置重试计数

            const percent = job?.recognition?.percent ?? 0;

            updateRecognitionProgressUI(percent, job.status_text || getStatusText(job.status));

            if (job.status === 'completed') {
                AppState.offline.recognizing = false;
                renderOfflineResult(job.result);
                uiManager.showToast('离线识别完成', 'success');
                resetOfflineUI();
                return;
            }

            if (job.status === 'failed' || job.status === 'canceled') {
                AppState.offline.recognizing = false;

                const rawError = String(job.error || '');
                let displayMessage;

                if (job.status === 'canceled') {
                    displayMessage = '任务已取消';
                } else {
                    displayMessage = rawError || '离线识别失败';
                }

                // 显示降级警告（后端成功降级后仍可能 completed，此处仅处理真正失败）
                updateRecognitionProgressUI(percent, displayMessage);
                uiManager.showToast(displayMessage, job.status === 'canceled' ? 'warning' : 'error');
                resetOfflineUI();
                return;
            }
        } catch (err) {
            if (err.name === 'AbortError') throw err;

            retryCount++;
            console.error(`获取任务状态失败 (${retryCount}/${maxRetries}):`, err);

            if (retryCount >= maxRetries) {
                AppState.offline.recognizing = false;
                resetOfflineUI();
                uiManager.showToast(`获取任务状态失败: ${err.message}`, 'error');
                throw err;
            }
        }

        await sleep(1000);
    }
}

function getStatusText(status) {
    const map = {
        'uploading': '上传中...',
        'uploaded': '文件处理中...',
        'queued': '等待识别...',
        'recognizing': '识别中...',
        'completed': '识别完成',
        'failed': '识别失败',
        'canceled': '已取消'
    };
    return map[status] || status;
}

function updateUploadProgressUI(percent, text) {
    const bar = document.getElementById('offlineUploadBar');
    const label = document.getElementById('offlineUploadText');
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    if (label) label.textContent = text || `${percent}%`;
}

function updateRecognitionProgressUI(percent, text) {
    const bar = document.getElementById('offlineRecognizeBar');
    const label = document.getElementById('offlineRecognizeText');
    const status = document.getElementById('offlineStatusText');
    if (bar) bar.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    if (label) label.textContent = `${percent}%`;
    if (status) status.textContent = text || '识别中...';
}

async function cancelOfflineTask() {
    AppState.offline.abortController?.abort();

    if (AppState.offline.jobId) {
        try {
            await offlineUploadClient.cancelJob(AppState.offline.jobId);
        } catch (e) {
            console.warn('Cancel job failed:', e);
        }
    }

    AppState.offline.uploading = false;
    AppState.offline.recognizing = false;
    uiManager.showToast('已取消离线任务', 'warning');
    resetOfflineUI();
}

function resetOfflineUI() {
    AppState.offline.uploading = false;
    AppState.offline.recognizing = false;
    AppState.offline.file = null;
    AppState.offline.uploadId = null;
    AppState.offline.jobId = null;

    document.getElementById('offlineUploadProgress')?.classList.add('hidden');
    document.getElementById('offlineRecognizeProgress')?.classList.add('hidden');
    document.getElementById('offlineFabPanel')?.classList.add('hidden');
    document.getElementById('offlineFab')?.classList.add('hidden');
    document.getElementById('offlineStartBtn')?.classList.remove('hidden');
    document.getElementById('offlineCancelBtn')?.classList.add('hidden');
    document.getElementById('offlineDropzone')?.classList.remove('hidden');
    const dzReset = document.getElementById('offlineDropzone');
    if (dzReset) { dzReset.querySelectorAll('.dropzone-text,.dropzone-hint,.icon').forEach(el => el.style.display = ''); }
    document.getElementById('offlineFileInfo')?.classList.add('hidden');
    document.getElementById('offlineFileInput').value = '';
    document.getElementById('offlineStatusText').textContent = '';
    document.getElementById('offlineUploadBar').style.width = '0%';
    document.getElementById('offlineRecognizeBar').style.width = '0%';
}

function renderOfflineResult(result) {
    uiManager.clearTranscript();
    resetTimelineState();

    const computeDeviceUsed = String(result?.compute_device || '').toLowerCase();
    if (computeDeviceUsed.startsWith('cuda')) {
        uiManager.showToast('离线识别已使用 GPU 推理', 'success');
    } else if (computeDeviceUsed === 'cpu') {
        uiManager.showToast('离线识别当前使用 CPU 推理', 'info');
    }

    // 显示识别警告
    const warnings = result?.warnings || [];
    warnings.forEach(w => uiManager.showToast(w, 'warning'));

    const timelineSeedLines = [];
    const segments = Array.isArray(result?.segments) ? result.segments : [];
    if (segments.length > 0) {
        segments.forEach(seg => {
            const start = Number(seg.start_time);
            const end = Number(seg.end_time);
            const hasTime = Number.isFinite(start) && Number.isFinite(end) && (start > 0 || end > 0);
            uiManager.addTranscriptSegment(
                seg.text || '',
                true,
                hasTime ? [start * 1000, end * 1000] : null
            );
            appendTimelineFromSegment({
                text: seg.text || '',
                timestamp: hasTime ? [start * 1000, end * 1000] : null
            });
            if (seg?.text) timelineSeedLines.push(seg.text);
        });
    } else {
        // 兜底：只有 full_text 没有 segments 时按行显示
        const fullText = String(result?.full_text || '').trim();
        if (fullText) {
            fullText.split(/\n+/).map(l => l.trim()).filter(Boolean).forEach(line => {
                uiManager.addTranscriptSegment(line, true, null);
                appendTimelineFromSegment({ text: line, timestamp: null });
                timelineSeedLines.push(line);
            });
        } else {
            uiManager.showToast('离线识别完成，但未返回可展示文本', 'warning');
        }
    }

    bootstrapTimelineFromOfflineSegments(timelineSeedLines).catch((err) => {
        console.warn('离线转写时间线AI补全失败:', err);
    });

    // 显示总结
    if (result?.summary) {
        uiManager.showSummary({
            summary: result.summary,
            key_points: result.key_points || [],
            todos: result.todos || []
        });
    }

    window.dispatchEvent(new CustomEvent('asr-result', { detail: { source: 'offline' } }));
}

function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

// ==================== 认证功能 ====================

function showAuthModal(mode = 'login') {
    const modal = document.getElementById('authModal');
    if (!modal) return;

    modal.classList.remove('hidden');
    switchAuthMode(null, mode);
}

function closeAuthModal() {
    const modal = document.getElementById('authModal');
    if (!modal) return;

    modal.classList.add('hidden');
    // 清空表单
    document.getElementById('loginForm')?.reset();
    document.getElementById('registerForm')?.reset();
}

function switchAuthMode(e, mode) {
    if (e) e.preventDefault();

    const loginForm = document.getElementById('loginForm');
    const registerForm = document.getElementById('registerForm');
    const title = document.getElementById('authModalTitle');
    const switchText = document.getElementById('authSwitchText');
    const switchLink = document.querySelector('#authSwitchText + a');

    if (mode === 'login') {
        loginForm?.classList.remove('hidden');
        registerForm?.classList.add('hidden');
        title.textContent = '登录';
        switchText.textContent = '还没有账号？';
        switchLink.textContent = '立即注册';
        switchLink.onclick = (e) => switchAuthMode(e, 'register');
    } else {
        loginForm?.classList.add('hidden');
        registerForm?.classList.remove('hidden');
        title.textContent = '注册';
        switchText.textContent = '已有账号？';
        switchLink.textContent = '立即登录';
        switchLink.onclick = (e) => switchAuthMode(e, 'login');
    }
}

async function handleLogin(e) {
    e.preventDefault();

    const username = document.getElementById('loginUsername').value.trim();
    const password = document.getElementById('loginPassword').value;

    if (!username || !password) {
        uiManager.showToast('请输入用户名和密码', 'error');
        return;
    }

    try {
        uiManager.showToast('登录中...', 'info');

        const result = await authClient.login(username, password);
        authClient.setSession(result);

        closeAuthModal();
        updateUserUI();
        uiManager.showToast(`欢迎回来，${result.user.username}！`, 'success');

        // 重新加载会议列表
        await loadMeetings();

    } catch (err) {
        uiManager.showToast(`登录失败: ${err.message}`, 'error');
    }
}

async function handleRegister(e) {
    e.preventDefault();

    const username = document.getElementById('registerUsername').value;
    const email = document.getElementById('registerEmail').value;
    const password = document.getElementById('registerPassword').value;
    const passwordConfirm = document.getElementById('registerPasswordConfirm').value;

    if (password !== passwordConfirm) {
        uiManager.showToast('两次输入的密码不一致', 'error');
        return;
    }

    try {
        uiManager.showToast('注册中...', 'info');

        const result = await authClient.register(username, password, email);
        authClient.setSession(result);

        closeAuthModal();
        updateUserUI();
        uiManager.showToast(`注册成功，欢迎 ${result.user.username}！`, 'success');

        // 注册成功后立即加载该用户的会议列表
        await loadMeetings();

    } catch (err) {
        uiManager.showToast(`注册失败: ${err.message}`, 'error');
    }
}

function handleLogout() {
    authClient.clearSession();
    updateUserUI();
    uiManager.showToast('已退出登录', 'success');

    // 重新加载会议列表
    loadMeetings();
}

function updateUserUI() {
    const userInfo = document.getElementById('userInfo');
    const loginBtn = document.getElementById('loginBtn');
    const userMenu = document.getElementById('userMenu');
    if (userMenu) userMenu.classList.add('hidden');

    if (AppState.user) {
        userInfo?.classList.remove('hidden');
        loginBtn?.classList.add('hidden');

        const avatar = document.getElementById('userAvatar');
        const userName = document.getElementById('userName');

        if (avatar) avatar.textContent = AppState.user.username[0].toUpperCase();
        if (userName) userName.textContent = AppState.user.username;
    } else {
        userInfo?.classList.add('hidden');
        loginBtn?.classList.remove('hidden');
    }
}

// 点击遮罩关闭模态框
document.addEventListener('click', (e) => {
    const modal = document.getElementById('authModal');
    if (e.target === modal) {
        closeAuthModal();
    }
});

// 导出全局变量
window.AppState = AppState;
window.startRecording = startRecording;
window.stopRecording = stopRecording;
window.generateSummary = generateSummary;
window.clearOfflineFile = clearOfflineFile;
window.showAuthModal = showAuthModal;
window.closeAuthModal = closeAuthModal;
window.handleLogin = handleLogin;
window.handleRegister = handleRegister;
window.switchAuthMode = switchAuthMode;
window.handleLogout = handleLogout;
