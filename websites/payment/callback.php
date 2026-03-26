<?php
/**
 * ONE-BUNE iyzico Callback Handler
 * URL: https://one-bune.com/payment/callback.php
 */

error_reporting(0);
ini_set('display_errors', 0);

$env = parse_ini_file(dirname(__DIR__) . '/.env', false, INI_SCANNER_RAW)
    ?: parse_ini_file(__DIR__ . '/.env', false, INI_SCANNER_RAW);

$payment_service_url = trim($env['PAYMENT_SERVICE_URL'] ?? '');

$token = $_POST['token'] ?? $_GET['token'] ?? '';

if (!$token) {
    header('Location: https://one-bune.com?payment=error');
    exit;
}

// Payment service'e callback bildir
if ($payment_service_url) {
    $ch = curl_init($payment_service_url . '/payment/callback');
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_POSTFIELDS     => http_build_query(['token' => $token]),
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_SSL_VERIFYPEER => true,
    ]);
    $result = curl_exec($ch);
    $code   = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    if ($code === 200) {
        header('Location: https://one-bune.com?premium=1');
        exit;
    }
}

header('Location: https://one-bune.com?payment=error');
exit;