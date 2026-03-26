<!DOCTYPE html>
<html lang="<?= $lang ?? 'tr' ?>">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title><?= htmlspecialchars($pageTitle ?? 'ONE-BUNE') ?> | ONE-BUNE</title>
<link rel="icon" href="/logo.png" type="image/png">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
    --bg-primary:   #0a0a0c;
    --bg-secondary: #111114;
    --bg-tertiary:  #1a1a1f;
    --accent-cyan:  #00f2fe;
    --accent-purple:#bc4efd;
    --accent-gradient: linear-gradient(135deg,#00f2fe,#bc4efd);
    --text-primary: #eeeef0;
    --text-secondary:#8e8ea0;
    --text-muted:   #55556a;
    --border-color: rgba(255,255,255,0.07);
    --border-subtle:rgba(255,255,255,0.04);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
    background:var(--bg-primary);
    color:var(--text-primary);
    font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
    line-height:1.7;
    -webkit-font-smoothing:antialiased;
    position:relative;
}
body::before{
    content:'';
    position:fixed;inset:0;
    background:
        radial-gradient(ellipse 800px 600px at 20% 30%,rgba(0,242,254,.03) 0%,transparent 70%),
        radial-gradient(ellipse 600px 400px at 80% 60%,rgba(188,78,253,.025) 0%,transparent 70%);
    pointer-events:none;z-index:0;
}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.08);border-radius:10px}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.15)}

/* HEADER */
.legal-header{
    position:sticky;top:0;z-index:100;
    background:rgba(10,10,12,.9);
    backdrop-filter:blur(20px) saturate(180%);
    border-bottom:1px solid var(--border-color);
}
.header-inner{
    max-width:900px;margin:0 auto;
    padding:14px 24px;
    display:flex;align-items:center;justify-content:space-between;gap:16px;
}
.logo-link{
    display:flex;align-items:center;gap:10px;
    text-decoration:none;color:var(--text-primary);
    transition:opacity .2s;
}
.logo-link:hover{opacity:.8}
.logo-orb{
    width:34px;height:34px;border-radius:9px;
    background:var(--accent-gradient);
    display:flex;align-items:center;justify-content:center;
    font-size:14px;color:#fff;
}
.logo-text{
    font-size:16px;font-weight:800;letter-spacing:.5px;
    background:var(--accent-gradient);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.header-right{display:flex;align-items:center;gap:10px}
.lang-btn{
    display:flex;align-items:center;gap:6px;
    padding:6px 12px;border-radius:8px;
    background:rgba(255,255,255,.05);
    border:1px solid var(--border-color);
    color:var(--text-secondary);font-size:12px;font-weight:500;
    text-decoration:none;transition:all .2s;cursor:pointer;
}
.lang-btn:hover{background:rgba(255,255,255,.08);color:var(--text-primary)}
.home-btn{
    display:flex;align-items:center;gap:6px;
    padding:6px 14px;border-radius:8px;
    background:rgba(0,242,254,.07);
    border:1px solid rgba(0,242,254,.15);
    color:var(--accent-cyan);font-size:12px;font-weight:600;
    text-decoration:none;transition:all .2s;
}
.home-btn:hover{background:rgba(0,242,254,.12);transform:translateY(-1px)}

/* MAIN */
.legal-container{
    max-width:860px;margin:0 auto;
    padding:50px 24px 80px;
    position:relative;z-index:1;
}

/* HERO */
.legal-hero{
    text-align:center;
    margin-bottom:40px;
    padding-bottom:36px;
    border-bottom:1px solid var(--border-color);
}
.legal-icon{
    display:inline-flex;align-items:center;justify-content:center;
    width:60px;height:60px;border-radius:16px;
    background:linear-gradient(135deg,rgba(0,242,254,.12),rgba(188,78,253,.1));
    border:1px solid rgba(0,242,254,.2);
    font-size:22px;color:var(--accent-cyan);
    margin-bottom:20px;
}
.legal-hero h1{
    font-size:32px;font-weight:800;color:#fff;
    letter-spacing:-.5px;margin-bottom:10px;
}
.legal-hero .subtitle{
    font-size:15px;color:var(--text-secondary);max-width:580px;margin:0 auto 20px;
}
.legal-meta{
    display:flex;gap:20px;justify-content:center;flex-wrap:wrap;
}
.legal-meta-item{
    display:flex;align-items:center;gap:6px;
    font-size:12px;color:var(--text-muted);
}
.legal-meta-item i{color:var(--accent-cyan);font-size:11px}

/* CONTENT SECTIONS */
.legal-content h2{
    font-size:20px;font-weight:700;color:#fff;
    margin:36px 0 14px;padding-top:8px;
    border-top:1px solid var(--border-subtle);
}
.legal-content h2:first-child{border-top:none;margin-top:0}
.legal-content h3{
    font-size:15px;font-weight:600;color:var(--accent-cyan);
    margin:22px 0 10px;
}
.legal-content h4{
    font-size:14px;font-weight:600;color:var(--text-primary);
    margin:16px 0 8px;
}
.legal-content p{
    font-size:14px;color:var(--text-secondary);
    line-height:1.75;margin-bottom:14px;
}
.legal-content ul,.legal-content ol{
    margin:10px 0 16px 22px;
}
.legal-content li{
    font-size:14px;color:var(--text-secondary);
    margin-bottom:7px;line-height:1.65;
}
.legal-content strong{color:var(--text-primary);font-weight:600}
.legal-content a{color:var(--accent-cyan);text-decoration:none}
.legal-content a:hover{text-decoration:underline}
code{
    font-family:'JetBrains Mono',monospace;
    font-size:12px;color:var(--accent-cyan);
    background:rgba(0,242,254,.08);
    padding:2px 6px;border-radius:4px;
}

/* INFO BOXES */
.info-box{
    border-radius:12px;padding:16px 18px;margin:16px 0;
    border:1px solid;
}
.info-box.info{
    background:rgba(0,242,254,.05);
    border-color:rgba(0,242,254,.15);
}
.info-box.warning{
    background:rgba(255,145,0,.05);
    border-color:rgba(255,145,0,.2);
}
.info-box.danger{
    background:rgba(255,75,75,.05);
    border-color:rgba(255,75,75,.2);
}
.info-box-header{
    display:flex;align-items:center;gap:8px;
    font-size:13px;font-weight:700;margin-bottom:10px;
}
.info-box.info .info-box-header{color:var(--accent-cyan)}
.info-box.warning .info-box-header{color:#ff9100}
.info-box.danger .info-box-header{color:#ff4b4b}
.info-box p,.info-box li{color:var(--text-secondary);font-size:13px;margin-bottom:6px}
.info-box ul{margin:8px 0 0 18px}

/* TABLE */
.legal-table{
    width:100%;border-collapse:collapse;
    margin:14px 0;font-size:13px;
    border-radius:10px;overflow:hidden;
}
.legal-table thead tr{background:rgba(255,255,255,.04)}
.legal-table th{
    padding:10px 14px;text-align:left;
    font-size:11px;font-weight:700;text-transform:uppercase;
    letter-spacing:.5px;color:var(--text-muted);
    border-bottom:1px solid var(--border-color);
}
.legal-table td{
    padding:10px 14px;color:var(--text-secondary);
    border-bottom:1px solid var(--border-subtle);
    vertical-align:top;
}
.legal-table tr:last-child td{border-bottom:none}
.legal-table tr:hover td{background:rgba(255,255,255,.02)}

/* CONTACT CARD */
.contact-card{
    background:linear-gradient(135deg,rgba(0,242,254,.05),rgba(188,78,253,.04));
    border:1px solid rgba(0,242,254,.12);
    border-radius:14px;padding:24px;margin-top:32px;text-align:center;
}
.contact-card h3{
    font-size:16px;font-weight:700;color:#fff;margin-bottom:6px;
}
.contact-card p{font-size:13px;color:var(--text-secondary);margin-bottom:16px}
.contact-links{display:flex;gap:10px;justify-content:center;flex-wrap:wrap}
.contact-link{
    display:flex;align-items:center;gap:7px;
    padding:9px 18px;border-radius:9px;
    background:rgba(255,255,255,.05);
    border:1px solid var(--border-color);
    color:var(--text-secondary);font-size:13px;font-weight:500;
    text-decoration:none;transition:all .2s;
}
.contact-link:hover{
    background:rgba(0,242,254,.08);
    border-color:rgba(0,242,254,.2);
    color:var(--accent-cyan);
    transform:translateY(-1px);
}

/* SECTION (legacy support) */
.section{margin-bottom:28px}
.section-title{
    font-size:18px;font-weight:700;color:#fff;
    margin-bottom:12px;padding-bottom:8px;
    border-bottom:1px solid var(--border-subtle);
}
.highlight-box{
    background:linear-gradient(135deg,rgba(0,242,254,.06),rgba(188,78,253,.04));
    border:1px solid rgba(0,242,254,.15);
    border-radius:12px;padding:18px 20px;margin-bottom:24px;
}
.highlight-box h3{
    font-size:14px;font-weight:700;color:var(--accent-cyan);margin-bottom:8px;
}
.highlight-box p{font-size:13px;color:var(--text-secondary);margin:0}

/* ACCORDION */
.accordion-container{margin:16px 0}
.accordion-item{
    background:rgba(255,255,255,.02);
    border:1px solid var(--border-color);
    border-radius:10px;margin-bottom:8px;overflow:hidden;
    transition:border-color .2s;
}
.accordion-item:hover{border-color:rgba(0,242,254,.15)}
.accordion-header{
    width:100%;padding:14px 16px;background:none;border:none;
    color:var(--text-primary);font-size:14px;font-weight:600;
    display:flex;align-items:center;justify-content:space-between;
    cursor:pointer;transition:background .2s;text-align:left;
}
.accordion-header:hover{background:rgba(0,242,254,.03)}
.accordion-header span{display:flex;align-items:center;gap:10px}
.accordion-header i:first-child{color:var(--accent-cyan);font-size:13px}
.accordion-icon{color:var(--text-muted);font-size:11px;transition:transform .3s}
.accordion-item.active .accordion-icon{transform:rotate(180deg)}
.accordion-content{
    max-height:0;overflow:hidden;
    transition:max-height .3s ease,padding .3s ease;
    padding:0 16px;
}
.accordion-item.active .accordion-content{
    max-height:2000px;padding:0 16px 16px;
}
.accordion-content p{margin-bottom:10px;font-size:13px;line-height:1.65}
.accordion-content ul{margin-left:18px;margin-bottom:10px}
.accordion-content ul li{margin-bottom:6px;font-size:13px}

/* INFO GRID */
.info-grid{
    display:grid;
    grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
    gap:12px;margin:16px 0;
}
.info-card{
    display:flex;align-items:flex-start;gap:12px;
    padding:14px 16px;
    background:rgba(255,255,255,.02);
    border:1px solid var(--border-color);
    border-radius:10px;transition:all .2s;
}
.info-card:hover{
    background:rgba(0,242,254,.03);
    border-color:rgba(0,242,254,.15);
}
.info-icon{
    width:34px;height:34px;min-width:34px;
    border-radius:8px;
    background:linear-gradient(135deg,rgba(0,242,254,.1),rgba(188,78,253,.08));
    display:flex;align-items:center;justify-content:center;
    color:var(--accent-cyan);font-size:14px;
}
.info-label{
    font-size:10px;color:var(--text-muted);
    text-transform:uppercase;letter-spacing:.5px;font-weight:600;margin-bottom:3px;
}
.info-value{font-size:13px;color:var(--text-primary);font-weight:500;word-wrap:break-word}

/* FOOTER */
.legal-footer{
    text-align:center;
    padding:32px 0 0;
    border-top:1px solid var(--border-color);
    margin-top:40px;
}
.legal-footer-links{
    display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-bottom:20px;
}
.legal-footer-link{
    font-size:12px;color:var(--text-muted);text-decoration:none;
    padding:4px 10px;border-radius:6px;transition:all .2s;
}
.legal-footer-link:hover{color:var(--text-secondary);background:rgba(255,255,255,.04)}
.legal-footer p{font-size:11px;color:var(--text-muted);margin-top:12px}

@media(max-width:640px){
    .legal-container{padding:32px 16px 60px}
    .legal-hero h1{font-size:24px}
    .header-inner{padding:12px 16px}
    .logo-text{font-size:14px}
    .info-grid{grid-template-columns:1fr}
    .contact-links{flex-direction:column;align-items:center}
    .legal-meta{flex-direction:column;gap:8px;align-items:center}
}
</style>
</head>
<body>
<header class="legal-header">
    <div class="header-inner">
        <a href="/" class="logo-link">
            <div class="logo-orb"><i class="fas fa-robot"></i></div>
            <span class="logo-text">ONE-BUNE</span>
        </a>
        <div class="header-right">
            <?php if(!empty($langSwitch)): ?>
            <a href="<?= htmlspecialchars($langSwitch['url']) ?>" class="lang-btn">
                <?= $langSwitch['flag'] ?> <?= htmlspecialchars($langSwitch['label']) ?>
            </a>
            <?php endif; ?>
            <a href="/" class="home-btn">
                <i class="fas fa-arrow-left"></i>
                <?= ($lang ?? 'tr') === 'en' ? 'Back' : 'Ana Sayfa' ?>
            </a>
        </div>
    </div>
</header>

<main class="legal-container">
    <?php echo $content ?? ''; ?>

    <footer class="legal-footer">
        <div class="legal-footer-links">
            <?php if(($lang ?? 'tr') === 'tr'): ?>
            <a href="hizmet-sartlari.php" class="legal-footer-link">Hizmet Şartları</a>
            <a href="gizlilik-politikasi.php" class="legal-footer-link">Gizlilik</a>
            <a href="cerez-politikasi.php" class="legal-footer-link">Çerezler</a>
            <a href="kvkk-aydinlatma.php" class="legal-footer-link">KVKK</a>
            <a href="abonelik-kosullari.php" class="legal-footer-link">Abonelik</a>
            <a href="mesafeli-satis.php" class="legal-footer-link">Mesafeli Satış</a>
            <a href="iade-iptal.php" class="legal-footer-link">İade & İptal</a>
            <a href="sirket-bilgileri.php" class="legal-footer-link">Şirket</a>
            <?php else: ?>
            <a href="terms.php" class="legal-footer-link">Terms</a>
            <a href="privacy-policy.php" class="legal-footer-link">Privacy</a>
            <a href="cookie-policy.php" class="legal-footer-link">Cookies</a>
            <a href="subscription-terms.php" class="legal-footer-link">Subscription</a>
            <a href="distance-sales.php" class="legal-footer-link">Distance Sales</a>
            <a href="refund-policy.php" class="legal-footer-link">Refund Policy</a>
            <a href="company.php" class="legal-footer-link">Company</a>
            <?php endif; ?>
        </div>
        <p>© <?= date('Y') ?> ONE-BUNE · SKYMERGE TECHNOLOGY</p>
    </footer>
</main>

<script>
function toggleAccordion(header) {
    const item = header.parentElement;
    const wasActive = item.classList.contains('active');
    document.querySelectorAll('.accordion-item').forEach(a => a.classList.remove('active'));
    if (!wasActive) item.classList.add('active');
}
</script>
</body>
</html>
