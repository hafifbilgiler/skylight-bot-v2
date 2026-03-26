<?php
$pageTitle    = "Company Information";
$pageSubtitle = "About SKYMERGE TECHNOLOGY and ONE-BUNE";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'sirket-bilgileri.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/company-en-content.php';
$content = ob_get_clean();
include 'template.php';
