// ==== CONFIG & UI ELEMENTS ====
const PROXY_URL = "proxy.php";

const chatDisplay = document.getElementById("chat-display");
const centerStage = document.getElementById("center-stage");
const userInput = document.getElementById("user-input");
const sendBtn = document.getElementById("send-btn");

const userProfileBox = document.getElementById("user-profile-box");
const userAvatar = document.getElementById("user-avatar");
const userDisplayName = document.getElementById("user-display-name");
const authContainer = document.getElementById("auth-container");

const conversationListContainer = document.getElementById("conversation-list-container");
const conversationList = document.getElementById("conversation-list");

let isSending = false;

// ── Akıllı otomatik kaydırma ──────────────────────────────────
// Kullanıcı yukarı kaydırdıysa stream sırasında aşağı çekmez.
// Yeni mesaj gönderildiğinde veya konuşma yüklendiğinde sıfırlanır.
let _userScrolledUp = false;

function _initScrollDetection() {
    if (!chatDisplay) return;

    // Scroll-to-bottom butonu oluştur
    const _btn = document.createElement('button');
    _btn.id = 'scroll-to-bottom-btn';
    _btn.innerHTML = '<i class="fas fa-arrow-down"></i>';
    _btn.title = 'En alta git';
    _btn.style.cssText = `
        position: absolute;
        bottom: 140px;
        left: 50%;
        transform: translateX(-50%) translateY(12px);
        width: 48px;
        height: 48px;
        border-radius: 50%;
        background: #1a1a1f;
        border: 1.5px solid rgba(255,255,255,0.18);
        color: #ffffff;
        font-size: 16px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        box-shadow: 0 4px 20px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.04);
        opacity: 0;
        pointer-events: none;
        transition: opacity 0.22s ease, transform 0.22s ease, background 0.15s;
        z-index: 200;
    `;
    _btn.onmouseenter = () => { _btn.style.background = '#2a2a30'; };
    _btn.onmouseleave = () => { _btn.style.background = '#1a1a1f'; };
    _btn.onclick = () => {
        _userScrolledUp = false;
        chatDisplay.scrollTo({ top: chatDisplay.scrollHeight, behavior: 'smooth' });
        _btn.style.opacity = '0';
        _btn.style.pointerEvents = 'none';
        _btn.style.transform = 'translateX(-50%) translateY(12px)';
    };

    // chatDisplay'in parent'ına ekle (position:relative olması lazım)
    const parent = chatDisplay.parentElement;
    if (parent) {
        parent.style.position = 'relative';
        parent.appendChild(_btn);
    }

    chatDisplay.addEventListener('scroll', () => {
        const distFromBottom =
            chatDisplay.scrollHeight - chatDisplay.scrollTop - chatDisplay.clientHeight;
        _userScrolledUp = distFromBottom > 80;

        // Butonu göster/gizle
        if (_userScrolledUp && chatDisplay.style.display !== 'none') {
            _btn.style.opacity = '1';
            _btn.style.pointerEvents = 'auto';
            _btn.style.transform = 'translateX(-50%) translateY(0)';
        } else {
            _btn.style.opacity = '0';
            _btn.style.pointerEvents = 'none';
            _btn.style.transform = 'translateX(-50%) translateY(12px)';
        }
    }, { passive: true });
}

// force=true → kullanıcı konumu ne olursa olsun en alta git
function _scrollToBottom(force = false) {
    if (force || !_userScrolledUp) {
        chatDisplay.scrollTop = chatDisplay.scrollHeight;
    }
}
// ─────────────────────────────────────────────────────────────
let authMode = "";
let tempEmail = "";

let currentConversationId = localStorage.getItem('current_conversation_id') || null;
function setCurrentConversation(id) {
    currentConversationId = id;
    if (id) localStorage.setItem('current_conversation_id', id);
    else localStorage.removeItem('current_conversation_id');
}
let conversations = [];

let uploadedFile = null;
let uploadedImage = null;
let pastedTextContent = null;
let currentAbortController = null;
let userSubscription = null;

let currentChatMode = "assistant";
let codeModelEnabled = false;

const MODE_CONFIG = {
    assistant:  { icon: "fa-robot",          label: "Asistan",        placeholder: "Bir şeyler sor..." },
    code:       { icon: "fa-code",           label: "Kod Yazıcı",     placeholder: "Kod yaz, debug, refactor..." },
    it_expert:  { icon: "fa-server",         label: "IT Uzmanı",      placeholder: "Teknik sorunuzu detaylı sorun...", beta: true },
    student:    { icon: "fa-graduation-cap", label: "Öğrenci",        placeholder: "Ödev, ders veya sınav hakkında sor...", beta: true },
    social:     { icon: "fa-heart",          label: "Sosyal",         placeholder: "Yemek tarifi, seyahat, hobi, yaşam...", beta: true },
};

function setChatMode(mode) {
    const allowedModes = userSubscription?.features?.allowed_modes || ["assistant"];
    if (!allowedModes.includes(mode)) {
        const modeLabels = {
            code: "Kod Yazıcı",
            it_expert: "IT Uzmanı (BETA)",
            student: "Öğrenci Asistanı (BETA)",
            social: "Sosyal Asistan (BETA)",
        };
        showUpgradeModal(modeLabels[mode] || mode);
        closeModeDropdown();
        return;
    }

    if (mode === "code" && !codeModelEnabled) { 
        showToast("Kod modu henüz aktif değil", "warn"); 
        return; 
    }

    currentChatMode = mode;
    const cfg = MODE_CONFIG[mode] || MODE_CONFIG.assistant;

    // Mode UI kaldırıldı — sadece placeholder güncellenir
    if (!uploadedFile && !uploadedImage) userInput.placeholder = cfg.placeholder;
    updateHintChips(mode);
    closeModeDropdown();
}

function toggleModeDropdown() {
    const menu = document.getElementById('mode-dropdown');
    const trigger = document.getElementById('mode-trigger');
    if (menu.classList.contains('show')) {
        closeModeDropdown();
    } else {
        menu.classList.add('show');
        trigger.classList.add('open');
    }
}

function closeModeDropdown() {
    const menu = document.getElementById('mode-dropdown');
    const trigger = document.getElementById('mode-trigger');
    if (menu) menu.classList.remove('show');
    if (trigger) trigger.classList.remove('open');
}

function selectMode(mode) {
    setChatMode(mode);
}

document.addEventListener('click', (e) => {
    if (!e.target.closest('.mode-dropdown-wrapper')) {
        closeModeDropdown();
    }
});

async function checkCodeModeStatus() {
    try {
        const token = localStorage.getItem('token');
        const res = await fetch(PROXY_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "code_mode_status", token })
        });
        const data = await res.json();
        codeModelEnabled = data.enabled === true;

        if (data.allowed_modes) {
            if (!userSubscription) userSubscription = {};
            userSubscription.features = userSubscription.features || {};
            userSubscription.features.allowed_modes = data.allowed_modes;
            userSubscription.plan_id = data.user_plan || 'free';
            userSubscription.is_premium = data.is_premium || false;
        }

        document.querySelectorAll('.mode-dropdown-item').forEach(item => {
            const mode = item.dataset.mode;
            let enabled = true;
            if (mode === 'code') enabled = codeModelEnabled;
            if (mode === 'it_expert' || mode === 'student' || mode === 'social') enabled = true; // Görünür ama locked
            item.style.opacity = enabled ? "1" : "0.35";
            item.style.pointerEvents = enabled ? "auto" : "auto";
        });

        updateModeDropdownLocks();
        console.log("[MODES] Code:", codeModelEnabled);
    } catch (e) {
        codeModelEnabled = false;
        console.log("[MODES] Status check failed:", e.message);
    }
}

function showToast(msg, type) {
    const ex = document.getElementById("toast-notification");
    if (ex) ex.remove();
    const t = document.createElement("div");
    t.id = "toast-notification";
    const colors = {
        warn:  { bg: "rgba(255,180,0,0.12)",  border: "rgba(255,180,0,0.3)",  text: "#ffb400" },
        error: { bg: "rgba(255,75,75,0.12)",   border: "rgba(255,75,75,0.3)",  text: "#ff4b4b" },
        info:  { bg: "rgba(0,242,254,0.1)",    border: "rgba(0,242,254,0.2)",  text: "#00f2fe" },
    };
    const c = colors[type] || colors.info;
    t.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);padding:10px 20px;border-radius:10px;font-size:12px;font-weight:600;color:${c.text};background:${c.bg};border:1px solid ${c.border};z-index:99999;font-family:inherit;backdrop-filter:blur(8px);animation:msg-in 0.3s ease;`;
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity 0.3s"; }, 2500);
    setTimeout(() => t.remove(), 2900);
}

const ASSISTANT_HINTS = [
    { icon: "fa-lightbulb", text: "Bana bir şey öğret", prompt: "Bugün bana bir şey öğret" },
    { icon: "fa-image", text: "Görsel analiz", prompt: "Bu görseli analiz et" },
    { icon: "fa-book-open", text: "Açıkla", prompt: "Şunu açıkla: " },
    { icon: "fa-wand-magic-sparkles", text: "Fikir öner", prompt: "Bir fikir öner: " },
];
const CODE_HINTS = [
    { icon: "fa-file-code", text: "FastAPI endpoint", prompt: "FastAPI ile bir REST API endpoint yaz" },
    { icon: "fa-bug", text: "Hata bul", prompt: "Bu kodda hata bul: " },
    { icon: "fa-arrows-rotate", text: "Refactor et", prompt: "Bu kodu refactor et: " },
    { icon: "fa-camera", text: "Screenshot analiz", prompt: "Bu ekran görüntüsündeki hatayı bul ve düzeltilmiş kodu yaz" },
];

function updateHintChips(mode) {
    const container = document.querySelector('.quick-hints');
    if (!container) return;
    const hintMap = { code: CODE_HINTS };
    const hints = hintMap[mode] || ASSISTANT_HINTS;
    container.innerHTML = hints.map(h =>
        `<div class="hint-chip" onclick="fillPrompt('${h.prompt.replace(/'/g, "\\'")}')">
            <i class="fas ${h.icon}"></i>${h.text}
        </div>`
    ).join('');
}

const FILE_ICONS = {
    pdf: "fa-file-pdf", docx: "fa-file-word", csv: "fa-file-csv",
    python: "fa-file-code", javascript: "fa-file-code", typescript: "fa-file-code",
    java: "fa-file-code", go: "fa-file-code", rust: "fa-file-code",
    c: "fa-file-code", cpp: "fa-file-code", sql: "fa-database",
    bash: "fa-terminal", yaml: "fa-file-lines", json: "fa-file-code",
    html: "fa-file-code", css: "fa-file-code", markdown: "fa-file-lines",
    dockerfile: "fa-docker", terraform: "fa-file-code", log: "fa-file-lines", text: "fa-file-lines",
};

const FILE_TYPE_NAMES = {
    pdf: "PDF", docx: "Word", csv: "CSV", python: "Python", javascript: "JavaScript",
    typescript: "TypeScript", java: "Java", go: "Go", rust: "Rust", c: "C", cpp: "C++",
    sql: "SQL", bash: "Shell", yaml: "YAML", json: "JSON", html: "HTML", css: "CSS",
    markdown: "Markdown", dockerfile: "Dockerfile", terraform: "Terraform", log: "Log", text: "Text",
};

let _autofillCleaned = false;
function cleanAutofill() {
    if (_autofillCleaned || !userInput) return;
    const val = userInput.value;
    if (val.includes('@') && !val.includes(' ') && val.length > 5) {
        userInput.value = "";
    }
    _autofillCleaned = true;
}

document.addEventListener('DOMContentLoaded', async () => {
    const savedName  = localStorage.getItem('user_name');
    const savedToken = localStorage.getItem('token');

    // iyzico callback sonrası premium=1
    if (new URLSearchParams(window.location.search).get('premium') === '1') {
        history.replaceState({}, '', '/');
        if (savedToken) {
            await loadSubscriptionStatus();
            showToast('🎉 Premium üyeliğiniz aktif edildi!', 'info');
        }
    }

    if (savedName && savedToken) {
        updateUIForLoggedInUser(savedName);
        await loadSubscriptionStatus();
        // Profil yükle ve avatar stilini al
        try {
            const _pRes = await fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ action:'get_profile', token: savedToken }) });
            const _pData = await _pRes.json();
            if (_pData.user?.avatar_style) {
                localStorage.setItem('avatar_style', _pData.user.avatar_style);
                // Avatar src'i güncelle
                const _av = document.getElementById('user-avatar');
                if (_av) {
                    _av.src = `/avatars/${_pData.user.avatar_style}.svg`;
                }
            }
        } catch(e) {}
        await loadConversations();
        
        const lastConvId = localStorage.getItem('current_conversation_id');
        if (lastConvId && conversations.find(c => c.id === lastConvId)) {
            loadConversation(lastConvId);
        }
    }

    checkCodeModeStatus();
    _initScrollDetection();

    [10, 100, 300, 500, 1000].forEach(delay => setTimeout(cleanAutofill, delay));
    userInput.addEventListener('focus', cleanAutofill);

    if (typeof marked !== 'undefined') {
        marked.setOptions({
            breaks: true,
            gfm: true,
            highlight: function (code, lang) {
                if (lang && Prism.languages[lang]) {
                    return Prism.highlight(code, Prism.languages[lang], lang);
                }
                return code;
            }
        });
    }
});

function updateUIForLoggedInUser(name) {
    if (authContainer) authContainer.style.display = "none";
    if (userProfileBox) {
        userProfileBox.style.display = "flex";
        userDisplayName.textContent = name;
        const _avatarStyle = localStorage.getItem('avatar_style') || 'av-01';
        userAvatar.src = `/avatars/${_avatarStyle}.svg`;
    }
    if (conversationListContainer) conversationListContainer.style.display = "flex";
}

function handleLogout() {
    if (confirm("Çıkış yapmak istediğinize emin misiniz?")) {
        userSubscription = null;
        localStorage.clear();
        location.reload();
    }
}

function showEmailStep(mode) {
    authMode = mode;
    document.getElementById("auth-choice-step").style.display = "none";
    document.getElementById("email-step").style.display = "block";
    document.getElementById("name-field").style.display = (mode === 'register') ? "block" : "none";
    const stepLabel = document.getElementById("auth-step-label");
    const stepIcon = document.getElementById("auth-step-icon");
    if (mode === 'register') {
        stepLabel.textContent = "Kayıt Ol / Register";
        if (stepIcon) { stepIcon.classList.add("register-icon-v2"); stepIcon.innerHTML = '<i class="fas fa-user-plus"></i>'; }
        const agr = document.getElementById("register-agreements");
        if (agr) { agr.style.display = "block"; agr.style.visibility = "visible"; }
    } else {
        stepLabel.textContent = "Giriş Yap / Login";
        if (stepIcon) { stepIcon.classList.remove("register-icon-v2"); stepIcon.innerHTML = '<i class="fas fa-envelope"></i>'; }
        const agr = document.getElementById("register-agreements");
        if (agr) { agr.style.display = "none"; agr.style.visibility = ""; }
    }
}

function resetAuth() {
    document.getElementById("auth-choice-step").style.display = "block";
    document.getElementById("email-step").style.display = "none";
    document.getElementById("code-step").style.display = "none";
}

async function handleCodeRequest() {
    const email = document.getElementById("auth-email").value.trim();
    const name = document.getElementById("auth-name") ? document.getElementById("auth-name").value.trim() : "";
    if (!email.includes('@')) return alert("Geçerli bir mail girin.");
    // Kayıt modunda KVKK zorunlu
    if (authMode === "register") {
        if (name.length < 2) return alert("Lütfen adınızı girin.");
        const kvkk = document.getElementById("chk-reg-kvkk");
        if (kvkk && !kvkk.checked) {
            kvkk.style.outline = "2px solid #ff4b4b";
            return alert("Devam etmek için KVKK ve Kullanım Koşulları'nı onaylamanız gerekiyor.");
        }
        if (kvkk) kvkk.style.outline = "";
    }
    tempEmail = email;
    try {
        const res = await fetch(PROXY_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "request_code", email, mode: authMode, name })
        });
        const data = await res.json();
        if (res.ok && data.status === "success") {
            document.getElementById("email-step").style.display = "none";
            document.getElementById("code-step").style.display = "block";
            document.getElementById("code-sent-info").textContent = email + " adresine kod gönderildi.";
        } else {
            alert(data.detail || data.message || "Bir hata oluştu.");
        }
    } catch (e) {
        alert("Kod gönderilemedi.");
    }
}

async function verifyAndFinish() {
    const code = document.getElementById("auth-code").value.trim();
    try {
        const res = await fetch(PROXY_URL, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action: "verify_code", email: tempEmail, code, mode: authMode })
        });
        const data = await res.json();
        if (res.ok && data.status === "success") {
            localStorage.setItem('token', data.token);
            localStorage.setItem('user_name', data.user.name);
            location.reload();
        } else {
            alert(data.detail || data.message || "Doğrulama hatası.");
        }
    } catch (e) {
        alert("Doğrulama hatası.");
    }
}

function handleFileSelect(event) {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) { alert("Dosya çok büyük. Maksimum 10MB."); event.target.value = ""; return; }

    const imageTypes = ["image/png", "image/jpeg", "image/jpg", "image/webp"];
    if (imageTypes.includes(file.type)) {
        handleImageUpload(file);
    } else {
        if (userSubscription && !userSubscription.is_premium && !userSubscription?.features?.file_upload) {
            showUpgradeModal("Dosya Yükleme");
            event.target.value = "";
            return;
        }
        uploadFile(file);
    }
    event.target.value = "";
}

function handleImageUpload(file) {
    const chipArea = document.getElementById("file-chip-area");
    const chip = document.getElementById("file-chip");
    const chipName = document.getElementById("file-chip-name");
    const chipMeta = document.getElementById("file-chip-meta");
    const chipIcon = document.getElementById("file-chip-icon");

    chipArea.style.display = "block";
    chip.classList.add("file-uploading");
    chipName.textContent = file.name || "Yapıştırılan görsel";
    chipMeta.textContent = formatFileSize(file.size);
    chipIcon.className = "fas fa-spinner fa-spin";

    const MAX_DIM = 1600;
    const QUALITY = 0.85;

    const img = new Image();
    img.onload = function() {
        let w = img.width;
        let h = img.height;
        let needsResize = (w > MAX_DIM || h > MAX_DIM || file.size > 2 * 1024 * 1024);

        if (needsResize) {
            if (w > h) { if (w > MAX_DIM) { h = Math.round(h * MAX_DIM / w); w = MAX_DIM; } }
            else { if (h > MAX_DIM) { w = Math.round(w * MAX_DIM / h); h = MAX_DIM; } }
            const canvas = document.createElement('canvas');
            canvas.width = w; canvas.height = h;
            const ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0, w, h);
            const compressedDataUrl = canvas.toDataURL('image/jpeg', QUALITY);
            const base64Data = compressedDataUrl.split(",")[1];
            finishImageUpload(base64Data, "image/jpeg", file.name || "screenshot.jpg", base64Data.length);
        } else {
            const reader = new FileReader();
            reader.onload = function(e) {
                const base64Data = e.target.result.split(",")[1];
                finishImageUpload(base64Data, file.type || "image/png", file.name || "image.png", file.size);
            };
            reader.readAsDataURL(file);
        }
    };
    img.onerror = function() {
        const reader = new FileReader();
        reader.onload = function(e) {
            const base64Data = e.target.result.split(",")[1];
            finishImageUpload(base64Data, file.type || "image/png", file.name || "image.png", file.size);
        };
        reader.onerror = function() { removeUploadedFile(); alert("Görsel okunamadı."); };
        reader.readAsDataURL(file);
    };
    img.src = URL.createObjectURL(file);
}

function finishImageUpload(base64Data, mimeType, filename, displaySize) {
    const chip = document.getElementById("file-chip");
    const chipMeta = document.getElementById("file-chip-meta");
    const chipIcon = document.getElementById("file-chip-icon");

    uploadedImage = { base64: base64Data, mime_type: mimeType, filename: filename, size: displaySize };
    chip.classList.remove("file-uploading");
    chipIcon.className = "fas fa-image";
    chipMeta.textContent = `Görsel · ${formatFileSize(displaySize)}`;
    userInput.placeholder = `"${filename}" hakkında sor...`;
    userInput.focus();
    
    // Görsel yüklendi - mode değişikliği YOK
    // Assistant ve Code mode'lar zaten görsel analiz yapabilir
    showToast("Görsel yüklendi - analiz için soru sorabilirsin", "info");
}

async function uploadFile(file) {
    const chipArea = document.getElementById("file-chip-area");
    const chip = document.getElementById("file-chip");
    const chipName = document.getElementById("file-chip-name");
    const chipMeta = document.getElementById("file-chip-meta");
    const chipIcon = document.getElementById("file-chip-icon");

    chipArea.style.display = "block";
    chip.classList.add("file-uploading");
    chipName.textContent = file.name;
    chipMeta.textContent = formatFileSize(file.size);
    chipIcon.className = "fas fa-spinner fa-spin";

    try {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("action", "upload_file");
        const token = localStorage.getItem("token");
        if (token) formData.append("token", token);

        const res = await fetch(PROXY_URL, { method: "POST", body: formData });
        const data = await res.json();

        if (res.ok && data.status === "success") {
            uploadedFile = { file_id: data.file_id, filename: data.filename, file_type: data.file_type, char_count: data.char_count, line_count: data.line_count, preview: data.preview, size_bytes: data.size_bytes };
            chip.classList.remove("file-uploading");
            const iconClass = FILE_ICONS[data.file_type] || "fa-file";
            chipIcon.className = `fas ${iconClass}`;
            const typeName = FILE_TYPE_NAMES[data.file_type] || data.file_type;
            chipMeta.textContent = `${typeName} · ${formatFileSize(data.size_bytes)} · ${data.line_count} satır`;
            userInput.placeholder = `"${file.name}" hakkında sor...`;
            userInput.focus();

            const codeTypes = ["python","javascript","typescript","java","go","rust","c","cpp","bash","sql","dockerfile","terraform"];
            if (codeTypes.includes(data.file_type) && codeModelEnabled) {
                setChatMode("code");
                showToast("Kod dosyası algılandı — Kod moduna geçildi", "info");
            }
        } else {
            removeUploadedFile();
            alert(data.detail || data.message || "Dosya yüklenemedi.");
        }
    } catch (e) {
        removeUploadedFile();
        alert("Dosya yükleme hatası: " + e.message);
    }
}

function removeUploadedFile(clearActive = false) {
    uploadedFile = null;
    if (clearActive) window._activeConvFile = null;  // Sadece X butonunda temizle
    uploadedImage = null;
    document.getElementById("file-chip-area").style.display = "none";
    document.getElementById("file-chip").classList.remove("file-uploading");
    const cfg = MODE_CONFIG[currentChatMode] || MODE_CONFIG.assistant;
    userInput.placeholder = cfg.placeholder;
    document.getElementById("file-input").value = "";
}

function formatFileSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
    return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function showStopButton() { sendBtn.innerHTML = '<i class="fas fa-stop"></i>'; sendBtn.classList.add("stop-mode"); }
function showSendButton() { sendBtn.innerHTML = '<i class="fas fa-arrow-up"></i>'; sendBtn.classList.remove("stop-mode"); }
function stopStream() { if (currentAbortController) { currentAbortController.abort(); currentAbortController = null; } isSending = false; showSendButton(); userInput.focus(); }

async function loadConversations() {
    const token = localStorage.getItem('token');
    if (!token) return;
    try {
        const res = await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "list_conversations", token }) });
        const data = await res.json();
        if (data.conversations) { conversations = data.conversations; renderConversations(); }
    } catch (e) { console.error("Failed to load conversations:", e); }
}

function renderConversations() {
    conversationList.innerHTML = "";
    if (conversations.length === 0) {
        conversationList.innerHTML = `
            <div style="text-align:center;padding:28px 12px;">
                <i class="far fa-comment-dots" style="font-size:26px;display:block;margin-bottom:10px;color:rgba(255,255,255,0.12);"></i>
                <div style="font-size:13px;color:rgba(255,255,255,0.3);font-weight:500;">Henüz konuşma yok</div>
                <div style="font-size:11px;color:rgba(255,255,255,0.18);margin-top:4px;">Yeni Sohbet ile başla</div>
            </div>`;
        return;
    }

    // Tarihe göre grupla: Bugün / Bu Hafta / Daha Eski
    const now        = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const weekStart  = todayStart - 7 * 24 * 60 * 60 * 1000;

    const groups = { today: [], week: [], older: [] };
    conversations.forEach(conv => {
        const raw = conv.updated_at || conv.created_at || '';
        const t   = new Date(raw).getTime();
        if (isNaN(t) || t >= todayStart)     groups.today.push(conv);
        else if (t >= weekStart)             groups.week.push(conv);
        else                                 groups.older.push(conv);
    });

    // Debug: kaç konuşma var?
    console.log('[CONV] Toplam:', conversations.length,
        '| Bugün:', groups.today.length,
        '| Hafta:', groups.week.length,
        '| Eski:', groups.older.length);

    const renderGroup = (label, list) => {
        if (!list.length) return;

        // Grup başlığı
        const header = document.createElement('div');
        header.style.cssText = 'font-size:10px;color:rgba(255,255,255,0.25);text-transform:uppercase;letter-spacing:1px;font-weight:600;padding:10px 4px 4px;';
        header.textContent = label;
        conversationList.appendChild(header);

        list.forEach(conv => {
            const item = document.createElement('div');
            item.className = 'conversation-item';
            if (conv.id === currentConversationId) item.classList.add('active');

            const title   = conv.title || 'Yeni Sohbet';
            const timeAgo = formatTimeAgo(conv.updated_at || conv.created_at);
            const isActive = conv.id === currentConversationId;

            // Rastgele ama tutarlı renk — id'den türet
            const hues = [195, 210, 260, 280, 160, 30, 340];
            const hue  = hues[parseInt(conv.id.replace(/-/g,'').slice(0,8), 16) % hues.length];
            const dotColor = isActive
                ? '#00f2fe'
                : `hsl(${hue}, 60%, 62%)`;
            const bgActive = isActive
                ? `linear-gradient(135deg, rgba(0,242,254,0.08), rgba(0,242,254,0.04))`
                : 'transparent';

            item.style.background = isActive ? bgActive : '';

            item.innerHTML = `
                <div style="display:flex;align-items:center;gap:9px;">
                    <div style="
                        width:7px;height:7px;border-radius:50%;
                        background:${dotColor};
                        flex-shrink:0;
                        box-shadow:${isActive ? '0 0 6px rgba(0,242,254,0.5)' : 'none'};
                        transition:all 0.2s;
                        opacity:${isActive ? '1' : '0.5'};
                    "></div>
                    <div style="flex:1;min-width:0;">
                        <div class="conv-title" style="
                            font-size:13px;
                            font-weight:${isActive ? '600' : '450'};
                            color:${isActive ? '#fff' : 'rgba(255,255,255,0.72)'};
                            letter-spacing:${isActive ? '-0.1px' : '0'};
                        ">${escapeHtml(title)}</div>
                        <div style="font-size:11px;color:rgba(255,255,255,0.28);margin-top:2px;">${timeAgo}</div>
                    </div>
                    <span class="conv-delete" onclick="deleteConversation('${conv.id}', event)"
                        style="opacity:0;padding:4px 6px;border-radius:6px;color:rgba(255,255,255,0.25);transition:all 0.15s;flex-shrink:0;"
                        onmouseover="this.style.color='#ff6b6b';this.style.background='rgba(255,75,75,0.1)'"
                        onmouseout="this.style.color='rgba(255,255,255,0.25)';this.style.background=''">
                        <i class="fas fa-trash-alt" style="font-size:10px;"></i>
                    </span>
                </div>`;

            // Hover'da silme butonu göster
            item.onmouseenter = () => { const d = item.querySelector('.conv-delete'); if(d) d.style.opacity='1'; };
            item.onmouseleave = () => { const d = item.querySelector('.conv-delete'); if(d) d.style.opacity='0'; };

            item.onclick = (e) => { if (!e.target.closest('.conv-delete')) loadConversation(conv.id); };
            conversationList.appendChild(item);
        });
    };

    renderGroup('Bugün', groups.today);
    renderGroup('Bu Hafta', groups.week);
    renderGroup('Daha Eski', groups.older);
}

// ── Konuşma arama / filtreleme ──────────────────────────────
function filterConversations(query) {
    const q = query.trim().toLowerCase();
    if (!q) {
        renderConversations();
        return;
    }
    const filtered = conversations.filter(c =>
        (c.title || "Yeni Sohbet").toLowerCase().includes(q)
    );
    conversationList.innerHTML = "";
    if (filtered.length === 0) {
        conversationList.innerHTML = `<div style="text-align:center;color:var(--text-muted);font-size:12px;padding:20px 0;"><i class="fas fa-search" style="display:block;margin-bottom:8px;opacity:0.3;font-size:18px;"></i>Sonuç bulunamadı</div>`;
        return;
    }
    filtered.forEach(conv => {
        const item = document.createElement("div");
        item.className = "conversation-item";
        if (conv.id === currentConversationId) item.classList.add("active");
        const title = conv.title || "Yeni Sohbet";
        const timeAgo = formatTimeAgo(conv.updated_at || conv.created_at);
        // Eşleşen kısmı vurgula
        const highlighted = escapeHtml(title).replace(
            new RegExp(escapeHtml(q), 'gi'),
            m => `<mark style="background:rgba(0,242,254,0.25);color:var(--accent-cyan);border-radius:2px;">${m}</mark>`
        );
        item.innerHTML = `<div class="conv-title">${highlighted}</div><div class="conv-meta"><span>${timeAgo}</span><span class="conv-delete" onclick="deleteConversation('${conv.id}', event)"><i class="fas fa-trash-alt"></i></span></div>`;
        item.onclick = (e) => { if (!e.target.closest('.conv-delete')) { loadConversation(conv.id); document.getElementById('conv-search').value = ''; filterConversations(''); } };
        conversationList.appendChild(item);
    });
}
// ─────────────────────────────────────────────────────────────

async function loadConversation(convId) {
    window._activeConvFile = null;  // Farklı konuşmaya geçince önceki dosya unutulsun
    const token = localStorage.getItem('token');
    if (!token) return;
    try {
        const res = await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "get_conversation", conversation_id: convId, token }) });
        const data = await res.json();
        if (data.messages && data.messages.length > 0) {
            setCurrentConversation(convId);
            chatDisplay.innerHTML = "";
            centerStage.style.display = "none";
            chatDisplay.style.display = "flex";
            let lastUserMsg = "";
            data.messages.forEach((msg) => {
                const msgEl = createMessage(msg.role === "user");
                if (msg.role === "user") { msgEl.content.textContent = msg.content; lastUserMsg = msg.content; }
                else {
                    msgEl.content.innerHTML = renderMarkdown(msg.content);
                    if (msgEl.copyBtn) { const mc = msg.content; msgEl.copyBtn.onclick = () => { navigator.clipboard.writeText(mc).then(() => { msgEl.copyBtn.innerHTML = '<i class="fas fa-check" style="color:var(--accent-color)"></i>'; showToast("Kopyalandı!", "info"); setTimeout(() => { msgEl.copyBtn.innerHTML = '<i class="far fa-copy"></i>'; }, 2000); }); }; }
                    setupFeedbackButtons(msgEl.feedbackBar, lastUserMsg, msg.content);
                }
            });
            setTimeout(() => {
            _userScrolledUp = false;
            _scrollToBottom(true);
            const btn = document.getElementById('scroll-to-bottom-btn');
            if (btn) { btn.style.opacity = '0'; btn.style.pointerEvents = 'none'; btn.style.transform = 'translateX(-50%) translateY(12px)'; }
        }, 100);
            renderConversations();
            if (window.innerWidth < 768) { document.getElementById('sidebar').classList.remove('open'); document.getElementById('overlay').classList.remove('active'); }
        } else {
            setCurrentConversation(convId);
            chatDisplay.innerHTML = "";
            centerStage.style.display = "none";
            chatDisplay.style.display = "flex";
            const emptyMsg = document.createElement("div");
            emptyMsg.style.cssText = "text-align:center;color:var(--text-muted);padding:48px 20px;font-size:13px;";
            emptyMsg.innerHTML = '<i class="far fa-comment-dots" style="font-size:28px;display:block;margin-bottom:10px;opacity:0.25;"></i>Bu konuşmada henüz mesaj yok.';
            chatDisplay.appendChild(emptyMsg);
            renderConversations();
        }
    } catch (e) { console.error("[LOAD CONVERSATION ERROR]", e); alert("Konuşma yüklenemedi: " + e.message); }
}

async function createNewChat() {
    window._activeConvFile = null;  // Yeni konuşmada önceki dosya unutulsun
    const token = localStorage.getItem('token');
    if (!token) { alert("Lütfen giriş yapın"); return; }
    try {
        const res = await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "create_conversation", token }) });
        const data = await res.json();
        if (data.id) { setCurrentConversation(data.id); conversations.unshift(data); chatDisplay.innerHTML = ""; centerStage.style.display = "flex"; chatDisplay.style.display = "none"; const _cs = document.getElementById('conv-search'); if(_cs) _cs.value = ''; renderConversations(); }
    } catch (e) { setCurrentConversation(null); chatDisplay.innerHTML = ""; centerStage.style.display = "flex"; chatDisplay.style.display = "none"; }
    removeUploadedFile();
    updateHintChips(currentChatMode);
    if (window.innerWidth < 768) { document.getElementById('sidebar').classList.remove('open'); document.getElementById('overlay').classList.remove('active'); }
}

async function deleteConversation(convId, event) {
    event.stopPropagation();
    if (!confirm("Bu konuşmayı silmek istediğinize emin misiniz?")) return;
    const token = localStorage.getItem('token');
    if (!token) return;
    try {
        await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete_conversation", conversation_id: convId, token }) });
        conversations = conversations.filter(c => c.id !== convId);
        if (currentConversationId === convId) { setCurrentConversation(null); chatDisplay.innerHTML = ""; centerStage.style.display = "flex"; chatDisplay.style.display = "none"; }
        renderConversations();
    } catch (e) { console.error("Failed to delete conversation:", e); }
}

async function deleteAllConversations() {
    if (conversations.length === 0) { showToast("Silinecek konuşma yok", "warn"); return; }
    if (!confirm(`Tüm konuşmalar (${conversations.length} adet) kalıcı olarak silinecek. Emin misiniz?`)) return;
    const token = localStorage.getItem('token');
    if (!token) return;
    try {
        await Promise.all(conversations.map(conv => fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "delete_conversation", conversation_id: conv.id, token }) }).catch(() => {})));
        conversations = []; setCurrentConversation(null); chatDisplay.innerHTML = ""; centerStage.style.display = "flex"; chatDisplay.style.display = "none"; renderConversations(); showToast("Tüm konuşmalar silindi", "info");
    } catch (e) { showToast("Silme sırasında hata oluştu", "error"); }
}

function createMessage(isUser) {
    const wrapper = document.createElement("div");
    wrapper.className = `message-wrapper ${isUser ? "user-msg" : "bot-msg"}`;
    const bubble = document.createElement("div");
    bubble.className = isUser ? "user-bubble" : "bot-bubble";
    const content = document.createElement("div");
    content.className = "msg-text";
    bubble.appendChild(content);
    let copyBtn = null;
    let feedbackBar = null;
    if (!isUser) {
        const actionBar = document.createElement("div");
        actionBar.className = "msg-action-bar";
        copyBtn = document.createElement("button");
        copyBtn.className = "msg-action-btn copy-btn";
        copyBtn.innerHTML = '<i class="far fa-copy"></i>';
        copyBtn.title = "Kopyala";
        actionBar.appendChild(copyBtn);
        const likeBtn = document.createElement("button");
        likeBtn.className = "msg-action-btn feedback-btn like-btn";
        likeBtn.innerHTML = '<i class="far fa-thumbs-up"></i>';
        likeBtn.title = "Beğen";
        actionBar.appendChild(likeBtn);
        const dislikeBtn = document.createElement("button");
        dislikeBtn.className = "msg-action-btn feedback-btn dislike-btn";
        dislikeBtn.innerHTML = '<i class="far fa-thumbs-down"></i>';
        dislikeBtn.title = "Beğenme";
        actionBar.appendChild(dislikeBtn);
        bubble.appendChild(actionBar);
        feedbackBar = { likeBtn, dislikeBtn, actionBar };
    }
    wrapper.appendChild(bubble);
    chatDisplay.appendChild(wrapper);
    _scrollToBottom(false);
    return { content, copyBtn, feedbackBar };
}

function renderMarkdown(text) {
    if (typeof marked === 'undefined') return escapeHtml(text);
    const imageTags = [];
    text = text.replace(/\[IMAGE_B64\]([\s\S]*?)\[\/IMAGE_B64\]/g, (match, b64) => {
        const placeholder = `%%GENIMAGE_${imageTags.length}%%`;
        imageTags.push(`
        <div class="generated-image-card">
            <div class="generated-image-label-row">
                <span class="generated-image-badge">
                    <i class="fas fa-sparkles"></i> ONE-BUNE Görseli
                </span>
            </div>
        
            <img
                src="data:image/png;base64,${b64.trim()}"
                alt="Oluşturulan görsel"
                class="generated-image-fade"
                loading="lazy"
            >
        
            <div class="generated-image-footer">
                <div class="generated-image-tagline">AI ile oluşturuldu</div>
                <button onclick="downloadGeneratedImage(this)" class="code-copy-btn generated-image-download-btn">
                    <i class="fas fa-download"></i> İndir
                </button>
            </div>
        </div>
        `);
        return placeholder;
    });
    const codeBlocks = [];
    let processed = text.replace(/```(\w+)?\n([\s\S]*?)```/g, (match, lang, code) => {
        const language = lang || 'plaintext';
        let highlighted = code;
        try { if (Prism.languages[language]) highlighted = Prism.highlight(code, Prism.languages[language], language); } catch (e) { highlighted = escapeHtml(code); }
        const placeholder = `%%CODEBLOCK_${codeBlocks.length}%%`;
        codeBlocks.push(`<div class="code-block"><div class="code-header"><span class="code-lang">${language}</span><button class="code-copy-btn" onclick="copyCode(this)"><i class="far fa-copy"></i> <span>Kopyala</span></button></div><pre><code class="language-${language}">${highlighted}</code></pre></div>`);
        return placeholder;
    });
    let html = marked.parse(processed);
    codeBlocks.forEach((block, i) => { html = html.replace(`%%CODEBLOCK_${i}%%`, block); html = html.replace(`<p>%%CODEBLOCK_${i}%%</p>`, block); });
    imageTags.forEach((imgHtml, i) => { html = html.replace(`%%GENIMAGE_${i}%%`, imgHtml); html = html.replace(`<p>%%GENIMAGE_${i}%%</p>`, imgHtml); });
    return html;
}

function downloadGeneratedImage(btn) {
    const img = btn.closest('div').parentElement.querySelector('img');
    if (!img) return;
    const a = document.createElement('a'); a.href = img.src; a.download = 'onebune-generated-' + Date.now() + '.png'; document.body.appendChild(a); a.click(); document.body.removeChild(a);
    btn.innerHTML = '<i class="fas fa-check"></i> İndirildi'; setTimeout(() => { btn.innerHTML = '<i class="fas fa-download"></i> İndir'; }, 2000);
}

function copyCode(btn) {
    const codeBlock = btn.closest('.code-block').querySelector('code');
    navigator.clipboard.writeText(codeBlock.textContent).then(() => {
        btn.innerHTML = '<i class="fas fa-check"></i> <span>Kopyalandı!</span>'; btn.style.color = 'var(--accent-color)'; showToast("Kopyalandı!", "info");
        setTimeout(() => { btn.innerHTML = '<i class="far fa-copy"></i> <span>Kopyala</span>'; btn.style.color = ''; }, 2000);
    });
}

async function sendFeedback(rating, userQuery, assistantResponse, likeBtn, dislikeBtn) {
    const token = localStorage.getItem('token');
    if (!token) return;
    try {
        await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "feedback", conversation_id: currentConversationId, user_query: userQuery.substring(0, 500), assistant_response: assistantResponse.substring(0, 1000), rating, token }) });
        if (rating === 1) { likeBtn.innerHTML = '<i class="fas fa-thumbs-up"></i>'; likeBtn.classList.add("active"); dislikeBtn.classList.remove("active"); dislikeBtn.innerHTML = '<i class="far fa-thumbs-down"></i>'; }
        else { dislikeBtn.innerHTML = '<i class="fas fa-thumbs-down"></i>'; dislikeBtn.classList.add("active"); likeBtn.classList.remove("active"); likeBtn.innerHTML = '<i class="far fa-thumbs-up"></i>'; }
    } catch (e) { console.error("[FEEDBACK ERROR]", e); }
}

function setupFeedbackButtons(feedbackBar, userQuery, assistantResponse) {
    if (!feedbackBar) return;
    feedbackBar.likeBtn.onclick = () => sendFeedback(1, userQuery, assistantResponse, feedbackBar.likeBtn, feedbackBar.dislikeBtn);
    feedbackBar.dislikeBtn.onclick = () => sendFeedback(-1, userQuery, assistantResponse, feedbackBar.likeBtn, feedbackBar.dislikeBtn);
}

async function handleChat() {
    if (isSending) return;
    cleanAutofill();
    let prompt = userInput.value.trim();
    if (!prompt && !uploadedFile && !uploadedImage && !pastedTextContent) return;
    if (!prompt && uploadedFile) prompt = `Bu dosyayı analiz et: ${uploadedFile.filename}`;
    if (!prompt && uploadedImage) prompt = `Bu görseli analiz et`;

    // Yapıştırılan metin varsa prompt'a ekle
    if (pastedTextContent) {
        const userQuestion = prompt || "Bu içeriği analiz et ve özetle";
        prompt = userQuestion + "\n\n```\n" + pastedTextContent + "\n```";
        _removePasteChip();
    }
    
    const token = localStorage.getItem('token');
    if (token) {
        console.log("[CHAT] Checking subscription status before sending...");
        await loadSubscriptionStatus();
        
        if (userSubscription && !userSubscription.is_premium) {
            const remaining = userSubscription.usage_today?.messages_remaining || 0;
            const limit = userSubscription.usage_today?.daily_message_limit || 50;
            
            if (remaining <= 0) {
                showToast(`⏰ Günlük mesaj limitine ulaştın (${limit}/gün). Premium'a yükselt veya yarın tekrar dene!`, "warn");
                return;
            }
        }
    }
    
    if (!currentConversationId) await createNewChat();

    isSending = true;
    _userScrolledUp = false;  // yeni mesaj → en alta dön
    const _stbBtn = document.getElementById('scroll-to-bottom-btn');
    if (_stbBtn) { _stbBtn.style.opacity = '0'; _stbBtn.style.pointerEvents = 'none'; _stbBtn.style.transform = 'translateX(-50%) translateY(12px)'; }
    centerStage.style.display = "none";
    chatDisplay.style.display = "flex";

    const fileForThisMsg = uploadedFile;
    const imageForThisMsg = uploadedImage;
    // Dosyayı konuşma boyunca hatırla — sadece görsel temizlenir
    // Dosya: konuşmada aktif kalır, kullanıcı X'e basarsa temizlenir
    if (imageForThisMsg) uploadedImage = null;
    // uploadedFile konuşma boyunca korunuyor — _activeConvFile'a kopyala
    if (fileForThisMsg && !window._activeConvFile) {
        window._activeConvFile = fileForThisMsg;
    }
    if (uploadedFile) removeUploadedFile();  // chip'i temizle ama _activeConvFile'da tutuluyor

    const userMsg = createMessage(true);
    if (imageForThisMsg) {
        userMsg.content.innerHTML = `<div class="file-msg-chip"><i class="fas fa-image"></i><span>${escapeHtml(imageForThisMsg.filename)}</span><span style="opacity:0.5;font-size:10px;">Görsel · ${formatFileSize(imageForThisMsg.size)}</span></div><div>${escapeHtml(prompt)}</div>`;
    } else if (fileForThisMsg) {
        const iconClass = FILE_ICONS[fileForThisMsg.file_type] || "fa-file";
        const typeName = FILE_TYPE_NAMES[fileForThisMsg.file_type] || fileForThisMsg.file_type;
        userMsg.content.innerHTML = `<div class="file-msg-chip"><i class="fas ${iconClass}"></i><span>${escapeHtml(fileForThisMsg.filename)}</span><span style="opacity:0.5;font-size:10px;">${typeName} · ${fileForThisMsg.line_count} satır</span></div><div>${escapeHtml(prompt)}</div>`;
    } else {
        userMsg.content.textContent = prompt;
    }
    userInput.value = "";
    userInput.style.height = 'auto';

    const botMsg = createMessage(false);
    // Mode badge kaldırıldı — tek asistan deneyimi
    botMsg.content.innerHTML = `<div class="typing-indicator"><span></span><span></span><span></span></div>`;

    showStopButton();

    // Kullanıcıya görünmeyen arka plan modu — gateway kendi routing yapıyor
    // Frontend sadece "assistant" gönderir, gateway intent'e göre model seçer
    const requestBody = { action: "chat", prompt, mode: "assistant", conversation_id: currentConversationId, token: localStorage.getItem('token') };
    // Dosya: bu mesajda yüklendiyse veya konuşmada daha önce yüklendiyse gönder
    const activeFile = fileForThisMsg || window._activeConvFile;
    if (activeFile) requestBody.file_id = activeFile.file_id;
    if (imageForThisMsg) { requestBody.image_data = imageForThisMsg.base64; requestBody.image_type = imageForThisMsg.mime_type; }

    currentAbortController = new AbortController();

    try {
        const response = await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(requestBody), signal: currentAbortController.signal });
        const reader = response.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let fullText = "";
        const IMAGE_LOADING_PLACEHOLDER = `
        <div class="onebune-image-loading">
            <div class="onebune-image-loading-wrap">
                <div class="onebune-loader-orb">
                    <div class="onebune-loader-core">
                        <div class="onebune-loader-text">ONE-BUNE</div>
                    </div>
                </div>
                <div class="onebune-loader-label">Görsel oluşturuluyor</div>
            </div>
        </div>`;        
        let firstChunk = true;

while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    fullText += decoder.decode(value, { stream: true });

    if (firstChunk) {
        botMsg.content.innerHTML = "";
        firstChunk = false;
    }

 let renderText = fullText;

const hasOpenTag = renderText.includes("[IMAGE_B64]");
const hasCloseTag = renderText.includes("[/IMAGE_B64]");

const isImageGeneratingText =
    fullText.includes("🎨 Görsel Oluşturuluyor") ||
    fullText.includes("🎨 Görsel oluşturuluyor") ||
    fullText.includes("Görsel Oluşturuluyor") ||
    fullText.includes("Görsel oluşturuluyor") ||
    fullText.includes("Bu işlem 30-60 saniye sürebilir") ||
    fullText.includes("Bu işlem 30–60 saniye sürebilir") ||
    fullText.includes("Bu işlem 30-60 saniye sürebilir...");

// Bu ara yazıları tamamen temizle
renderText = renderText
    .replace("🎨 Görsel Oluşturuluyor...", "")
    .replace("🎨 Görsel oluşturuluyor...", "")
    .replace("🖼️ Görsel yükleniyor...", "")
    .replace("Görsel Oluşturuluyor...", "")
    .replace("Görsel oluşturuluyor...", "")
    .replace("⏳ Bu işlem 30-60 saniye sürebilir...", "")
    .replace("⏳ Bu işlem 30–60 saniye sürebilir...", "")
    .replace("Bu işlem 30-60 saniye sürebilir...", "")
    .trim();

// image stream başlamış ama bitmemişse loader göster
if (hasOpenTag && !hasCloseTag) {
    const idx = renderText.indexOf("[IMAGE_B64]");
    renderText = renderText.substring(0, idx) + IMAGE_LOADING_PLACEHOLDER;
}

// image henüz gelmemiş ama backend ara mesaj gönderiyorsa yine loader göster
if (!hasOpenTag && isImageGeneratingText) {
    renderText = IMAGE_LOADING_PLACEHOLDER;
}

botMsg.content.innerHTML = renderText.includes("onebune-image-loading")
    ? renderText
    : renderMarkdown(renderText);

    _scrollToBottom(false);
}

        const cleanedFinalText = fullText
        .replace("🎨 Görsel oluşturuluyor...", "")
        .replace("🖼️ Görsel yükleniyor...", "")
        .replace("Görsel oluşturuluyor...", "")
        .trim();
    
    botMsg.content.innerHTML = renderMarkdown(cleanedFinalText);
    if (firstChunk) botMsg.content.innerHTML = "";

        if (botMsg.copyBtn) {
            botMsg.copyBtn.onclick = () => {
                const cleanText = fullText
               .replace("🎨 Görsel oluşturuluyor...", "")
               .replace("🖼️ Görsel yükleniyor...", "")
               .replace("Görsel oluşturuluyor...", "")
               .replace(/\[IMAGE_B64\][\s\S]*?\[\/IMAGE_B64\]/g, '[Oluşturulan görsel]')
               .trim();
                navigator.clipboard.writeText(cleanText).then(() => { botMsg.copyBtn.innerHTML = '<i class="fas fa-check" style="color:var(--accent-color)"></i>'; showToast("Kopyalandı!", "info"); setTimeout(() => { botMsg.copyBtn.innerHTML = '<i class="far fa-copy"></i>'; }, 2000); });
            };
        }

        setupFeedbackButtons(botMsg.feedbackBar, prompt, fullText);

        const conv = conversations.find(c => c.id === currentConversationId);
        if (conv && (conv.title === "Yeni Sohbet" || !conv.title)) {
            const smartTitle = generateSmartTitle(prompt);
            conv.title = smartTitle;
            renderConversations();
            updateConversationTitle(currentConversationId, smartTitle);
        }
        
        if (token) {
            console.log("[CHAT] Refreshing subscription after message sent...");
            await loadSubscriptionStatus();
        }

    } catch (e) {
        if (e.name === 'AbortError') {
            if (botMsg.content.textContent === '' || botMsg.content.querySelector('.typing-indicator')) {
                botMsg.content.innerHTML = '<span style="color:var(--text-muted);font-style:italic;">⏹ Yanıt durduruldu.</span>';
            }
        } else {
            botMsg.content.innerHTML = '<span style="color:var(--danger-color);">Bağlantı hatası. Lütfen tekrar deneyin.</span>';
        }
    } finally {
        isSending = false;
        currentAbortController = null;
        showSendButton();
        userInput.focus();
    }
}

sendBtn.addEventListener("click", () => { if (isSending) stopStream(); else handleChat(); });
userInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
        const val = userInput.value.trim();
        if (val.includes('@') && !val.includes(' ') && val.length > 5 && !_autofillCleaned) { e.preventDefault(); userInput.value = ""; return; }
        e.preventDefault();
        if (isSending) stopStream(); else handleChat();
    }
});

// Yapıştırılan metin için chip referansı
userInput.addEventListener("paste", (e) => {
    const clipboardData = e.clipboardData || window.clipboardData;
    if (!clipboardData) return;
    const items = clipboardData.items;

    // Görsel yapıştırma
    if (items) {
        for (let i = 0; i < items.length; i++) {
            const item = items[i];
            if (item.type.startsWith("image/")) {
                e.preventDefault();
                const file = item.getAsFile();
                if (file) { handleImageUpload(file); showToast("Görsel yapıştırıldı", "info"); }
                return;
            }
        }
    }

    // Büyük metin kontrolü — 300 karakterden fazlaysa chip yap
    const text = clipboardData.getData("text");
    if (text && text.length > 300) {
        e.preventDefault();
        if (text.length > 50000) {
            showToast("İçerik çok büyük — maksimum 50.000 karakter yapıştırabilirsiniz", "warn");
            return;
        }
        pastedTextContent = text;
        _showPasteChip(text);
        return;
    }

    setTimeout(() => { if (typeof autoResizeTextarea === 'function') autoResizeTextarea(); }, 10);
});

function _showPasteChip(text) {
    // Varsa eskiyi kaldır
    document.getElementById('paste-chip')?.remove();

    const lines    = text.split('\n').length;
    const words    = text.trim().split(/\s+/).length;
    const preview  = text.trim().substring(0, 60).replace(/\n/g, ' ') + (text.length > 60 ? '...' : '');
    const isCode   = /^[\s]*(def |function |class |import |const |let |var |SELECT |<!DOCTYPE|<html|{)/.test(text.trim());
    const icon     = isCode ? 'fa-code' : 'fa-align-left';
    const label    = isCode ? 'Kod' : 'Metin';

    const chipArea = document.getElementById('file-chip-area');
    chipArea.style.display = 'block';

    // Gerçek dosya yoksa file-chip'i gizle
    if (!uploadedFile && !uploadedImage) {
        const existingFileChip = document.getElementById('file-chip');
        if (existingFileChip) existingFileChip.style.display = 'none';
    }

    const chip = document.createElement('div');
    chip.id = 'paste-chip';
    chip.style.cssText = `
        display:flex;align-items:flex-start;gap:10px;
        background:rgba(255,255,255,0.04);
        border:1px solid rgba(255,255,255,0.1);
        border-radius:10px;padding:10px 12px;
        position:relative;max-width:280px;
        margin-bottom:6px;cursor:default;
    `;
    chip.innerHTML = `
        <div style="width:34px;height:34px;min-width:34px;border-radius:8px;
                    background:linear-gradient(135deg,rgba(0,242,254,.12),rgba(188,78,253,.1));
                    display:flex;align-items:center;justify-content:center;">
            <i class="fas ${icon}" style="font-size:13px;color:var(--accent-cyan);"></i>
        </div>
        <div style="flex:1;min-width:0;">
            <div style="font-size:12px;font-weight:600;color:#eeeef0;margin-bottom:2px;">
                Yapıştırılan ${label}
            </div>
            <div style="font-size:10px;color:#55556a;">
                ${lines} satır · ${words} kelime
            </div>
            <div style="font-size:10px;color:#8e8ea0;margin-top:3px;
                        white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
                        max-width:180px;">
                ${preview}
            </div>
        </div>
        <button onclick="_removePasteChip()" style="
            position:absolute;top:6px;right:6px;
            width:18px;height:18px;border-radius:50%;
            background:rgba(255,255,255,.08);border:none;
            color:#55556a;cursor:pointer;font-size:10px;
            display:flex;align-items:center;justify-content:center;">
            <i class="fas fa-xmark"></i>
        </button>
    `;

    chipArea.insertBefore(chip, chipArea.firstChild);
    userInput.placeholder = "Bu içerik hakkında ne sormak istersiniz?";
    userInput.focus();
}

function _removePasteChip() {
    document.getElementById('paste-chip')?.remove();
    pastedTextContent = null;
    userInput.placeholder = MODE_CONFIG[currentChatMode]?.placeholder || "Bir şeyler yazın...";
    // file-chip'i tekrar göster (varsa)
    const existingFileChip = document.getElementById('file-chip');
    if (existingFileChip) existingFileChip.style.display = '';
    // Başka içerik yoksa area'yı gizle
    const chipArea = document.getElementById('file-chip-area');
    if (chipArea && !uploadedFile && !uploadedImage) {
        chipArea.style.display = 'none';
    }
}

document.addEventListener("dragover", (e) => { e.preventDefault(); e.stopPropagation(); document.body.style.outline = "2px dashed var(--accent-color)"; document.body.style.outlineOffset = "-4px"; });
document.addEventListener("dragleave", (e) => { e.preventDefault(); document.body.style.outline = "none"; });
document.addEventListener("drop", (e) => { e.preventDefault(); e.stopPropagation(); document.body.style.outline = "none"; const files = e.dataTransfer.files; if (files.length > 0) { const file = files[0]; if (file.size > 10 * 1024 * 1024) { alert("Dosya çok büyük. Maksimum 10MB."); return; } uploadFile(file); } });

let ptrStartY = 0, ptrActive = false, ptrTriggered = false;
const ptr = document.getElementById("ptr-indicator");
const PTR_THRESHOLD = 110;
// Sadece chat kapalıysa veya kullanıcı gerçekten en üstteyse PTR tetikle
function isAtTop() { 
    return chatDisplay.style.display === "none" || 
           (chatDisplay.scrollTop <= 2 && !_userScrolledUp); 
}
window.addEventListener("touchstart", (e) => { 
    if (document.activeElement === userInput) return; 
    if (isAtTop()) { 
        ptrStartY = e.touches[0].clientY; 
        ptrActive = true; 
        ptrTriggered = false; 
    } else {
        ptrActive = false;
    }
}, { passive: true });
window.addEventListener("touchmove", (e) => { 
    if (!ptrActive) return; 
    const diff = e.touches[0].clientY - ptrStartY; 
    if (diff > 0 && diff < 160) { 
        ptr.style.transform = `translateY(${Math.min(diff, 80)}px)`; 
        ptrTriggered = diff > PTR_THRESHOLD; 
        ptr.classList.toggle("active", ptrTriggered); 
    } 
}, { passive: true });
window.addEventListener("touchend", () => { 
    if (!ptrActive) return; 
    if (ptrTriggered) location.reload(); 
    else { ptr.style.transform = "translateY(0)"; ptr.classList.remove("active"); } 
    ptrActive = false; 
});

function generateSmartTitle(prompt) {
    let title = prompt.trim();
    if (title.startsWith("Bu dosyayı analiz et:")) return title.replace("Bu dosyayı analiz et:", "📄").trim();
    const sentenceEnd = title.search(/[.!?\n]/);
    if (sentenceEnd > 0 && sentenceEnd < 60) title = title.substring(0, sentenceEnd);
    if (title.length > 50) { title = title.substring(0, 50); const lastSpace = title.lastIndexOf(' '); if (lastSpace > 30) title = title.substring(0, lastSpace); title += '…'; }
    return title || "Yeni Sohbet";
}

async function updateConversationTitle(convId, title) {
    const token = localStorage.getItem('token');
    if (!token || !convId) return;
    try { await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "update_conversation", conversation_id: convId, title, token }) }); } catch (e) { console.error("[TITLE UPDATE]", e); }
}

function formatTimeAgo(timestamp) {
    const diff = Math.floor((new Date() - new Date(timestamp)) / 1000);
    if (diff < 60) return "Az önce";
    if (diff < 3600) return `${Math.floor(diff / 60)} dk önce`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} saat önce`;
    if (diff < 604800) return `${Math.floor(diff / 86400)} gün önce`;
    return new Date(timestamp).toLocaleDateString('tr-TR', { day: 'numeric', month: 'short' });
}

function escapeHtml(text) { const div = document.createElement('div'); div.textContent = text; return div.innerHTML; }

async function loadSubscriptionStatus() {
    const token = localStorage.getItem('token');
    if (!token) { 
        userSubscription = null; 
        updateUIForSubscription(); 
        return; 
    }
    
    try {
        const res = await fetch(PROXY_URL, { 
            method: "POST", 
            headers: { "Content-Type": "application/json" }, 
            body: JSON.stringify({ action: "subscription_status", token }) 
        });
        
        const data = await res.json();
        
        if (data.plan_id) { 
            userSubscription = data; 
            localStorage.setItem('user_plan', data.plan_id); 
        } else { 
            userSubscription = { 
                plan_id: "free", 
                is_premium: false, 
                features: { allowed_modes: ["assistant"] }, 
                limits: { daily_messages: 50 },
                usage_today: { messages_sent: 0, messages_remaining: 50, daily_message_limit: 50 }
            }; 
        }
        
        console.log("[SUBSCRIPTION] Loaded:", {
            plan: userSubscription.plan_id,
            premium: userSubscription.is_premium,
            messages: userSubscription.usage_today?.messages_remaining || 'unknown',
            limit: userSubscription.usage_today?.daily_message_limit || 'unknown'
        });
        
    } catch (e) {
        console.error("[SUBSCRIPTION] Load failed:", e);
        userSubscription = { 
            plan_id: "free", 
            is_premium: false, 
            features: { allowed_modes: ["assistant"] }, 
            limits: { daily_messages: 50 },
            usage_today: { messages_sent: 0, messages_remaining: 50, daily_message_limit: 50 }
        };
    }
    
    updateUIForSubscription();
}

function updateUIForSubscription() {
    console.log("[UI UPDATE] Subscription:", userSubscription);
    updateModeDropdownLocks();
    updateSidebarPlanBadge();
    updateUsageIndicator();
    updateAttachButton();
}

function updateModeDropdownLocks() {
    const allowedModes = userSubscription?.features?.allowed_modes || ["assistant"];
    document.querySelectorAll('.mode-dropdown-item').forEach(item => {
        const mode = item.dataset.mode;
        const isAllowed = allowedModes.includes(mode);
        const lockIcon = item.querySelector('.mode-lock-icon');
        const checkIcon = item.querySelector('.item-check');
        if (!isAllowed) {
            item.classList.add('mode-locked');
            if (!lockIcon) {
                const lock = document.createElement('span');
                lock.className = 'mode-lock-icon';
                lock.innerHTML = '<i class="fas fa-lock"></i>';
                if (checkIcon) checkIcon.style.display = 'none';
                item.appendChild(lock);
            }
        } else {
            item.classList.remove('mode-locked');
            if (lockIcon) lockIcon.remove();
            if (checkIcon) checkIcon.style.display = '';
        }
    });
}

function updateSidebarPlanBadge() {
    let badge = document.getElementById('plan-badge-container');
    if (!badge) {
        badge = document.createElement('div');
        badge.id = 'plan-badge-container';
        const profileBox = document.getElementById('user-profile-box');
        if (profileBox && profileBox.parentNode) profileBox.parentNode.insertBefore(badge, profileBox.nextSibling);
    }
    
    if (!userSubscription || !localStorage.getItem('token')) { 
        badge.style.display = 'none'; 
        return; 
    }
    
    badge.style.display = 'block';

    if (userSubscription.is_premium) {
        badge.innerHTML = `
            <div class="plan-card premium-card" onclick="showSubscriptionPanel()">
                <div class="plan-card-header">
                    <div class="plan-badge-compact premium">
                        <i class="fas fa-crown"></i>
                        <span>Premium</span>
                    </div>
                    <i class="fas fa-chevron-right plan-card-arrow"></i>
                </div>
                <div class="plan-card-subtitle">Tüm özellikler aktif</div>
            </div>`;
    } else {
        const usage = userSubscription.usage_today || { messages_sent: 0, daily_message_limit: 50 };
        const sent = usage.messages_sent || 0;
        const limit = usage.daily_message_limit || 50;
        const remaining = usage.messages_remaining || (limit - sent);
        const pct = Math.min(100, Math.round((sent / limit) * 100));
        
        badge.innerHTML = `
            <div class="plan-card free-card" onclick="showUpgradeModal()">
                <div class="plan-card-header">
                    <div class="plan-badge-compact free">
                        <i class="fas fa-sparkles"></i>
                        <span>Free</span>
                    </div>
                    <button class="plan-upgrade-compact" onclick="event.stopPropagation(); showUpgradeModal();">
                        <i class="fas fa-arrow-up"></i> Upgrade
                    </button>
                </div>
                <div class="usage-stats">
                    <div class="usage-main">
                        <span class="usage-count">${remaining}</span>
                        <span class="usage-label">messages left today</span>
                    </div>
                    <div class="usage-total">${sent} of ${limit} used</div>
                </div>
                <div class="usage-progress-bar">
                    <div class="usage-progress-fill" style="width:${pct}%"></div>
                </div>
            </div>`;
    }
}

// Sidebar footer accordion menu
function initSidebarFooter() {
    const footer = document.querySelector('.sidebar-footer');
    if (!footer) return;

    footer.innerHTML = `
        <style>
        .footer-accordion { padding: 0 8px 8px; }

        .footer-trigger {
            width: 100%;
            display: flex; align-items: center; justify-content: space-between;
            padding: 9px 12px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.2s;
            color: var(--text-secondary);
            font-size: 12px; font-weight: 600;
            letter-spacing: 0.3px;
            margin-bottom: 0;
        }
        .footer-trigger:hover {
            background: rgba(255,255,255,0.06);
            border-color: rgba(255,255,255,0.12);
            color: var(--text-primary);
        }
        .footer-trigger-left { display: flex; align-items: center; gap: 8px; }
        .footer-trigger-left i { color: var(--accent-cyan); font-size: 11px; }
        .footer-chevron {
            font-size: 10px; color: var(--text-muted);
            transition: transform 0.25s;
        }
        .footer-accordion-item.open .footer-chevron { transform: rotate(180deg); }
        .footer-accordion-item.open .footer-trigger {
            border-radius: 10px 10px 0 0;
            border-bottom-color: transparent;
            background: rgba(255,255,255,0.05);
        }

        .footer-panel {
            max-height: 0; overflow: hidden;
            transition: max-height 0.3s ease;
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.07);
            border-top: none;
            border-radius: 0 0 10px 10px;
        }
        .footer-accordion-item.open .footer-panel { max-height: 400px; }
        .footer-panel-inner { padding: 12px 10px 10px; }

        /* Legal links */
        .footer-links-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 3px;
            margin-bottom: 10px;
        }
        .footer-lnk {
            display: flex; align-items: center; gap: 7px;
            padding: 7px 10px;
            border-radius: 7px;
            font-size: 12px; font-weight: 500;
            color: var(--text-secondary);
            text-decoration: none;
            transition: all 0.15s;
            white-space: nowrap;
        }
        .footer-lnk:hover {
            background: rgba(0,242,254,0.07);
            color: var(--accent-cyan);
        }
        .footer-lnk i {
            font-size: 11px;
            width: 14px; text-align: center;
            color: var(--text-muted);
            flex-shrink: 0;
        }
        .footer-lnk:hover i { color: var(--accent-cyan); }

        /* Divider */
        .footer-divider {
            height: 1px;
            background: rgba(255,255,255,0.05);
            margin: 8px 0;
        }

        /* Payment logos */
        .footer-payment {
            display: flex; align-items: center; justify-content: center; gap: 12px;
            padding: 8px 0;
        }
        .footer-payment-logo {
            height: 22px;
            opacity: 0.85;
            filter: brightness(1.1);
            transition: opacity 0.2s, transform 0.2s;
            object-fit: contain;
        }
        .footer-payment-logo:hover { opacity: 1; transform: scale(1.05); }

        /* Branding */
        .footer-brand {
            text-align: center; padding-top: 8px;
        }
        .footer-brand-name {
            font-size: 11px; font-weight: 700;
            color: var(--text-secondary);
            letter-spacing: 0.5px;
        }
        .footer-brand-sub {
            font-size: 9px; color: var(--text-muted);
            margin-top: 2px; letter-spacing: 0.3px;
        }
        </style>

        <div class="footer-accordion">
            <div class="footer-accordion-item" id="footer-acc">
                <button class="footer-trigger" onclick="toggleFooterAccordion(this)">
                    <span class="footer-trigger-left">
                        <i class="fas fa-ellipsis"></i>
                        Daha Fazla / More
                    </span>
                    <i class="fas fa-chevron-down footer-chevron"></i>
                </button>
                <div class="footer-panel">
                    <div class="footer-panel-inner">
                        <div class="footer-links-grid">
                            <a href="/legal/hizmet-sartlari.php" class="footer-lnk" target="_blank">
                                <i class="far fa-file-lines"></i> Hizmet Şartları
                            </a>
                            <a href="/legal/gizlilik-politikasi.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-shield-halved"></i> Gizlilik
                            </a>
                            <a href="/legal/cerez-politikasi.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-cookie-bite"></i> Çerezler
                            </a>
                            <a href="/legal/kvkk-aydinlatma.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-balance-scale"></i> KVKK
                            </a>
                            <a href="/legal/abonelik-kosullari.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-crown"></i> Abonelik
                            </a>
                            <a href="/legal/iade-iptal.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-rotate-left"></i> İade & İptal
                            </a>
                            <a href="/legal/mesafeli-satis.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-file-contract"></i> Mesafeli Satış
                            </a>
                            <a href="/legal/sirket-bilgileri.php" class="footer-lnk" target="_blank">
                                <i class="fas fa-building"></i> Şirket
                            </a>
                            <button onclick="showSupportModal()" class="footer-lnk" style="background:none;border:none;cursor:pointer;font-family:inherit;">
                                <i class="fas fa-headset"></i> Destek
                            </button>
                        </div>

                        <div class="footer-divider"></div>

                        <div class="footer-payment">
                            <img src="/payment/logos/visa.png" class="footer-payment-logo" alt="Visa" onerror="this.style.display='none'">
                            <img src="/payment/logos/mastercard.png" class="footer-payment-logo" alt="Mastercard" onerror="this.style.display='none'">
                            <img src="/payment/logos/iyzico.png" class="footer-payment-logo" alt="iyzico" onerror="this.style.display='none'">
                        </div>

                        <div class="footer-divider"></div>

                        <div class="footer-brand">
                            <div class="footer-brand-name">ONE-BUNE AI</div>
                            <div class="footer-brand-sub">SKYMERGE TECHNOLOGY · © 2026</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;
}

function toggleFooterAccordion(trigger) {
    const item = trigger.closest('.footer-accordion-item');
    const isOpen = item.classList.contains('open');
    
    document.querySelectorAll('.footer-accordion-item').forEach(i => i.classList.remove('open'));
    
    if (!isOpen) {
        item.classList.add('open');
    }
}

// Call on DOM load
document.addEventListener('DOMContentLoaded', () => {
    // ... existing code ...
    setTimeout(initSidebarFooter, 100);
});

function updateUsageIndicator() {
    let indicator = document.getElementById('usage-indicator');
    
    if (!userSubscription || userSubscription.is_premium) { 
        if (indicator) indicator.style.display = 'none'; 
        return; 
    }
    
    const usage = userSubscription.usage_today || {};
    const remaining = usage.messages_remaining;
    
    if (remaining !== undefined && remaining >= 0 && remaining <= 10) {
        if (!indicator) { 
            indicator = document.createElement('div'); 
            indicator.id = 'usage-indicator'; 
            const mc = document.querySelector('.mode-selector-container'); 
            if (mc) mc.parentNode.insertBefore(indicator, mc); 
        }
        indicator.style.display = 'flex';
        indicator.className = `usage-indicator ${remaining <= 3 ? 'critical' : 'warning'}`;
        indicator.innerHTML = `<i class="fas fa-bolt"></i><span>${remaining} mesaj hakkın kaldı</span><button onclick="showUpgradeModal()" class="usage-upgrade-link">Premium'a geç →</button>`;
    } else { 
        if (indicator) indicator.style.display = 'none'; 
    }
}

function updateAttachButton() {
    const btn = document.getElementById('attach-btn');
    if (!btn) return;
    const canUpload = userSubscription?.is_premium || userSubscription?.features?.file_upload;
    if (!canUpload && userSubscription?.plan_id === 'free') {
        btn.classList.add('feature-locked');
        btn.title = 'Dosya yükleme — Premium özellik';
        btn.onclick = (e) => { e.preventDefault(); showUpgradeModal("Dosya Yükleme"); };
    } else {
        btn.classList.remove('feature-locked');
        btn.title = 'Dosya veya Görsel Yükle';
        btn.onclick = () => document.getElementById('file-input').click();
    }
}

let selectedPricePlan = 'monthly';

function showUpgradeModal(lockedFeature) {
    const existing = document.getElementById('upgrade-modal');
    if (existing) existing.remove();
    const featureText = lockedFeature ? `<div class="upgrade-locked-feature"><i class="fas fa-lock"></i> <strong>${lockedFeature}</strong> Premium abonelik gerektiriyor</div>` : '';
    const modal = document.createElement('div');
    modal.id = 'upgrade-modal';
    modal.className = 'upgrade-modal-overlay';
    modal.onclick = (e) => { if (e.target === modal) closeUpgradeModal(); };
    modal.innerHTML = `
        <div class="upgrade-modal-content">
            <button class="upgrade-modal-close" onclick="closeUpgradeModal()"><i class="fas fa-xmark"></i></button>
            ${featureText}
            <div class="upgrade-modal-header">
                <div class="upgrade-orb"><i class="fas fa-crown"></i></div>
                <h2>Premium'a Yükselt</h2>
                <p>Tüm özelliklerin kilidini aç</p>
            </div>
            <div class="upgrade-features">
                <div class="upgrade-feature-item"><i class="fas fa-robot"></i><span>7 AI modu — Kod, Görsel, IT Uzmanı, Öğrenci, Sosyal</span></div>
                <div class="upgrade-feature-item"><i class="fas fa-infinity"></i><span>Sınırsız mesaj hakkı</span></div>
                <div class="upgrade-feature-item"><i class="fas fa-file-arrow-up"></i><span>Dosya yükleme & analiz</span></div>
                <div class="upgrade-feature-item"><i class="fas fa-plane"></i><span>Ucuz bilet & fiyat araştırma</span></div>
                <div class="upgrade-feature-item"><i class="fas fa-image"></i><span>AI görsel oluşturma</span></div>
                <div class="upgrade-feature-item"><i class="fas fa-magnifying-glass"></i><span>Web arama & RAG</span></div>
                <div class="upgrade-feature-item"><i class="fas fa-headset"></i><span>Öncelikli destek</span></div>
            </div>
            <div class="upgrade-single-price">
                <div class="upgrade-price-big">
                    <span class="price-currency">₺</span><span class="price-number">64</span><span class="price-period">/ay</span>
                </div>
                <div class="price-daily-eq">Günlük sadece ₺2.13</div>
            </div>
            <button class="upgrade-cta-btn" id="upgrade-cta" onclick="handleUpgrade()"><i class="fas fa-crown"></i> Premium'a Geç — ₺64/ay</button>
            
            <!-- FATURA BİLGİLERİ -->
            <div style="margin-top:14px;display:flex;flex-direction:column;gap:8px;">
                <div style="font-size:11px;color:var(--text-muted);margin-bottom:2px;">
                    <i class="fas fa-file-invoice"></i> Fatura Bilgileri
                </div>
                <input id="checkout-phone" type="tel" placeholder="Telefon: 05XX XXX XX XX" maxlength="13"
                    style="width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);
                           border-radius:10px;padding:10px 14px;color:#fff;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box;"
                    onfocus="this.style.borderColor='rgba(0,242,254,0.4)'" onblur="this.style.borderColor='rgba(255,255,255,0.1)'"/>
                <input id="checkout-identity" type="text" placeholder="TC Kimlik No (11 hane)" maxlength="11"
                    style="width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);
                           border-radius:10px;padding:10px 14px;color:#fff;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box;"
                    onfocus="this.style.borderColor='rgba(0,242,254,0.4)'" onblur="this.style.borderColor='rgba(255,255,255,0.1)'"/>
                <input id="checkout-address" type="text" placeholder="Açık adres"
                    style="width:100%;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);
                           border-radius:10px;padding:10px 14px;color:#fff;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box;"
                    onfocus="this.style.borderColor='rgba(0,242,254,0.4)'" onblur="this.style.borderColor='rgba(255,255,255,0.1)'"/>
                <div style="display:flex;gap:8px;">
                    <input id="checkout-city" type="text" placeholder="Şehir" 
                        style="flex:1;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);
                               border-radius:10px;padding:10px 14px;color:#fff;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box;"
                        onfocus="this.style.borderColor='rgba(0,242,254,0.4)'" onblur="this.style.borderColor='rgba(255,255,255,0.1)'"/>
                    <input id="checkout-zip" type="text" placeholder="Posta Kodu" maxlength="5"
                        style="width:120px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);
                               border-radius:10px;padding:10px 14px;color:#fff;font-size:13px;font-family:inherit;outline:none;box-sizing:border-box;"
                        onfocus="this.style.borderColor='rgba(0,242,254,0.4)'" onblur="this.style.borderColor='rgba(255,255,255,0.1)'"/>
                </div>
            </div>
            
            <!-- ONAY KUTULARI -->
            <div style="display:flex;flex-direction:column;gap:8px;margin-top:14px;padding:12px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:10px;">
                <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-size:12px;color:var(--text-secondary);line-height:1.5;">
                    <input type="checkbox" id="chk-hizmet" style="margin-top:2px;accent-color:#00f2fe;flex-shrink:0;width:15px;height:15px;cursor:pointer;">
                    <span><a href="/legal/hizmet-sartlari.php" target="_blank" style="color:var(--accent-cyan);text-decoration:none;" onclick="event.stopPropagation()">Hizmet Şartları</a>'nı okudum ve kabul ediyorum. <span style="color:#ff4b4b;">*</span></span>
                </label>
                <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-size:12px;color:var(--text-secondary);line-height:1.5;">
                    <input type="checkbox" id="chk-gizlilik" style="margin-top:2px;accent-color:#00f2fe;flex-shrink:0;width:15px;height:15px;cursor:pointer;">
                    <span><a href="/legal/gizlilik-politikasi.php" target="_blank" style="color:var(--accent-cyan);text-decoration:none;" onclick="event.stopPropagation()">Gizlilik Politikası</a>'nı ve <a href="/legal/kvkk-aydinlatma.php" target="_blank" style="color:var(--accent-cyan);text-decoration:none;" onclick="event.stopPropagation()">KVKK Aydınlatma Metni</a>'ni okudum ve onaylıyorum. <span style="color:#ff4b4b;">*</span></span>
                </label>
                <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-size:12px;color:var(--text-secondary);line-height:1.5;">
                    <input type="checkbox" id="chk-abonelik" style="margin-top:2px;accent-color:#00f2fe;flex-shrink:0;width:15px;height:15px;cursor:pointer;">
                    <span><a href="/legal/abonelik-kosullari.php" target="_blank" style="color:var(--accent-cyan);text-decoration:none;" onclick="event.stopPropagation()">Abonelik Koşulları</a>'nı ve <a href="/legal/mesafeli-satis.php" target="_blank" style="color:var(--accent-cyan);text-decoration:none;" onclick="event.stopPropagation()">Mesafeli Satış Sözleşmesi</a>'ni okudum ve kabul ediyorum. <span style="color:#ff4b4b;">*</span></span>
                </label>
                <label style="display:flex;align-items:flex-start;gap:10px;cursor:pointer;font-size:12px;color:var(--text-secondary);line-height:1.5;">
                    <input type="checkbox" id="chk-iletisim" style="margin-top:2px;accent-color:#00f2fe;flex-shrink:0;width:15px;height:15px;cursor:pointer;">
                    <span>Kampanya, indirim ve yenilikler hakkında e-posta / SMS ile bilgilendirme almak istiyorum. <span style="color:var(--text-muted);font-size:11px;">(isteğe bağlı)</span></span>
                </label>
            </div>

            <!-- ÖDEME LOGOLARI -->
            <div style="display: flex; align-items: center; justify-content: center; gap: 14px; margin: 12px 0 8px 0; padding: 10px; background: rgba(255,255,255,0.03); border-radius: 8px;">
                <img src="/payment/logos/visa.png" style="height: 22px; opacity: 0.75;" alt="Visa" onerror="this.style.display='none'">
                <img src="/payment/logos/mastercard.png" style="height: 22px; opacity: 0.75;" alt="Mastercard" onerror="this.style.display='none'">
                <img src="/payment/logos/iyzico.png" style="height: 18px; opacity: 0.75;" alt="iyzico" onerror="this.style.display='none'">
            </div>
            
            <div class="upgrade-footer">İstediğin zaman iptal edebilirsin · Güvenli ödeme</div>
        </div>`;
    document.body.appendChild(modal);
    requestAnimationFrame(() => modal.classList.add('show'));
}

function selectPricePlan(plan) {
    selectedPricePlan = plan;
    document.getElementById('price-monthly').classList.toggle('active', plan === 'monthly');
    document.getElementById('price-yearly').classList.toggle('active', plan === 'yearly');
}

function closeUpgradeModal() {
    const modal = document.getElementById('upgrade-modal');
    if (modal) { modal.classList.remove('show'); setTimeout(() => modal.remove(), 300); }
}

// handleUpgrade — aşağıda tanımlı

function showSubscriptionPanel() {
    const existing = document.getElementById('subscription-panel');
    if (existing) existing.remove();
    if (!userSubscription) return;

    const periodEnd = userSubscription.current_period_end ? new Date(userSubscription.current_period_end).toLocaleDateString('tr-TR', { day: 'numeric', month: 'long', year: 'numeric' }) : '—';
    const cancelText = userSubscription.cancel_at_period_end
        ? `<div class="sub-panel-cancel-notice"><i class="fas fa-info-circle"></i> Abonelik ${periodEnd} tarihinde sona erecek</div>`
        : `<button class="sub-panel-cancel-btn" onclick="handleCancelSubscription()">Aboneliği İptal Et</button>`;

    const panel = document.createElement('div');
    panel.id = 'subscription-panel';
    panel.className = 'upgrade-modal-overlay';
    panel.onclick = (e) => { if (e.target === panel) panel.remove(); };
    panel.innerHTML = `
        <div class="upgrade-modal-content sub-panel">
            <button class="upgrade-modal-close" onclick="this.closest('.upgrade-modal-overlay').remove()"><i class="fas fa-xmark"></i></button>
            <div class="sub-panel-header"><div class="upgrade-orb"><i class="fas fa-crown"></i></div><h2>Premium Abonelik</h2></div>
            <div class="sub-panel-info">
                <div class="sub-panel-row"><span>Plan</span><span class="sub-panel-val">Premium</span></div>
                <div class="sub-panel-row"><span>Dönem</span><span class="sub-panel-val">${userSubscription.billing_period === 'yearly' ? 'Yıllık' : 'Aylık'}</span></div>
                <div class="sub-panel-row"><span>Sonraki yenileme</span><span class="sub-panel-val">${periodEnd}</span></div>
                <div class="sub-panel-row"><span>Durum</span><span class="sub-panel-val" style="color:${userSubscription.cancel_at_period_end ? '#ffb400' : '#22c55e'};">${userSubscription.cancel_at_period_end ? 'İptal Edildi ⚠' : 'Aktif ✓'}</span></div>
            </div>
            ${cancelText}
        </div>`;
    document.body.appendChild(panel);
    requestAnimationFrame(() => panel.classList.add('show'));
}

function showCheckoutModal(formContent) {
    document.getElementById('iyzico-modal')?.remove();

    const modal = document.createElement('div');
    modal.id = 'iyzico-modal';
    modal.style.cssText = [
        'position:fixed;inset:0;',
        'background:rgba(0,0,0,0.88);',
        'backdrop-filter:blur(10px);',
        'z-index:99999;',
        'display:flex;align-items:center;justify-content:center;',
        'padding:16px;box-sizing:border-box;',
    ].join('');
    modal.onclick = (e) => { if (e.target === modal) modal.remove(); };

    const box = document.createElement('div');
    box.style.cssText = [
        'background:#fff;border-radius:20px;',
        'width:100%;max-width:560px;',
        'max-height:92vh;overflow-y:auto;',
        'position:relative;',
        'box-shadow:0 24px 80px rgba(0,0,0,0.6);',
        '-webkit-overflow-scrolling:touch;',
    ].join('');

    const closeBtn = document.createElement('button');
    closeBtn.innerHTML = '✕';
    closeBtn.style.cssText = [
        'position:sticky;top:0;float:right;',
        'margin:10px 10px 0 0;',
        'background:rgba(0,0,0,0.07);border:none;',
        'width:32px;height:32px;border-radius:50%;',
        'cursor:pointer;font-size:14px;z-index:10;',
    ].join('');
    closeBtn.onclick = () => modal.remove();

    const formDiv = document.createElement('div');
    formDiv.style.cssText = 'padding:0;min-height:400px;';
    formDiv.innerHTML = formContent;

    box.appendChild(closeBtn);
    box.appendChild(formDiv);
    modal.appendChild(box);
    document.body.appendChild(modal);

    formDiv.querySelectorAll('script').forEach(s => {
        const ns = document.createElement('script');
        if (s.src) ns.src = s.src;
        else ns.textContent = s.textContent;
        document.body.appendChild(ns);
    });

    // iyzico iframe yüklenince tam genişliğe çek
    const obs = new MutationObserver(() => {
        const iframe = box.querySelector('iframe');
        if (iframe) {
            iframe.style.width = '100%';
            iframe.style.minHeight = '520px';
            iframe.style.border = 'none';
            iframe.style.display = 'block';
            obs.disconnect();
        }
    });
    obs.observe(box, { childList: true, subtree: true });
}

async function handleCancelSubscription() {
    if (!confirm("Aboneliğinizi iptal etmek istediğinize emin misiniz?\nDönem sonuna kadar premium özellikler aktif kalır.")) return;
    const token = localStorage.getItem('token');
    try {
        const res = await fetch(PROXY_URL, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: "subscription_cancel", immediate: false, token }) });
        const data = await res.json();
        document.getElementById('subscription-panel')?.remove();
        await loadSubscriptionStatus();
        showToast(data.message || "Abonelik dönem sonunda iptal edilecek", "info");
        // Güncel bilgiyle paneli yeniden aç
        setTimeout(() => showSubscriptionPanel(), 300);
    } catch (e) { 
        showToast("Hata oluştu", "error"); 
    }
}
async function handleUpgrade() {
    const token = localStorage.getItem('token');
    if (!token) {
        showToast("Lütfen giriş yapın", "warn");
        return;
    }
 
    // Zaten premium mi kontrol et
    if (userSubscription && userSubscription.is_premium) {
        showToast("Zaten Premium üyesiniz!", "info");
        return;
    }

    // Sözleşme onayları kontrolü
    const chkHizmet   = document.getElementById('chk-hizmet');
    const chkGizlilik = document.getElementById('chk-gizlilik');
    const chkAbonelik = document.getElementById('chk-abonelik');

    if (!chkHizmet?.checked) {
        showToast("Hizmet Şartları'nı kabul etmeniz gerekiyor", "warn");
        chkHizmet?.closest('label')?.querySelector('input')?.focus();
        return;
    }
    if (!chkGizlilik?.checked) {
        showToast("Gizlilik Politikası'nı onaylamanız gerekiyor", "warn");
        return;
    }
    if (!chkAbonelik?.checked) {
        showToast("Abonelik Koşulları'nı kabul etmeniz gerekiyor", "warn");
        return;
    }

    // Fatura bilgileri kontrolü
    const phoneInput    = document.getElementById('checkout-phone');
    const identityInput = document.getElementById('checkout-identity');
    const addressInput  = document.getElementById('checkout-address');
    const cityInput     = document.getElementById('checkout-city');
    const zipInput      = document.getElementById('checkout-zip');

    const phone    = phoneInput?.value.trim()    || '';
    const identity = identityInput?.value.trim() || '';
    const address  = addressInput?.value.trim()  || '';
    const city     = cityInput?.value.trim()     || '';
    const zip      = zipInput?.value.trim()      || '';

    const highlight = (el) => { if(el) { el.style.borderColor='rgba(255,75,75,0.5)'; el.focus(); } };

    if (!phone || phone.replace(/\D/g,'').length < 10) {
        showToast("Geçerli bir telefon numarası girin", "warn"); highlight(phoneInput); return;
    }
    if (!identity || identity.replace(/\D/g,'').length !== 11) {
        showToast("TC Kimlik No 11 hane olmalı", "warn"); highlight(identityInput); return;
    }
    if (!address) {
        showToast("Adres girin", "warn"); highlight(addressInput); return;
    }
    if (!city) {
        showToast("Şehir girin", "warn"); highlight(cityInput); return;
    }
 
    const btn = document.getElementById('upgrade-cta');
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Ödeme sayfası yükleniyor...';
    }
 
    try {
        // Payment service'e checkout isteği gönder
        const res = await fetch(PROXY_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action:         'payment_checkout',
                token:          token,
                gsmNumber:      phone,
                identityNumber: identity,
                address:        address,
                city:           city,
                zipCode:        zip || '34000',
            })
        });
        
        const data = await res.json();
        
        if (!res.ok) {
            throw new Error(data.detail || data.message || 'Checkout başlatılamadı');
        }
 
        console.log('[PAYMENT] Checkout response:', data);
 
        // İyzico checkout form HTML'i geldi mi?
        if (data.checkoutFormContent) {
            // Modal'da göster
            showCheckoutModal(data.checkoutFormContent);
        } else if (data.paymentPageUrl) {
            // İyzico sayfasına yönlendir
            window.location.href = data.paymentPageUrl;
        } else {
            throw new Error('Checkout formu alınamadı');
        }
        
    } catch (e) {
        console.error('[PAYMENT] Upgrade error:', e);
        showToast("Ödeme başlatılamadı: " + e.message, "error");
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-crown"></i> Premium\'a Geç — ₺64/ay';
        }
    }
}
 // ═══════════════════════════════════════════════════════════════
// PAYMENT HANDLER - PROXY KULLANAN VERSİYON
// ═══════════════════════════════════════════════════════════════
// app.js'deki handlePremiumCheckout fonksiyonunu BU ile değiştir
// (Satır ~1449 civarı)
// ═══════════════════════════════════════════════════════════════

async function handlePremiumCheckout() {
    const token = localStorage.getItem('token');
    if (!token) {
        showToast('Lütfen giriş yapın', 'error');
        return;
    }

    // Check if already premium
    const status = await checkSubscriptionStatus();
    if (status && status.subscription_active) {
        showToast('Zaten premium üyesiniz!', 'info');
        return;
    }

    try {
        const checkoutBtn = document.getElementById('premium-checkout-btn');
        if (checkoutBtn) {
            checkoutBtn.disabled = true;
            checkoutBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Hazırlanıyor...';
        }

        console.log('[PAYMENT] Starting checkout via PROXY...');

        // ✅ PROXY.PHP KULLAN
        const response = await fetch(PROXY_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                action: 'payment_checkout',
                token: token
            })
        });

        const data = await response.json();

        if (!response.ok) {
            throw new Error(data.detail || data.message || 'Checkout başlatılamadı');
        }

        console.log('[PAYMENT] Checkout success:', data);

        if (data.checkoutFormContent) {
            showCheckoutModal(data.checkoutFormContent);
        } else {
            throw new Error('Checkout formu alınamadı');
        }

    } catch (error) {
        console.error('[PAYMENT] Error:', error);
        showToast('Ödeme başlatılamadı: ' + error.message, 'error');
        
        const checkoutBtn = document.getElementById('premium-checkout-btn');
        if (checkoutBtn) {
            checkoutBtn.disabled = false;
            checkoutBtn.innerHTML = '<i class="fas fa-crown"></i> Premium\'a Geç';
        }
    }
}

// ═══════════════════════════════════════════════════════════════
// checkSubscriptionStatus - PROXY KULLANAN VERSİYON
// ═══════════════════════════════════════════════════════════════

async function checkSubscriptionStatus() {
    const token = localStorage.getItem('token');
    if (!token) return null;

    try {
        // ✅ PROXY.PHP KULLAN
        const response = await fetch(PROXY_URL, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                action: 'subscription_status',
                token: token
            })
        });

        if (!response.ok) return null;
        const data = await response.json();
        return data;

    } catch (error) {
        console.error('[SUBSCRIPTION] Check failed:', error);
        return null;
    }
}
// ═══════════════════════════════════════════════════════════════
// AYARLAR / PROFİL MODALI
// ═══════════════════════════════════════════════════════════════

async function showSettingsModal() {
    document.getElementById('settings-modal')?.remove();
    const token = localStorage.getItem('token');
    if (!token) return;

    let profile = {}, usage = {}, history = [];
    try {
        const [pRes, uRes, hRes] = await Promise.all([
            fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'get_profile', token }) }),
            fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'get_usage', token }) }),
            fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'get_payment_history', token }) }),
        ]);
        profile = (await pRes.json()) || {};
        usage   = (await uRes.json()) || {};
        history = (await hRes.json()) || [];
    } catch(e) {}

    const user       = profile.user || {};
    const sub        = userSubscription || {};
    const isPremium       = sub.is_premium || usage.is_premium || false;
    const isCancelled     = sub.cancel_at_period_end || false;
    const periodEnd       = sub.current_period_end
        ? new Date(sub.current_period_end).toLocaleDateString('tr-TR', {day:'numeric', month:'long', year:'numeric'})
        : (sub.period_end || '');
    const todayUsed  = usage.today_messages || 0;
    const dailyLimit = usage.daily_limit || 50;
    const usagePct   = Math.min(100, Math.round((todayUsed / dailyLimit) * 100));
    const avatarName  = user.name || localStorage.getItem('user_name') || 'U';
    const avatarStyle = user.avatar_style || localStorage.getItem('avatar_style') || 'avataaars';
    const avatarSeed  = encodeURIComponent(avatarName);

    const DICEBEAR_STYLES = [
        { id: 'av-01', label: 'Asistan' },
        { id: 'av-02', label: 'Yazılımcı' },
        { id: 'av-03', label: 'Tasarımcı' },
        { id: 'av-04', label: 'Mühendis' },
        { id: 'av-05', label: 'Robotik' },
        { id: 'av-06', label: 'Mor' },
        { id: 'av-07', label: 'Neon' },
        { id: 'av-08', label: 'Turuncu' },
        { id: 'av-09', label: 'Siborg' },
        { id: 'av-10', label: 'Lavanta' },
        { id: 'av-11', label: 'Lacivert' },
        { id: 'av-12', label: 'Yeşil' },
        { id: 'av-13', label: 'Mavi-Mor' },
        { id: 'av-14', label: 'Altın' },
        { id: 'av-15', label: 'Zümrüt' },
        { id: 'av-16', label: 'Pembe' },
        { id: 'av-17', label: 'Çelik' },
        { id: 'av-18', label: 'Kahve' },
        { id: 'av-19', label: 'Gök' },
        { id: 'av-20', label: 'Violet' },
        { id: 'av-21', label: 'Haki' },
        { id: 'av-22', label: 'Derin Mor' },
        { id: 'av-23', label: 'Nane' },
        { id: 'av-24', label: 'Kırmızı' },
        { id: 'av-25', label: 'Lacivert2' },
        { id: 'av-26', label: 'Bal' },
        { id: 'av-27', label: 'Deniz' },
        { id: 'av-28', label: 'Gümüş' },
        { id: 'av-29', label: 'Taba' },
        { id: 'av-30', label: 'Körfez' },
        { id: 'av-31', label: 'Orkide' },
        { id: 'av-32', label: 'Fıstık' },
        { id: 'av-33', label: 'Ametist' },
        { id: 'av-34', label: 'Bronz' },
        { id: 'av-35', label: 'Safir' },
        { id: 'av-36', label: 'Kızıl' },
        { id: 'av-37', label: 'Gece' },
        { id: 'av-38', label: 'Zeytin' },
        { id: 'av-39', label: 'Erik' },
        { id: 'av-40', label: 'Denim' },
        { id: 'av-41', label: 'Antrasit' },
        { id: 'av-42', label: 'Ruby' },
        { id: 'av-43', label: 'Orman' },
        { id: 'av-44', label: 'Altın2' },
        { id: 'av-45', label: 'İndigo' },
        { id: 'av-46', label: 'Gül' },
        { id: 'av-47', label: 'Arktik' },
        { id: 'av-48', label: 'Üzüm' },
        { id: 'av-49', label: 'Ceviz' },
        { id: 'av-50', label: 'Yeşim' },
    ];
    const _avatarUrl = (id) => `/avatars/${id}.svg`;

    const modal = document.createElement('div');
    modal.id = 'settings-modal';
    modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.75);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;padding:20px;animation:smFade .2s ease;';

    modal.innerHTML = `
<style>
@keyframes smFade{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:scale(1)}}
.sm-box{background:#111114;border:1px solid rgba(255,255,255,.07);border-radius:20px;width:100%;max-width:520px;max-height:88vh;overflow-y:auto;position:relative;}
.sm-header{display:flex;align-items:center;justify-content:space-between;padding:20px 22px 0;position:sticky;top:0;background:#111114;border-radius:20px 20px 0 0;z-index:2;}
.sm-title{font-size:16px;font-weight:700;color:#fff;}
.sm-close{width:30px;height:30px;border-radius:8px;background:rgba(255,255,255,.05);border:none;color:#8e8ea0;cursor:pointer;font-size:13px;display:flex;align-items:center;justify-content:center;transition:all .2s;}
.sm-close:hover{background:rgba(255,255,255,.1);color:#fff;}
.sm-tabs{display:flex;gap:2px;padding:12px 22px 0;border-bottom:1px solid rgba(255,255,255,.06);overflow-x:auto;scrollbar-width:none;}
.sm-tabs::-webkit-scrollbar{display:none;}
.sm-tab{padding:7px 13px;border-radius:8px 8px 0 0;font-size:12px;font-weight:600;cursor:pointer;color:#55556a;white-space:nowrap;transition:all .15s;border:none;background:none;border-bottom:2px solid transparent;margin-bottom:-1px;}
.sm-tab:hover{color:#8e8ea0;}
.sm-tab.active{color:var(--accent-cyan);border-bottom-color:var(--accent-cyan);}
.sm-body{padding:18px 22px 22px;}
.sm-section{display:none;}
.sm-section.active{display:block;}
.sm-field{margin-bottom:14px;}
.sm-lbl{font-size:10px;font-weight:700;color:#55556a;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px;display:block;}
.sm-inp{width:100%;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 13px;color:#fff;font-size:13px;font-family:inherit;outline:none;transition:border-color .2s;box-sizing:border-box;}
.sm-inp:focus{border-color:rgba(0,242,254,.35);}
.sm-btn{padding:9px 18px;border-radius:9px;border:none;font-size:12px;font-weight:600;cursor:pointer;transition:all .2s;}
.sm-primary{background:linear-gradient(135deg,#00f2fe,#bc4efd);color:#0a0a0c;}
.sm-primary:hover{opacity:.9;transform:translateY(-1px);}
.sm-ghost{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);color:#8e8ea0;}
.sm-ghost:hover{background:rgba(255,255,255,.08);color:#fff;}
.sm-danger{background:rgba(255,75,75,.08);border:1px solid rgba(255,75,75,.18);color:#ff4b4b;}
.sm-danger:hover{background:rgba(255,75,75,.15);}
.sm-div{height:1px;background:rgba(255,255,255,.05);margin:16px 0;}
.sm-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;}
.sm-row-lbl{font-size:13px;color:#eeeef0;font-weight:500;}
.sm-row-sub{font-size:11px;color:#55556a;margin-top:2px;}
.sm-toggle{position:relative;width:36px;height:20px;cursor:pointer;flex-shrink:0;}
.sm-toggle input{opacity:0;width:0;height:0;}
.sm-trk{position:absolute;inset:0;background:rgba(255,255,255,.08);border-radius:20px;transition:.2s;border:1px solid rgba(255,255,255,.1);}
.sm-toggle input:checked+.sm-trk{background:rgba(0,242,254,.2);border-color:rgba(0,242,254,.35);}
.sm-trk::after{content:'';position:absolute;width:14px;height:14px;border-radius:50%;background:#55556a;top:2px;left:2px;transition:.2s;}
.sm-toggle input:checked+.sm-trk::after{background:var(--accent-cyan);transform:translateX(16px);}
.sm-av{width:60px;height:60px;border-radius:16px;background:linear-gradient(135deg,#00f2fe,#bc4efd);display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:800;color:#0a0a0c;margin:0 auto 16px;}
.sm-plan{border-radius:12px;padding:13px 15px;border:1px solid;margin-bottom:14px;}
.sm-plan.premium{background:linear-gradient(135deg,rgba(188,78,253,.07),rgba(0,242,254,.04));border-color:rgba(188,78,253,.2);}
.sm-plan.free{background:rgba(255,255,255,.02);border-color:rgba(255,255,255,.07);}
.sm-stat-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:7px;margin-bottom:14px;}
.sm-stat{background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:11px;text-align:center;}
.sm-stat-v{font-size:20px;font-weight:800;color:#fff;}
.sm-stat-l{font-size:10px;color:#55556a;margin-top:2px;}
.sm-ubar{height:5px;border-radius:10px;background:rgba(255,255,255,.06);margin:7px 0 4px;overflow:hidden;}
.sm-ufill{height:100%;border-radius:10px;background:linear-gradient(90deg,#00f2fe,#bc4efd);}
.sm-pay{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;border-radius:8px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.05);margin-bottom:5px;}
</style>
<div class="sm-box">
    <div class="sm-header">
        <span class="sm-title">Ayarlar</span>
        <button class="sm-close" onclick="document.getElementById('settings-modal').remove()"><i class="fas fa-xmark"></i></button>
    </div>
    <div class="sm-tabs">
        <button class="sm-tab active" onclick="smTab(this,'smp')"><i class="fas fa-user" style="margin-right:5px;font-size:10px;"></i>Profil</button>
        <button class="sm-tab" onclick="smTab(this,'sma')"><i class="fas fa-crown" style="margin-right:5px;font-size:10px;"></i>Abonelik</button>
        <button class="sm-tab" onclick="smTab(this,'smn')"><i class="fas fa-bell" style="margin-right:5px;font-size:10px;"></i>Bildirimler</button>
        <button class="sm-tab" onclick="smTab(this,'smg')"><i class="fas fa-shield-halved" style="margin-right:5px;font-size:10px;"></i>Güvenlik</button>
    </div>
    <div class="sm-body">

        <!-- PROFİL -->
        <div class="sm-section active" id="smp">
            <!-- AVATAR SEÇİCİ -->
            <div style="text-align:center;margin-bottom:20px;">
                <img id="sm-avatar-preview"
                    src="/avatars/${avatarStyle}.svg"
                    style="width:80px;height:80px;border-radius:20px;
                           border:2px solid rgba(0,242,254,0.3);
                           background:#18181d;padding:4px;
                           transition:all 0.3s;"
                    alt="Avatar">
                <div style="font-size:11px;color:var(--text-muted);margin-top:6px;">Avatar stilini seç</div>
                <div style="display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin-top:10px;">
                    ${DICEBEAR_STYLES.map(s => `
                        <button onclick="smSelectAvatar('${s.id}', '${avatarSeed}')"
                            data-avatar-style="${s.id}"
                            style="display:flex;flex-direction:column;align-items:center;gap:4px;
                                   padding:6px 8px;border-radius:10px;cursor:pointer;
                                   background:${s.id === avatarStyle ? 'rgba(0,242,254,0.12)' : 'rgba(255,255,255,0.04)'};
                                   border:1px solid ${s.id === avatarStyle ? 'rgba(0,242,254,0.4)' : 'rgba(255,255,255,0.08)'};
                                   transition:all 0.15s;min-width:64px;">
                            <img src="/avatars/${s.id}.svg"
                                 style="width:36px;height:36px;border-radius:8px;background:#18181d;padding:3px;"
                                 alt="${s.label}">
                            <span style="font-size:10px;color:var(--text-secondary);">${s.label}</span>
                        </button>
                    `).join('')}
                </div>
            </div>
            <div class="sm-field"><label class="sm-lbl">Ad Soyad</label>
                <input class="sm-inp" id="sm-name" value="${user.name || localStorage.getItem('user_name') || ''}" placeholder="Adınızı girin"></div>
            <div class="sm-field"><label class="sm-lbl">E-posta</label>
                <input class="sm-inp" id="sm-email" value="${user.email || ''}" placeholder="E-posta adresiniz"
                    readonly
                    style="opacity:0.5;cursor:not-allowed;user-select:none;"
                    title="E-posta adresi değiştirilemez"
                    onfocus="this.blur()"></div>
            <div style="display:flex;gap:8px;">
                <button class="sm-btn sm-primary" onclick="smSaveProfile()">Kaydet</button>
            </div>
            <div id="sm-pmsg" style="font-size:12px;margin-top:8px;min-height:16px;"></div>
        </div>

        <!-- ABONELİK -->
        <div class="sm-section" id="sma">
            <div class="sm-plan ${isPremium?'premium':'free'}">
                <div style="display:flex;align-items:center;justify-content:space-between;">
                    <div>
                        <div style="font-size:14px;font-weight:700;color:#fff;">${isPremium?'👑 Premium':'🆓 Ücretsiz Plan'}</div>
                        <div style="font-size:11px;margin-top:2px;color:${isCancelled?'#ffb400':'#8e8ea0'};">
                            ${isPremium
                                ? (isCancelled
                                    ? `⚠ İptal edildi · ${periodEnd} tarihinde sona erecek`
                                    : (periodEnd ? `Yenileme: ${periodEnd}` : 'Aktif abonelik'))
                                : 'Günde 50 mesaj hakkı'}
                        </div>
                    </div>
                    ${!isPremium?`<button class="sm-btn sm-primary" style="font-size:11px;padding:7px 13px;" onclick="document.getElementById('settings-modal').remove();setTimeout(()=>document.querySelector('.plan-card')?.click(),150)">Yükselt →</button>`:''}
                </div>
            </div>
            <div class="sm-stat-grid">
                <div class="sm-stat"><div class="sm-stat-v">${usage.total_messages??'0'}</div><div class="sm-stat-l">Toplam Mesaj</div></div>
                <div class="sm-stat"><div class="sm-stat-v">${usage.total_conversations??'0'}</div><div class="sm-stat-l">Sohbet</div></div>
                <div class="sm-stat"><div class="sm-stat-v">${usage.member_since ? usage.member_since.split('.').pop() : '—'}</div><div class="sm-stat-l">Üye Yılı</div></div>
            </div>
            <label class="sm-lbl">Bugünkü Kullanım</label>
            <div style="display:flex;justify-content:space-between;font-size:11px;color:#8e8ea0;">
                <span>${todayUsed} mesaj gönderildi</span>
                <span>${isPremium?'Sınırsız':dailyLimit+' limit'}</span>
            </div>
            <div class="sm-ubar"><div class="sm-ufill" style="width:${isPremium?Math.min(usagePct,30):usagePct}%"></div></div>
            <div class="sm-div"></div>
            <label class="sm-lbl">Fatura Geçmişi</label>
            ${history.length===0
                ?`<div style="font-size:13px;color:#55556a;padding:10px 0;">Henüz ödeme geçmişi yok.</div>`
                :history.slice(0,6).map(h=>`<div class="sm-pay"><div><div style="font-size:13px;color:#eeeef0;font-weight:500;">Premium Abonelik</div><div style="font-size:11px;color:#55556a;margin-top:2px;">${h.paid_at||h.date}</div></div><div style="font-size:14px;font-weight:700;color:#00e676;">₺${parseFloat(h.amount||0).toFixed(2)}</div></div>`).join('')
            }
            ${isPremium?`<div class="sm-div"></div>
            <button class="sm-btn sm-ghost" style="width:100%;display:flex;justify-content:center;" onclick="smCancelSub()">
                Aboneliği İptal Et
            </button>`:''}
        </div>

        <!-- BİLDİRİMLER -->
        <div class="sm-section" id="smn">
            <div class="sm-row">
                <div><div class="sm-row-lbl">Platform Güncellemeleri</div><div class="sm-row-sub">Yeni özellik ve duyurular</div></div>
                <label class="sm-toggle"><input type="checkbox" id="n-upd" checked onchange="smSaveNotifs()"><span class="sm-trk"></span></label>
            </div>
            <div class="sm-div"></div>
            <div class="sm-row">
                <div><div class="sm-row-lbl">Güvenlik Bildirimleri</div><div class="sm-row-sub">Giriş ve hesap aktivitesi</div></div>
                <label class="sm-toggle"><input type="checkbox" id="n-sec" checked onchange="smSaveNotifs()"><span class="sm-trk"></span></label>
            </div>
            <div class="sm-div"></div>
            <div class="sm-row">
                <div><div class="sm-row-lbl">Fatura E-postaları</div><div class="sm-row-sub">Ödeme ve abonelik bildirimleri</div></div>
                <label class="sm-toggle"><input type="checkbox" id="n-bil" checked onchange="smSaveNotifs()"><span class="sm-trk"></span></label>
            </div>
            <div id="sm-nmsg" style="font-size:12px;color:#00e676;min-height:16px;margin-top:4px;"></div>
        </div>

        <!-- GÜVENLİK -->
        <div class="sm-section" id="smg">
            <div style="background:rgba(0,242,254,.04);border:1px solid rgba(0,242,254,.1);border-radius:10px;padding:13px;margin-bottom:18px;">
                <div style="font-size:12px;color:#00f2fe;font-weight:600;margin-bottom:4px;"><i class="fas fa-shield-halved"></i> OTP ile güvenli giriş aktif</div>
                <div style="font-size:12px;color:#8e8ea0;line-height:1.6;">Hesabınız e-posta doğrulaması ile korunuyor. Şifre gerekmez — bu en güvenli yöntemdir.</div>
            </div>
            <label class="sm-lbl">Üyelik Tarihi</label>
            <div style="font-size:14px;color:#eeeef0;margin-bottom:18px;">${usage.member_since||'—'}</div>
            <div class="sm-div"></div>
            <label class="sm-lbl" style="color:#ff4b4b;">Tehlikeli Bölge</label>
            <div style="background:rgba(255,75,75,.04);border:1px solid rgba(255,75,75,.12);border-radius:10px;padding:14px;">
                <div style="font-size:13px;color:#eeeef0;font-weight:600;margin-bottom:4px;">Hesabı Kalıcı Olarak Sil</div>
                <div style="font-size:12px;color:#8e8ea0;margin-bottom:12px;line-height:1.5;">Tüm verileriniz ve sohbet geçmişiniz silinir. Bu işlem geri alınamaz.</div>
                <button class="sm-btn sm-danger" onclick="smDeleteAccount()"><i class="fas fa-trash-can"></i> Hesabımı Sil</button>
            </div>
        </div>

    </div>
</div>`;

    document.body.appendChild(modal);
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

function smSelectAvatar(style, seed) {
    // Önizleme güncelle
    const preview = document.getElementById('sm-avatar-preview');
    if (preview) preview.src = `/avatars/${style}.svg`;

    // Buton highlight'larını güncelle
    document.querySelectorAll('[data-avatar-style]').forEach(btn => {
        const isActive = btn.dataset.avatarStyle === style;
        btn.style.background = isActive ? 'rgba(0,242,254,0.12)' : 'rgba(255,255,255,0.04)';
        btn.style.borderColor = isActive ? 'rgba(0,242,254,0.4)' : 'rgba(255,255,255,0.08)';
    });

    // Geçici olarak sakla (kaydet butonuna basılınca DB'ye gidecek)
    localStorage.setItem('avatar_style_pending', style);
}

// ── DESTEK MODAL ──────────────────────────────────────────────
let _supportType = 'general';

function showSupportModal() {
    const modal = document.getElementById('support-modal');
    if (modal) {
        modal.style.display = 'flex';
        // reset
        _supportType = 'general';
        document.querySelectorAll('.support-type-btn').forEach(b => {
            b.style.background = 'rgba(255,255,255,0.04)';
            b.style.borderColor = 'rgba(255,255,255,0.08)';
        });
        const msgEl = document.getElementById('support-message');
        if (msgEl) msgEl.value = '';
        const res = document.getElementById('support-result');
        if (res) res.style.display = 'none';
    }
}

function setSupportType(type) {
    _supportType = type;
    document.querySelectorAll('.support-type-btn').forEach(b => {
        const isActive = b.dataset.stype === type;
        b.style.background = isActive ? 'rgba(0,242,254,0.08)' : 'rgba(255,255,255,0.04)';
        b.style.borderColor = isActive ? 'rgba(0,242,254,0.3)' : 'rgba(255,255,255,0.08)';
    });
}

async function submitSupport() {
    const msg = document.getElementById('support-message')?.value?.trim();
    if (!msg || msg.length < 5) {
        showToast('Lütfen mesajınızı yazın', 'warn'); return;
    }
    const token = localStorage.getItem('token');
    try {
        await fetch(PROXY_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                action: 'feedback',
                token,
                conversation_id: currentConversationId || '',
                user_query: `[DESTEK - ${_supportType.toUpperCase()}]`,
                assistant_response: msg,
                rating: _supportType === 'bug' ? -1 : 1,
                comment: `[${_supportType}] ${msg}`,
            })
        });
        const res = document.getElementById('support-result');
        if (res) { res.style.display = 'block'; }
        showToast('Mesajınız alındı, teşekkürler!', 'success');
        setTimeout(() => {
            document.getElementById('support-modal').style.display = 'none';
        }, 2000);
    } catch(e) {
        showToast('Gönderilemedi, tekrar deneyin', 'error');
    }
}
// ─────────────────────────────────────────────────────────────

// ── SIDEBAR TAB ──────────────────────────────────────────────
function switchSidebarTab(tab) {
    const navChats = document.getElementById('nav-chats');
    const navApps  = document.getElementById('nav-apps');

    if (tab === 'apps') {
        // Sidebar'ı kapat, tam ekran apps sayfasını aç
        openAppsPage();
        // Nav highlight — apps aktif
        navApps?.classList.add('active');
        navChats?.classList.remove('active');
        // Kısa süre sonra nav'ı sıfırla (sayfa kapanınca tekrar sohbetler aktif)
        setTimeout(() => {
            navApps?.classList.remove('active');
            navChats?.classList.add('active');
        }, 300);
    }
}

function openAppsPage() {
    const page = document.getElementById('apps-fullpage');
    if (!page) return;

    // Mevcut state'i kaydet — kapanınca restore edeceğiz
    window._appsPagePrevState = {
        centerStage: document.getElementById('center-stage')?.style.display || '',
        chatDisplay: document.getElementById('chat-display')?.style.display || '',
        mainContainer: document.getElementById('main-container')?.style.display || '',
    };

    // Animasyonlu aç
    page.style.display    = 'block';
    page.style.opacity    = '0';
    page.style.transform  = 'translateY(12px)';
    page.style.transition = 'opacity 0.22s ease, transform 0.22s ease';
    requestAnimationFrame(() => requestAnimationFrame(() => {
        page.style.opacity   = '1';
        page.style.transform = 'translateY(0)';
    }));

    document.body.style.overflow = 'hidden';
    page.scrollTop = 0;
}

function closeAppsPage() {
    const page = document.getElementById('apps-fullpage');
    if (!page) return;

    page.style.opacity   = '0';
    page.style.transform = 'translateY(12px)';

    setTimeout(() => {
        page.style.display    = 'none';
        page.style.opacity    = '';
        page.style.transform  = '';
        page.style.transition = '';
        document.body.style.overflow = '';

        // Önceki state'i restore et
        const prev = window._appsPagePrevState || {};
        const centerStage   = document.getElementById('center-stage');
        const chatDisplay   = document.getElementById('chat-display');
        const mainContainer = document.getElementById('main-container');
        if (centerStage)   centerStage.style.display   = prev.centerStage   ?? '';
        if (chatDisplay)   chatDisplay.style.display   = prev.chatDisplay   ?? '';
        if (mainContainer) mainContainer.style.display = prev.mainContainer ?? '';

        // Nav butonunu sıfırla
        document.getElementById('nav-chats')?.classList.add('active');
        document.getElementById('nav-apps')?.classList.remove('active');

    }, 220);
}

function toggleConvDropdown() {
    const toggle = document.getElementById('conv-dropdown-toggle');
    const body   = document.getElementById('conv-dropdown-body');
    if (!toggle || !body) return;
    const isOpen = body.classList.contains('open');
    if (isOpen) {
        body.classList.remove('open');
        toggle.classList.remove('open');
    } else {
        body.classList.add('open');
        toggle.classList.add('open');
    }
}
// ─────────────────────────────────────────────────────────────

function smTab(btn, id) {
    document.querySelectorAll('.sm-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.sm-section').forEach(s => s.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(id)?.classList.add('active');
}

async function smSaveProfile() {
    const token = localStorage.getItem('token');
    const name  = document.getElementById('sm-name')?.value.trim();
    const email = document.getElementById('sm-email')?.value.trim();
    const msg   = document.getElementById('sm-pmsg');
    try {
        const pendingStyle = localStorage.getItem('avatar_style_pending') || localStorage.getItem('avatar_style') || 'avataaars';
        const res  = await fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({ action:'update_profile', token, name, avatar_style: pendingStyle }) });  // email değiştirilemez
        const data = await res.json();
        if (data.status === 'success') {
            msg.style.color = '#00e676'; msg.textContent = '✓ Profil güncellendi';
            if (name) { localStorage.setItem('user_name', name); const el = document.getElementById('user-display-name'); if(el) el.textContent = name; }
            // Avatar stilini kaydet ve güncelle
            const savedStyle = localStorage.getItem('avatar_style_pending') || localStorage.getItem('avatar_style') || 'avataaars';
            localStorage.setItem('avatar_style', savedStyle);
            localStorage.removeItem('avatar_style_pending');
            const _seed = encodeURIComponent(name || localStorage.getItem('user_name') || 'U');
            const userAv = document.getElementById('user-avatar');
            if (userAv) userAv.src = `/avatars/${savedStyle}.svg`;
        } else {
            msg.style.color = '#ff4b4b'; msg.textContent = data.message || data.detail || 'Hata oluştu';
        }
    } catch(e) { msg.style.color = '#ff4b4b'; msg.textContent = 'Bağlantı hatası'; }
}

async function smSaveNotifs() {
    const token = localStorage.getItem('token');
    try {
        await fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({
            action:'update_notifications', token,
            email_updates:  document.getElementById('n-upd')?.checked,
            email_security: document.getElementById('n-sec')?.checked,
            email_billing:  document.getElementById('n-bil')?.checked,
        })});
        const msg = document.getElementById('sm-nmsg');
        if(msg) { msg.textContent = '✓ Kaydedildi'; setTimeout(()=>msg.textContent='', 2000); }
    } catch(e) {}
}

async function smCancelSub() {
    if (!confirm('Aboneliğinizi iptal etmek istediğinize emin misiniz?\nMevcut dönem sonuna kadar erişim devam eder.')) return;
    const token = localStorage.getItem('token');
    try {
        const res  = await fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'subscription_cancel', token, immediate: false }) });
        const data = await res.json();
        showToast(data.message || 'Abonelik iptal edildi', 'info');
        document.getElementById('settings-modal')?.remove();
        await loadSubscriptionStatus();
    } catch(e) { showToast('Hata oluştu', 'error'); }
}

async function smDeleteAccount() {
    const confirmed = prompt('Hesabınızı silmek istediğinizden emin misiniz?\nTüm verileriniz silinecek.\n\nOnaylamak için "SİL" yazın:');
    if (confirmed !== 'SİL') { if(confirmed !== null) showToast('Onay eşleşmedi', 'warn'); return; }
    const token = localStorage.getItem('token');
    try {
        const res  = await fetch(PROXY_URL, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ action:'delete_account', token }) });
        const data = await res.json();
        if (data.status === 'success') { showToast('Hesabınız silindi', 'info'); setTimeout(() => { localStorage.clear(); location.reload(); }, 1500); }
    } catch(e) { showToast('Hata oluştu', 'error'); }
}