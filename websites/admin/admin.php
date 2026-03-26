<?php
// ONE-BUNE Admin Panel — admin.php
// DB bağlantısı yok. Şifre → gateway. OTP → gateway SMTP.

error_reporting(0);
ini_set('display_errors', 0);
ini_set('max_execution_time', '30');
session_start();

// CORS
$allowed_origins = ['https://one-bune.com','https://www.one-bune.com'];
$origin = $_SERVER['HTTP_ORIGIN'] ?? '';
if (in_array($origin, $allowed_origins, true)) {
    header("Access-Control-Allow-Origin: {$origin}");
    header('Vary: Origin');
}
header('Access-Control-Allow-Methods: POST, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization');
header('Content-Type: application/json; charset=UTF-8');
header('Cache-Control: no-store, no-cache, must-revalidate');
if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') { http_response_code(204); exit; }
if ($_SERVER['REQUEST_METHOD'] !== 'POST')    { http_response_code(405); exit; }

// ── IP & INPUT — EN ÖNCE ────────────────────────────────────────
$input     = file_get_contents('php://input');
$data      = json_decode($input, true) ?? [];
$action    = $data['action'] ?? '';
$client_ip = trim(explode(',',
    $_SERVER['HTTP_X_FORWARDED_FOR'] ??
    $_SERVER['HTTP_X_REAL_IP']       ??
    $_SERVER['REMOTE_ADDR']          ?? '0.0.0.0'
)[0]);

// ── BRUTE FORCE ─────────────────────────────────────────────────
define('MAX_ATTEMPTS',    5);
define('LOCKOUT_SECONDS', 900);
function is_locked(): bool {
    return isset($_SESSION['admin_lockout_until']) && time() < $_SESSION['admin_lockout_until'];
}
function lockout_remaining(): int {
    return max(0, ($_SESSION['admin_lockout_until'] ?? 0) - time());
}
function record_fail(): int {
    $_SESSION['admin_attempts'] = ($_SESSION['admin_attempts'] ?? 0) + 1;
    if ($_SESSION['admin_attempts'] >= MAX_ATTEMPTS) {
        $_SESSION['admin_lockout_until'] = time() + LOCKOUT_SECONDS;
        $_SESSION['admin_attempts'] = 0;
        return 0;
    }
    return MAX_ATTEMPTS - $_SESSION['admin_attempts'];
}
function reset_attempts(): void {
    unset($_SESSION['admin_attempts'], $_SESSION['admin_lockout_until']);
}

// ── SESSION-ONLY ACTIONS — ENV gerekmez ─────────────────────────
if ($action === 'check_lockout') {
    echo json_encode(is_locked()
        ? ['locked'=>true,  'remaining_seconds'=>lockout_remaining()]
        : ['locked'=>false]);
    exit;
}
if ($action === 'admin_logout') {
    session_destroy();
    echo json_encode(['status'=>'success']);
    exit;
}

// ── ENV ─────────────────────────────────────────────────────────
// proxy.php public_html/.env'i __DIR__ ile buluyor.
// admin.php public_html/admin/ altında → dirname(__DIR__) = public_html/
$env = false;
$env_candidates = [
    dirname(__DIR__) . '/.env',  // public_html/.env
    __DIR__ . '/.env',           // public_html/admin/.env (fallback)
];
foreach ($env_candidates as $ep) {
    if (file_exists($ep)) {
        $env = parse_ini_file($ep, false, INI_SCANNER_RAW);
        if ($env !== false) break;
    }
}
if ($env === false) {
    error_log('[ADMIN] .env bulunamadi. Denenen: ' . implode(' | ', $env_candidates));
    http_response_code(500);
    echo json_encode(['status'=>'error','message'=>'.env dosyasi bulunamadi.']);
    exit;
}

$api_base_url = trim($env['API_BASE_URL']     ?? '');
$bearer_token = trim($env['API_BEARER_TOKEN'] ?? '');
if (!$api_base_url || !$bearer_token) {
    error_log('[ADMIN] ENV eksik: API_BASE_URL veya API_BEARER_TOKEN');
    http_response_code(500);
    echo json_encode(['status'=>'error','message'=>'Yapilandirma hatasi.']);
    exit;
}

// ── GATEWAY CURL ─────────────────────────────────────────────────
function call_gateway(string $endpoint, string $method='GET', array $body=[]): array {
    global $api_base_url, $bearer_token;
    $ch = curl_init($api_base_url . $endpoint);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => [
            'Content-Type: application/json',
            'Authorization: Bearer ' . $bearer_token,
        ],
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT        => 15,
        CURLOPT_CUSTOMREQUEST  => $method,
    ]);
    if (!empty($body))
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($body, JSON_UNESCAPED_UNICODE));
    $res  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $err  = curl_error($ch);
    curl_close($ch);
    if ($err) {
        error_log("[ADMIN] curl: {$err}");
        return ['status'=>'error','message'=>'Gateway baglanti hatasi.','_http_code'=>0];
    }
    $d = json_decode($res, true) ?? [];
    $d['_http_code'] = $code;
    return $d;
}

// ── ADIM 1: ŞİFRE ────────────────────────────────────────────────
if ($action === 'admin_login') {
    if (is_locked()) {
        echo json_encode(['status'=>'error','locked'=>true,
            'remaining_seconds'=>lockout_remaining(),'message'=>'Hesap kilitlendi.']);
        exit;
    }
    $username = trim($data['username'] ?? '');
    $password = $data['password']      ?? '';
    if (!$username || !$password) {
        echo json_encode(['status'=>'error','message'=>'Kullanici adi ve sifre gerekli.']);
        exit;
    }
    $result = call_gateway('/admin/verify_password','POST',['username'=>$username,'password'=>$password]);
    if ($result['status'] !== 'success') {
        $rem = record_fail();
        error_log("[ADMIN] Basarisiz: {$username} IP={$client_ip}");
        if ($rem === 0) {
            echo json_encode(['status'=>'error','locked'=>true,
                'remaining_seconds'=>LOCKOUT_SECONDS,'message'=>'Cok fazla hatali giris. 15 dakika bekleyin.']);
        } else {
            echo json_encode(['status'=>'error','locked'=>false,
                'attempts_left'=>$rem,'message'=>'Kullanici adi veya sifre yanlis.']);
        }
        exit;
    }
    reset_attempts();
    $_SESSION['admin_username_ok']      = $username;
    $_SESSION['admin_otp_attempts']     = 0;
    $_SESSION['admin_otp_resend_after'] = time() + 60;
    $otp = call_gateway('/admin/send_otp','POST',['username'=>$username]);
    if (($otp['_http_code'] ?? 0) >= 400) {
        error_log("[ADMIN] OTP gonderilemedi: " . json_encode($otp));
        unset($_SESSION['admin_username_ok']);
        echo json_encode(['status'=>'error','message'=>'Dogrulama kodu gonderilemedi.']);
        exit;
    }
    error_log("[ADMIN] OTP gonderildi: {$username} IP={$client_ip}");
    echo json_encode(['status'=>'otp_required','message'=>'Dogrulama kodu gonderildi.']);
    exit;
}

// ── ADIM 2: OTP ───────────────────────────────────────────────────
if ($action === 'verify_otp') {
    if (empty($_SESSION['admin_username_ok'])) {
        echo json_encode(['status'=>'error','message'=>'Once giris yapin.']);
        exit;
    }
    $_SESSION['admin_otp_attempts'] = ($_SESSION['admin_otp_attempts'] ?? 0) + 1;
    if ($_SESSION['admin_otp_attempts'] > 5) {
        unset($_SESSION['admin_username_ok'], $_SESSION['admin_otp_attempts']);
        echo json_encode(['status'=>'error','message'=>'Cok fazla yanlis kod.']);
        exit;
    }
    $result = call_gateway('/admin/verify_otp','POST',[
        'username' => $_SESSION['admin_username_ok'],
        'otp'      => trim($data['otp'] ?? ''),
    ]);
    if ($result['status'] !== 'success') {
        $rem = 5 - $_SESSION['admin_otp_attempts'];
        echo json_encode(['status'=>'error','expired'=>$result['expired']??false,
            'message'=>$result['message']??'Kod yanlis.','attempts_left'=>max(0,$rem)]);
        exit;
    }
    unset($_SESSION['admin_username_ok'],$_SESSION['admin_otp_attempts'],$_SESSION['admin_otp_resend_after']);
    session_regenerate_id(true);
    $_SESSION['admin_authenticated'] = true;
    $_SESSION['admin_ip']            = $client_ip;
    $_SESSION['admin_login_time']    = time();
    $_SESSION['csrf_token']          = bin2hex(random_bytes(32));
    error_log("[ADMIN] Basarili giris IP={$client_ip}");
    echo json_encode(['status'=>'success','csrf_token'=>$_SESSION['csrf_token']]);
    exit;
}

// ── RESEND OTP ────────────────────────────────────────────────────
if ($action === 'resend_otp') {
    if (empty($_SESSION['admin_username_ok'])) {
        echo json_encode(['status'=>'error','message'=>'Once giris yapin.']);
        exit;
    }
    if (time() < ($_SESSION['admin_otp_resend_after'] ?? 0)) {
        $w = ($_SESSION['admin_otp_resend_after'] - time());
        echo json_encode(['status'=>'error','message'=>"Lutfen {$w} saniye bekleyin."]);
        exit;
    }
    $_SESSION['admin_otp_resend_after'] = time() + 60;
    $_SESSION['admin_otp_attempts']     = 0;
    call_gateway('/admin/send_otp','POST',['username'=>$_SESSION['admin_username_ok']]);
    echo json_encode(['status'=>'success','message'=>'Yeni kod gonderildi.']);
    exit;
}

// ── CHECK SESSION ─────────────────────────────────────────────────
if ($action === 'check_session') {
    check_admin_auth();
    $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    echo json_encode(['status'=>'success','csrf_token'=>$_SESSION['csrf_token']]);
    exit;
}

// ── AUTH GUARD ────────────────────────────────────────────────────
function check_admin_auth(): void {
    global $client_ip;
    if (empty($_SESSION['admin_authenticated'])) {
        http_response_code(401);
        echo json_encode(['status'=>'error','message'=>'Giris yapilmamis.']);
        exit;
    }
    if ($_SESSION['admin_ip'] !== $client_ip) {
        session_destroy(); http_response_code(401);
        echo json_encode(['status'=>'error','message'=>'Oturum gecersiz.']);
        exit;
    }
    if (time() - $_SESSION['admin_login_time'] > 7200) {
        session_destroy(); http_response_code(401);
        echo json_encode(['status'=>'error','message'=>'Oturum suresi doldu.']);
        exit;
    }
}
check_admin_auth();

// ── ADMIN ACTIONS ─────────────────────────────────────────────────
if ($action==='toggle_admin') {
    $id=intval($data['user_id']??0); $en=(bool)($data['enable']??false);
    if (!$id) { echo json_encode(['status'=>'error','message'=>'user_id gerekli']); exit; }
    echo json_encode(call_gateway("/admin/users/{$id}/admin",'POST',['is_admin'=>$en])); exit;
}
if ($action==='get_stats') { echo json_encode(call_gateway('/admin/stats','GET')); exit; }
if ($action==='get_users') {
    $p=intval($data['page']??1); $l=intval($data['limit']??20);
    $s=urlencode($data['search']??''); $f=urlencode($data['filter']??'');
    echo json_encode(call_gateway('/admin/users'."?page={$p}&limit={$l}".($s?"&search={$s}":'').($f?"&filter={$f}":''),'GET')); exit;
}
if ($action==='get_user') {
    $id=intval($data['user_id']??0);
    if (!$id) { echo json_encode(['status'=>'error','message'=>'user_id gerekli']); exit; }
    echo json_encode(call_gateway("/admin/users/{$id}",'GET')); exit;
}
if ($action==='toggle_premium') {
    $id=intval($data['user_id']??0); $en=(bool)($data['enable']??false);
    if (!$id) { echo json_encode(['status'=>'error','message'=>'user_id gerekli']); exit; }
    echo json_encode(call_gateway("/admin/users/{$id}/premium",'POST',['premium'=>$en])); exit;
}
if ($action==='ban_user') {
    $id=intval($data['user_id']??0); $ban=(bool)($data['ban']??true);
    $reason=substr(trim($data['reason']??''),0,500);
    if (!$id) { echo json_encode(['status'=>'error','message'=>'user_id gerekli']); exit; }
    echo json_encode(call_gateway("/admin/users/{$id}/ban",'POST',['banned'=>$ban,'reason'=>$reason])); exit;
}
if ($action==='reset_usage') {
    $id=intval($data['user_id']??0);
    if (!$id) { echo json_encode(['status'=>'error','message'=>'user_id gerekli']); exit; }
    echo json_encode(call_gateway("/admin/users/{$id}/reset_usage",'POST')); exit;
}
if ($action==='cancel_subscription') {
    $id        = intval($data['user_id'] ?? 0);
    $immediate = (bool)($data['immediate'] ?? true);
    if (!$id) { echo json_encode(['status'=>'error','message'=>'user_id gerekli']); exit; }
    // Payment service'e direkt git
    global $api_base_url, $bearer_token;
    $payment_url = trim($env['PAYMENT_SERVICE_URL'] ?? '');
    // user'ın token'ını bul
    $user_row = call_gateway("/admin/users/{$id}", 'GET');
    $ch = curl_init($payment_url . '/payment/subscription/cancel');
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => [
            'Content-Type: application/json',
            'Authorization: Bearer ' . $bearer_token,
        ],
        CURLOPT_POSTFIELDS => json_encode([
            'user_id'   => $id,
            'immediate' => $immediate,
            'admin'     => true,
        ]),
        CURLOPT_TIMEOUT => 15,
    ]);
    $res  = curl_exec($ch);
    $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);
    $d = json_decode($res, true) ?? [];
    $d['_http_code'] = $code;
    echo json_encode($d); exit;
}
if ($action==='get_payments') {
    $p=intval($data['page']??1); $l=intval($data['limit']??20);
    echo json_encode(call_gateway("/admin/payments?page={$p}&limit={$l}",'GET')); exit;
}
if ($action==='get_logs') {
    $p=intval($data['page']??1); $l=intval($data['limit']??50); $t=urlencode($data['type']??'');
    echo json_encode(call_gateway('/admin/logs'."?page={$p}&limit={$l}".($t?"&type={$t}":''),'GET')); exit;
}

http_response_code(400);
echo json_encode(['status'=>'error','message'=>'Bilinmeyen action.']);