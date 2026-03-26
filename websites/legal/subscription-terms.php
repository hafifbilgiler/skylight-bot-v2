<?php
$pageTitle    = "Subscription Terms";
$pageSubtitle = "ONE-BUNE Premium subscription, billing and cancellation";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'abonelik-kosullari.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/subscription-en-content.php';
$content = ob_get_clean();
include 'template.php';
