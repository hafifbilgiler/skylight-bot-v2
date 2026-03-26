<?php
$pageTitle    = "Terms of Service";
$pageSubtitle = "Your rights and responsibilities when using ONE-BUNE";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'hizmet-sartlari.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/terms-en-content.php';
$content = ob_get_clean();
include 'template.php';
