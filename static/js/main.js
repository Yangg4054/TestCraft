/* TestCraft main.js */

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
