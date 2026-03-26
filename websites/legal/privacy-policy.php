<?php
$pageTitle    = "Privacy Policy";
$pageSubtitle = "How we collect, use and protect your personal data";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'gizlilik-politikasi.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/privacy-en-content.php';
$content = ob_get_clean();
include 'template.php';
