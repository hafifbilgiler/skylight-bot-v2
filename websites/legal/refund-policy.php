<?php
$pageTitle    = "Refund Policy";
$pageSubtitle = "ONE-BUNE Premium cancellation and refund terms";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'iade-iptal.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/refund-en-content.php';
$content = ob_get_clean();
include 'template.php';
