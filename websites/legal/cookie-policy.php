<?php
$pageTitle    = "Cookie Policy";
$pageSubtitle = "How we use cookies and similar technologies";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'cerez-politikasi.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/cookie-en-content.php';
$content = ob_get_clean();
include 'template.php';
