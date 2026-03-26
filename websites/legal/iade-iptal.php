<?php
$pageTitle    = "İptal ve İade Koşulları";
$pageSubtitle = "ONE-BUNE Premium abonelik iptali ve iade politikası";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'refund-policy.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/iade-content.php';
$content = ob_get_clean();
include 'template.php';
