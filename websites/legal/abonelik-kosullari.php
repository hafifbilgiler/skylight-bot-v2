<?php
$pageTitle    = "Abonelik Koşulları";
$pageSubtitle = "ONE-BUNE Premium aboneliği, ücretlendirme ve iptal koşulları";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'subscription-terms.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/abonelik-content.php';
$content = ob_get_clean();
include 'template.php';
