// activation.js - Quản lý kích hoạt & chuyển đổi 3 loại License
// activation.js - Quản lý kích hoạt & chuyển đổi 3 loại License + Tích hợp mã 12 ký tự email cũ
let selectedDurationKey = '1m';

async function postJson(url, payload) {
    const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
        throw new Error(data.message || data.error || `HTTP ${res.status}`);
    }
    return data;
}

function setResult(message, ok) {
    const box = document.getElementById('activationResult');
    if (!box) return;
    box.className = ok ? 'ok' : 'err';
    box.style.display = 'block';
    box.textContent = message || '';
}

async function switchLicenseTier(tier) {
    const btn = event?.currentTarget;
    if (btn) {
        btn.disabled = true;
        btn.textContent = 'Đang chuyển...';
    }
    try {
        const data = await postJson('/api/activation/switch-tier', { tier: Number(tier) });
        setResult(data.message || `Đã chuyển sang ${data.license_info?.tierName || 'gói mới'}.`, true);
        setTimeout(() => {
            window.location.reload();
        }, 600);
    } catch (err) {
        setResult('Lỗi: ' + (err.message || 'Không thể chuyển gói.'), false);
        if (btn) {
            btn.disabled = false;
            btn.textContent = `Chuyển sang Loại ${tier}`;
        }
    }
}

async function generateSampleKey(tier, days) {
    try {
        const data = await postJson('/api/activation/generate-sample', { tier: Number(tier), days: Number(days || 30) });
        if (data.key) {
            const input = document.getElementById('activationCode');
            if (input) {
                input.value = data.key;
                input.focus();
                input.select();
            }
            setResult(`Đã tạo key mẫu ${data.tierName}: ${data.key}. Bấm "Kích hoạt License" để áp dụng.`, true);
        }
    } catch (err) {
        setResult('Lỗi tạo key: ' + err.message, false);
    }
}

// ─── Các hàm cho hệ thống check key thiết bị cũ (mã 12 ký tự qua email) ───────
function selectDurationOption(key, label, el) {
    selectedDurationKey = key;
    document.querySelectorAll('.plan-btn-card').forEach(card => card.classList.remove('active-plan'));
    if (el) el.classList.add('active-plan');
    const labelEl = document.getElementById('selectedDurationLabel');
    if (labelEl) {
        labelEl.innerHTML = `Gói đang chọn: <b>${label}</b>`;
    }
}

async function requestDeviceEmailCode(isResend = false) {
    const btn = document.getElementById(isResend ? 'resendActivationBtn' : 'requestActivationBtn');
    const originalText = btn ? btn.textContent : '';
    if (btn) {
        btn.disabled = true;
        btn.textContent = isResend ? 'Đang gửi lại...' : 'Đang gửi mã...';
    }
    try {
        const data = await postJson('/api/activation/resend', { duration_key: selectedDurationKey });
        setResult(data.message || 'Đã gửi mã kích hoạt 12 ký tự thành công tới email Admin.', true);
    } catch (err) {
        setResult('Không thể gửi mã: ' + (err.message || 'Lỗi kết nối'), false);
    } finally {
        if (btn) {
            btn.disabled = false;
            btn.textContent = originalText;
        }
    }
}

async function checkStatus() {
    try {
        const res = await fetch('/api/activation/status');
        const data = await res.json().catch(() => ({}));
        if (data.activated) {
            setResult(`Thiết bị đã kích hoạt: ${data.message || 'Đang hoạt động tốt'}.`, true);
        } else {
            setResult(`Thiết bị chưa kích hoạt: ${data.message || 'Chưa nhập mã hợp lệ'}.`, false);
        }
    } catch (err) {
        setResult('Lỗi kiểm tra trạng thái: ' + err.message, false);
    }
}

window.switchLicenseTier = switchLicenseTier;
window.generateSampleKey = generateSampleKey;
window.selectDurationOption = selectDurationOption;
window.requestDeviceEmailCode = requestDeviceEmailCode;
window.checkStatus = checkStatus;

document.addEventListener('DOMContentLoaded', () => {
    const saveBtn = document.getElementById('saveActivationBtn');
    const codeEl = document.getElementById('activationCode');

    if (codeEl) {
        // Cho phép nhập chữ, số và dấu gạch nối
        // Cho phép nhập chữ, số và dấu gạch nối (hỗ trợ cả mã 12 ký tự và mã license JM1/2/3)
        codeEl.addEventListener('input', () => {
            codeEl.value = (codeEl.value || '').toUpperCase().replace(/[^A-Z0-9-]/g, '').slice(0, 32);
        });
        codeEl.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                e.preventDefault();
                if (saveBtn) saveBtn.click();
            }
        });
    }

    if (saveBtn) {
        saveBtn.addEventListener('click', async () => {
            const code = (codeEl?.value || '').trim();
            if (!code) {
                setResult('Vui lòng nhập mã bản quyền (License Key).', false);
                return;
            }

            saveBtn.disabled = true;
            const originalText = saveBtn.textContent;
            saveBtn.textContent = 'Đang kiểm tra...';

            try {
                const data = await postJson('/api/activation/save', { code });
                setResult(data.message || 'Kích hoạt thành công!', true);
                setTimeout(() => {
                    // Nếu gói mới có defaultUrl thì về trang đó hoặc reload
                    const nextUrl = data.license_info?.defaultUrl || window.location.pathname;
                    window.location.href = nextUrl;
                }, 800);
            } catch (err) {
                setResult(err.message || 'Mã kích hoạt không hợp lệ.', false);
            } finally {
                saveBtn.disabled = false;
                saveBtn.textContent = originalText;
            }
        });
    }
});
