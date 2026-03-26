<?php
$pageTitle    = "Şirket Bilgileri";
$pageSubtitle = "SKYMERGE TECHNOLOGY hakkında bilgiler";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'company.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/company-content.php';
$content = ob_get_clean();
include 'template.php';
