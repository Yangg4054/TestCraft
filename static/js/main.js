/* TestCraft main.js */

// 轻量 toast：替代打断式 alert()，成功/失败反馈统一走这里
function tcToast(message, type) {
    const host = document.getElementById('tcToastHost');
    if (!host) { alert(message); return; }
    const node = document.createElement('div');
    node.className = 'tc-toast' + (type ? ' is-' + type : '');
    const icon = type === 'error' ? 'bi-exclamation-octagon' : (type === 'success' ? 'bi-check-circle' : 'bi-info-circle');
    node.innerHTML = '<i class="bi ' + icon + '"></i><span></span>';
    node.querySelector('span').textContent = message;
    host.appendChild(node);
    setTimeout(function () {
        node.classList.add('is-hiding');
        setTimeout(function () { node.remove(); }, 200);
    }, type === 'error' ? 5200 : 3200);
}

// 统一的 JSON 请求封装，服务端错误直接抛出便于 catch 后 toast
async function tcFetchJson(url, options) {
    const resp = await fetch(url, options || {});
    let data = {};
    try { data = await resp.json(); } catch (e) { data = {}; }
    if (!resp.ok || data.error) {
        throw new Error(data.error || ('请求失败（HTTP ' + resp.status + '）'));
    }
    return data;
}

// 按钮加载态，避免每个页面各写一份
function tcBusy(btn, label) {
    if (!btn) return function () {};
    const original = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>' + (label || '处理中...');
    return function restore() {
        btn.disabled = false;
        btn.innerHTML = original;
    };
}

function tcEscape(value) {
    const div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
}

// Theme toggle
(function () {
    const html = document.documentElement;
    const saved = localStorage.getItem('tc-theme');
    if (saved) {
        html.setAttribute('data-bs-theme', saved);
    }

    const toggle = document.getElementById('themeToggle');
    if (toggle) {
        const icon = toggle.querySelector('i');
        function updateIcon() {
            const dark = html.getAttribute('data-bs-theme') === 'dark';
            icon.className = dark ? 'bi bi-sun-fill' : 'bi bi-moon-fill';
        }
        updateIcon();

        toggle.addEventListener('click', function (e) {
            e.preventDefault();
            const current = html.getAttribute('data-bs-theme');
            const next = current === 'dark' ? 'light' : 'dark';
            html.setAttribute('data-bs-theme', next);
            localStorage.setItem('tc-theme', next);
            updateIcon();
        });
    }
})();

// Drag & drop for file inputs
function setupDropZone(zoneId, inputId, displayId) {
    const zone = document.getElementById(zoneId);
    const input = document.getElementById(inputId);
    const display = document.getElementById(displayId);
    if (!zone || !input) return;

    zone.addEventListener('click', function (e) {
        if (e.target.tagName !== 'BUTTON') {
            input.click();
        }
    });

    zone.addEventListener('dragover', function (e) {
        e.preventDefault();
        zone.classList.add('drag-over');
    });

    zone.addEventListener('dragleave', function () {
        zone.classList.remove('drag-over');
    });

    zone.addEventListener('drop', function (e) {
        e.preventDefault();
        zone.classList.remove('drag-over');
        if (e.dataTransfer.files.length) {
            input.files = e.dataTransfer.files;
            showFileName(display, e.dataTransfer.files[0].name);
        }
    });

    input.addEventListener('change', function () {
        if (input.files.length) {
            showFileName(display, input.files[0].name);
        }
    });
}

function showFileName(display, name) {
    if (!display) return;
    display.classList.remove('d-none');
    const span = display.querySelector('.name');
    if (span) span.textContent = name;
}

function clearFile(type) {
    if (type === 'doc') {
        document.getElementById('docFile').value = '';
        document.getElementById('docFileName').classList.add('d-none');
    } else {
        document.getElementById('codeFile').value = '';
        document.getElementById('codeFileName').classList.add('d-none');
    }
}

// Init drop zones
setupDropZone('docDropZone', 'docFile', 'docFileName');
setupDropZone('codeDropZone', 'codeFile', 'codeFileName');

// Form submit loading state
const form = document.getElementById('generateForm');
if (form) {
    form.addEventListener('submit', function () {
        const overlay = document.getElementById('loadingOverlay');
        if (overlay) {
            overlay.classList.remove('d-none');
        }
        const btn = document.getElementById('generateBtn');
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2"></span>Generating...';
        }
    });
}
