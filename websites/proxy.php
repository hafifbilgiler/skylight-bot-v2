<?php
// Hata raporlama — HTML yerine JSON döndür
error_reporting(0);
ini_set('display_errors', 0);

set_exception_handler(function($e) {
    header('Content-Type: application/json; charset=UTF-8');
    http_response_code(500);
    echo json_encode(['status' => 'error', 'message' => 'Sunucu hatası: ' . $e->getMessage()]);
    exit;
});

set_error_handler(function($errno, $errstr) {
    error_log("PHP Error [{$errno}]: {$errstr}");
    return true;
});

ini_set('max_execution_time', '120');
ini_set('memory_limit', '256M');

session_start();

/*
|--------------------------------------------------------------------------
| CORS — sadece izinli origin'lere izin ver
|--------------------------------------------------------------------------
*/
$allowed_origins = [
    'https://one-bune.com',
    'https://www.one-bune.com',
];

$origin = $_SERVER['HTTP_ORIGIN'] ?? '';

if (in_array($origin, $allowed_origins, true)) {
    header("Access-Control-Allow-Origin: {$origin}");
    header('Vary: Origin');
}

header('Access-Control-Allow-Methods: POST, GET, DELETE, OPTIONS');
header('Access-Control-Allow-Headers: Content-Type, Authorization');
header('Content-Type: application/json; charset=UTF-8');

if ($_SERVER['REQUEST_METHOD'] === 'OPTIONS') {
    http_response_code(204);
    exit;
}

// ENV dosyasını yükle
$env = parse_ini_file(__DIR__ . '/.env', false, INI_SCANNER_RAW);

if ($env === false) {
    error_log('[ONE-BUNE PROXY] ENV load failed');
    http_response_code(500);
    echo json_encode([
        'status' => 'error',
        'message' => 'Sunucu yapılandırma hatası.'
    ]);
    exit;
}

$api_base_url = trim($env['API_BASE_URL'] ?? '');
$bearer_token = trim($env['API_BEARER_TOKEN'] ?? '');
$payment_service_url = trim($env['PAYMENT_SERVICE_URL'] ?? '');

if ($api_base_url === '' || $bearer_token === '' || $payment_service_url === '') {
    error_log('[ONE-BUNE PROXY] ENV missing required values');
    http_response_code(500);
    echo json_encode([
        'status' => 'error',
        'message' => 'Sunucu yapılandırma hatası.'
    ]);
    exit;
}

/*
|--------------------------------------------------------------------------
| Upload Security Helpers
|--------------------------------------------------------------------------
*/
function normalize_extension(string $filename): string {
    return strtolower(pathinfo($filename, PATHINFO_EXTENSION));
}

function get_allowed_upload_map(): array {
    return [
        'pdf' => ['application/pdf'],

        'png'  => ['image/png'],
        'jpg'  => ['image/jpeg'],
        'jpeg' => ['image/jpeg'],
        'webp' => ['image/webp'],

        'txt' => ['text/plain'],
        'md'  => ['text/plain', 'text/markdown'],
        'csv' => ['text/plain', 'text/csv', 'application/csv', 'application/vnd.ms-excel'],
        'tsv' => ['text/plain', 'text/tab-separated-values'],

        'json' => ['application/json', 'text/plain'],
        'yaml' => ['text/plain', 'application/x-yaml', 'text/yaml'],
        'yml'  => ['text/plain', 'application/x-yaml', 'text/yaml'],
        'xml'  => ['application/xml', 'text/xml', 'text/plain'],

        'html' => ['text/html', 'text/plain'],
        'css'  => ['text/css', 'text/plain'],
        'js'   => ['application/javascript', 'text/javascript', 'text/plain'],
        'ts'   => ['text/plain', 'application/typescript'],
        'jsx'  => ['text/plain', 'application/javascript'],
        'tsx'  => ['text/plain', 'application/typescript'],

        'py'   => ['text/plain', 'text/x-python', 'application/x-python-code'],
        'java' => ['text/plain'],
        'go'   => ['text/plain'],
        'rs'   => ['text/plain'],
        'c'    => ['text/plain'],
        'cpp'  => ['text/plain'],
        'h'    => ['text/plain'],
        'cs'   => ['text/plain'],
        'rb'   => ['text/plain'],
        'php'  => ['text/plain', 'application/x-httpd-php'],
        'sql'  => ['text/plain', 'application/sql'],
        'sh'   => ['text/plain', 'application/x-sh'],
        'bash' => ['text/plain', 'application/x-sh'],

        'toml' => ['text/plain'],
        'ini'  => ['text/plain'],
        'cfg'  => ['text/plain'],
        'conf' => ['text/plain'],
        'env'  => ['text/plain'],
        'log'  => ['text/plain'],

        'tf'   => ['text/plain'],
        'hcl'  => ['text/plain'],

        'doc'  => ['application/msword'],
        'docx' => ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
    ];
}

function detect_real_mime(string $tmpPath): string {
    $finfo = new finfo(FILEINFO_MIME_TYPE);
    return $finfo->file($tmpPath) ?: 'application/octet-stream';
}

function is_probably_text_file(string $tmpPath): bool {
    $sample = file_get_contents($tmpPath, false, null, 0, 4096);
    if ($sample === false) {
        return false;
    }

    if (strpos($sample, "\0") !== false) {
        return false;
    }

    return mb_check_encoding($sample, 'UTF-8') || preg_match('//u', $sample);
}

function validate_uploaded_file(array $file): array {
    $allowedMap = get_allowed_upload_map();

    $filename = $file['name'] ?? '';
    $tmpPath = $file['tmp_name'] ?? '';

    if ($filename === '' || $tmpPath === '' || !is_uploaded_file($tmpPath)) {
        return [false, 'Geçersiz upload dosyası.'];
    }

    $ext = normalize_extension($filename);

    if (!isset($allowedMap[$ext])) {
        return [false, "Bu dosya türüne izin verilmiyor: ." . $ext];
    }

    $realMime = detect_real_mime($tmpPath);
    $allowedMimes = $allowedMap[$ext];

    $textLikeExts = [
        'txt','md','csv','tsv','json','yaml','yml','xml','html','css','js','ts','jsx','tsx',
        'py','java','go','rs','c','cpp','h','cs','rb','php','sql','sh','bash',
        'toml','ini','cfg','conf','env','log','tf','hcl'
    ];

    if (in_array($ext, $textLikeExts, true)) {
        if (
            !in_array($realMime, $allowedMimes, true) &&
            !str_starts_with($realMime, 'text/') &&
            !is_probably_text_file($tmpPath)
        ) {
            return [false, "Dosya içeriği beklenen metin formatında değil. Algılanan MIME: {$realMime}"];
        }
    } else {
        if (!in_array($realMime, $allowedMimes, true)) {
            return [false, "Dosya türü doğrulanamadı. Algılanan MIME: {$realMime}"];
        }
    }

    return [true, $realMime];
}


// =====================================================
// 🔥 FILE UPLOAD (multipart — ayrı handling)
// =====================================================
if (isset($_POST['action']) && $_POST['action'] === 'upload_file') {

    if (!isset($_FILES['file'])) {
        $maxPost = ini_get('post_max_size');
        http_response_code(400);
        echo json_encode([
            'status' => 'error',
            'message' => "Dosya alınamadı. PHP post_max_size={$maxPost}. Dosya çok büyük olabilir."
        ]);
        exit;
    }

    $uploadError = $_FILES['file']['error'];
    if ($uploadError !== UPLOAD_ERR_OK) {
        $errorMessages = [
            UPLOAD_ERR_INI_SIZE   => 'Dosya PHP upload_max_filesize limitini aşıyor (' . ini_get('upload_max_filesize') . ')',
            UPLOAD_ERR_FORM_SIZE  => 'Dosya form limitini aşıyor',
            UPLOAD_ERR_PARTIAL    => 'Dosya kısmen yüklendi',
            UPLOAD_ERR_NO_FILE    => 'Dosya seçilmedi',
            UPLOAD_ERR_NO_TMP_DIR => 'Sunucuda tmp klasörü yok',
            UPLOAD_ERR_CANT_WRITE => 'Sunucuda yazma hatası',
        ];
        $msg = $errorMessages[$uploadError] ?? "Bilinmeyen upload hatası (kod: {$uploadError})";
        http_response_code(400);
        echo json_encode(['status' => 'error', 'message' => $msg]);
        exit;
    }

    $user_token = $_POST['token'] ?? null;
    $file = $_FILES['file'];

    [$validFile, $mimeOrError] = validate_uploaded_file($file);

    if (!$validFile) {
        http_response_code(400);
        echo json_encode([
            'status' => 'error',
            'message' => $mimeOrError
        ]);
        exit;
    }

    $validatedMime = $mimeOrError;

    $ch = curl_init($api_base_url . '/upload');
    $cfile = new CURLFile(
        $file['tmp_name'],
        $validatedMime,
        $file['name']
    );

    $headers = ['Accept: application/json'];
    if ($user_token) {
        $headers[] = 'Authorization: Bearer ' . $user_token;
    } else {
        $headers[] = 'Authorization: Bearer ' . $bearer_token;
    }

    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_POSTFIELDS => ['file' => $cfile],
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT => 30,
    ]);

    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    if (curl_errno($ch)) {
        http_response_code(500);
        echo json_encode(['status' => 'error', 'message' => 'Backend bağlantı hatası: ' . curl_error($ch)]);
        curl_close($ch);
        exit;
    }

    if ($http_code >= 400) http_response_code($http_code);
    header('Content-Type: application/json');
    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// JSON ACTIONS (normal flow)
// =====================================================
$input = file_get_contents('php://input');
$data = json_decode($input, true);

if ($data === null && strlen($input) === 0) {
    $maxPost = ini_get('post_max_size');
    header('Content-Type: application/json; charset=UTF-8');
    http_response_code(413);
    echo json_encode([
        'status' => 'error',
        'message' => "İstek çok büyük. PHP post_max_size={$maxPost}. Görseli küçültüp tekrar deneyin."
    ]);
    exit;
}
if ($data === null) {
    header('Content-Type: application/json; charset=UTF-8');
    http_response_code(400);
    echo json_encode([
        'status' => 'error',
        'message' => 'JSON parse hatası: ' . json_last_error_msg()
    ]);
    exit;
}

if (isset($data['mode']) && $data['mode'] === 'vision') {
    error_log("[PROXY VISION] mode=vision, image_data=" . (isset($data['image_data']) ? strlen($data['image_data']) . " bytes" : "YOK") . ", image_type=" . ($data['image_type'] ?? 'YOK'));
}

$user_token = $data['token'] ?? null;

// =====================================================
// 🔥 CSRF TOKEN GENERATION (İYZİCO İÇİN)
// =====================================================
if (isset($data['action']) && $data['action'] === 'generate_csrf') {
    if (session_status() === PHP_SESSION_NONE) {
        session_start();
    }
    
    if (empty($_SESSION['csrf_token'])) {
        $_SESSION['csrf_token'] = bin2hex(random_bytes(32));
    }
    
    header('Content-Type: application/json');
    echo json_encode(['csrf_token' => $_SESSION['csrf_token']]);
    exit;
}

// =====================================================
// 🔥 CODE MODE STATUS — plan bilgisi dahil
// =====================================================
if (isset($data['action']) && $data['action'] === 'code_mode_status') {
    $ch = curl_init($api_base_url . '/code-mode/status');

    $headers = ['Content-Type: application/json'];
    if ($user_token) {
        $headers[] = 'Authorization: Bearer ' . $user_token;
    } else {
        $headers[] = 'Authorization: Bearer ' . $bearer_token;
    }

    curl_setopt_array($ch, [
        CURLOPT_HTTPGET => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT => 5,
    ]);

    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    if (curl_errno($ch) || $http_code >= 400) {
        echo json_encode(['enabled' => false, 'model' => null, 'provider' => null]);
    } else {
        header('Content-Type: application/json');
        echo $response;
    }
    curl_close($ch);
    exit;
}

// =====================================================
// 🔥 SUBSCRIPTION STATUS
// =====================================================
if (isset($data['action']) && $data['action'] === 'subscription_status') {
    if (!$user_token) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Login required']);
        exit;
    }

    $ch = curl_init($api_base_url . '/subscription/status');
    curl_setopt_array($ch, [
        CURLOPT_HTTPGET => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $user_token,
            'Content-Type: application/json'
        ],
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT => 10,
    ]);
    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    if ($http_code >= 400) http_response_code($http_code);
    header('Content-Type: application/json');
    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// 🔥 SUBSCRIPTION PLANS
// =====================================================
if (isset($data['action']) && $data['action'] === 'subscription_plans') {
    $ch = curl_init($api_base_url . '/subscription/plans');
    curl_setopt_array($ch, [
        CURLOPT_HTTPGET => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $bearer_token,
            'Content-Type: application/json'
        ],
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT => 10,
    ]);
    $response = curl_exec($ch);
    header('Content-Type: application/json');
    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// 🔥 SUBSCRIPTION UPGRADE
// =====================================================
if (isset($data['action']) && $data['action'] === 'subscription_upgrade') {
    if (!$user_token) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Login required']);
        exit;
    }

    $post_body = json_encode([
        'billing_period' => $data['billing_period'] ?? 'monthly'
    ]);

    $ch = curl_init($api_base_url . '/subscription/upgrade');
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $user_token,
            'Content-Type: application/json'
        ],
        CURLOPT_POSTFIELDS => $post_body,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT => 30,
    ]);
    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    if ($http_code >= 400) http_response_code($http_code);
    header('Content-Type: application/json');
    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// ⚙️ PROFİL & AYARLAR
// =====================================================
$settings_actions = [
    'get_profile'          => ['method' => 'GET',  'path' => '/profile'],
    'get_usage'            => ['method' => 'GET',  'path' => '/profile/usage'],
    'get_payment_history'  => ['method' => 'GET',  'path' => '/profile/payment-history'],
    'update_profile'       => ['method' => 'PUT',  'path' => '/profile/update'],
    'update_notifications' => ['method' => 'PUT',  'path' => '/profile/notifications'],
    'delete_account'       => ['method' => 'DELETE','path' => '/profile/account'],
];

if (isset($data['action']) && isset($settings_actions[$data['action']])) {
    if (!$user_token) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Login required']);
        exit;
    }

    $cfg    = $settings_actions[$data['action']];
    $method = $cfg['method'];
    $path   = $cfg['path'];
    $url    = $api_base_url . $path;

    // Body gerekiyorsa hazırla (GET ve DELETE hariç)
    $body = null;
    if ($method === 'PUT') {
        $payload = $data;
        unset($payload['action'], $payload['token']);
        $body = json_encode($payload);
    }

    $ch = curl_init($url);
    $headers = [
        'Authorization: Bearer ' . $user_token,
        'Content-Type: application/json',
    ];

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => $headers,
        CURLOPT_CUSTOMREQUEST  => $method,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT        => 15,
    ]);

    if ($body) {
        curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
    }

    $response  = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($http_code >= 400) http_response_code($http_code);
    header('Content-Type: application/json');
    echo $response ?: json_encode(['status' => 'error', 'message' => 'No response']);
    exit;
}

// =====================================================
// 🔥 PAYMENT CHECKOUT — YENİ EKLENEN HANDLER
// =====================================================
if (isset($data['action']) && $data['action'] === 'payment_checkout') {
    if (!$user_token) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Login required']);
        exit;
    }

    $post_body = json_encode([
        'token'          => $user_token,
        'gsmNumber'      => $data['gsmNumber']      ?? '',
        'identityNumber' => $data['identityNumber'] ?? '',
        'address'        => $data['address']         ?? '',
        'city'           => $data['city']            ?? '',
        'zipCode'        => $data['zipCode']         ?? '',
    ]);

    $ch = curl_init($payment_service_url . '/payment/checkout');
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => [
            'Authorization: Bearer ' . $user_token,
            'Content-Type: application/json',
        ],
        CURLOPT_POSTFIELDS     => $post_body,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT        => 45,
    ]);

    $response  = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    if (curl_errno($ch)) {
        http_response_code(500);
        echo json_encode([
            'status'  => 'error',
            'message' => 'Payment service bağlantı hatası: ' . curl_error($ch)
        ]);
        curl_close($ch);
        exit;
    }

    curl_close($ch);

    if ($http_code >= 400) http_response_code($http_code);
    header('Content-Type: application/json');
    echo $response;
    exit;
}

// =====================================================
// 🔥 SUBSCRIPTION CANCEL — payment service'e yönlendir
// =====================================================
if (isset($data['action']) && $data['action'] === 'subscription_cancel') {
    if (!$user_token) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Login required']);
        exit;
    }

    $post_body = json_encode([
        'token'     => $user_token,
        'immediate' => $data['immediate'] ?? false,
    ]);

    $ch = curl_init($payment_service_url . '/payment/subscription/cancel');
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => [
            'Authorization: Bearer ' . $user_token,
            'Content-Type: application/json',
        ],
        CURLOPT_POSTFIELDS     => $post_body,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_TIMEOUT        => 15,
    ]);
    $response  = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    if ($http_code >= 400) http_response_code($http_code);
    header('Content-Type: application/json');
    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// OTP ENDPOINTS
// =====================================================
if (isset($data['action']) && in_array($data['action'], ['request_code', 'verify_code'])) {
    $ch = curl_init($api_base_url . '/' . $data['action']);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $bearer_token,
            'Content-Type: application/json'
        ],
        CURLOPT_POSTFIELDS => $input,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2
    ]);

    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    if ($http_code >= 400) http_response_code($http_code);

    $resData = json_decode($response, true);

    if (isset($resData['status']) && $resData['status'] === 'success' && isset($resData['user']['id'])) {
        $_SESSION['user_id'] = $resData['user']['id'];
        $_SESSION['user_name'] = $resData['user']['name'];
    }

    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// CONVERSATION ACTIONS
// =====================================================
$conversation_actions = ['list_conversations', 'get_conversation', 'create_conversation', 'delete_conversation', 'update_conversation'];

if (isset($data['action']) && in_array($data['action'], $conversation_actions)) {

    if (!$user_token) {
        http_response_code(401);
        echo json_encode(['status' => 'error', 'message' => 'Login required']);
        exit;
    }

    $endpoint_map = [
        'list_conversations' => '/conversations/list',
        'get_conversation' => '/conversations/' . ($data['conversation_id'] ?? 'unknown') . '/messages',
        'create_conversation' => '/conversations/create',
        'delete_conversation' => '/conversations/' . ($data['conversation_id'] ?? 'unknown'),
        'update_conversation' => '/conversations/' . ($data['conversation_id'] ?? 'unknown')
    ];

    $endpoint = $endpoint_map[$data['action']];

    if ($data['action'] === 'list_conversations' || $data['action'] === 'get_conversation') {
        $method = 'GET';
        if ($data['action'] === 'list_conversations') $endpoint .= '?limit=50';
    } elseif ($data['action'] === 'delete_conversation') {
        $method = 'DELETE';
    } elseif ($data['action'] === 'update_conversation') {
        $method = 'PUT';
    } else {
        $method = 'POST';
    }

    $ch = curl_init($api_base_url . $endpoint);

    $headers = [
        'Content-Type: application/json',
        'Authorization: Bearer ' . $user_token
    ];

    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => $headers,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2
    ]);

    if ($method === 'POST' || $method === 'PUT') {
        $post_data = [];
        if ($data['action'] === 'create_conversation') {
            $post_data['title'] = $data['title'] ?? 'Yeni Sohbet';
        }
        if ($data['action'] === 'update_conversation') {
            if (!empty($data['title'])) $post_data['title'] = $data['title'];
        }
        curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($post_data, JSON_UNESCAPED_UNICODE));
    }

    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);

    if ($http_code >= 400) http_response_code($http_code);

    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// CHECK USER
// =====================================================
if (isset($data['action']) && $data['action'] === 'check_user') {
    $ch = curl_init($api_base_url . '/check_user');
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $bearer_token,
            'Content-Type: application/json'
        ],
        CURLOPT_POSTFIELDS => $input,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2
    ]);

    $response = curl_exec($ch);
    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// LOGIN & REGISTER
// =====================================================
if (isset($data['action']) && in_array($data['action'], ['login', 'register'])) {
    $ch = curl_init($api_base_url . '/' . $data['action']);
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . $bearer_token,
            'Content-Type: application/json'
        ],
        CURLOPT_POSTFIELDS => $input,
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2
    ]);

    $response = curl_exec($ch);
    $resData = json_decode($response, true);

    if (isset($resData['status']) && $resData['status'] === 'success') {
        $_SESSION['user_id'] = $resData['user']['id'] ?? null;
        $_SESSION['user_name'] = $resData['user']['name'] ?? null;
    }

    echo $response;
    curl_close($ch);
    exit;
}

// =====================================================
// CHAT (streaming — file_id + mode destekli)
// =====================================================
if (isset($data['action']) && $data['action'] === 'chat') {
    header('Content-Type: text/plain; charset=UTF-8');
    header('Cache-Control: no-cache');
    header('X-Accel-Buffering: no');

    $post_body = [
        'prompt' => $data['prompt'],
        'stream' => true,
        'history' => $data['history'] ?? null,
        'user_id' => $_SESSION['user_id'] ?? 'guest',
        'conversation_id' => $data['conversation_id'] ?? null,
    ];

    if (!empty($data['mode'])) $post_body['mode'] = $data['mode'];
    if (!empty($data['file_id'])) $post_body['file_id'] = $data['file_id'];
    if (!empty($data['file_context'])) $post_body['file_context'] = $data['file_context'];
    if (!empty($data['image_data'])) $post_body['image_data'] = $data['image_data'];
    if (!empty($data['image_type'])) $post_body['image_type'] = $data['image_type'];

    $ch = curl_init($api_base_url . '/chat');
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . ($user_token ? $user_token : $bearer_token),
            'Content-Type: application/json',
        ],
        CURLOPT_POSTFIELDS => json_encode($post_body, JSON_UNESCAPED_UNICODE),
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
        CURLOPT_WRITEFUNCTION => function ($ch, $data) {
            echo $data;
            if (ob_get_level() > 0) ob_flush();
            flush();
            return strlen($data);
        },
    ]);
    curl_exec($ch);
    curl_close($ch);
}

// =====================================================
// 🔥 FEEDBACK ACTION
// =====================================================
if (isset($data['action']) && $data['action'] === 'feedback') {
    $post_body = [
        'conversation_id' => $data['conversation_id'] ?? '',
        'user_query' => $data['user_query'] ?? '',
        'assistant_response' => $data['assistant_response'] ?? '',
        'rating' => intval($data['rating'] ?? 0),
        'comment' => $data['comment'] ?? '',
    ];

    $ch = curl_init($api_base_url . '/feedback');
    curl_setopt_array($ch, [
        CURLOPT_POST => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER => [
            'Authorization: Bearer ' . ($user_token ? $user_token : $bearer_token),
            'Content-Type: application/json',
        ],
        CURLOPT_POSTFIELDS => json_encode($post_body, JSON_UNESCAPED_UNICODE),
        CURLOPT_SSL_VERIFYPEER => true,
        CURLOPT_SSL_VERIFYHOST => 2,
    ]);
    $response = curl_exec($ch);
    $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    header('Content-Type: application/json');
    http_response_code($http_code);
    echo $response;
    exit;
}
?>