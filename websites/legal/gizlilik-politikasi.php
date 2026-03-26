<?php
$pageTitle    = "Gizlilik Politikası";
$pageSubtitle = "Kişisel verilerinizin nasıl toplandığı, kullanıldığı ve korunduğu";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'privacy-policy.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/gizlilik-politikasi-content.php';
$content = ob_get_clean();
include 'template.php';
