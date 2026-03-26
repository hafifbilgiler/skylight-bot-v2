<?php
$pageTitle    = "Distance Sales Agreement";
$pageSubtitle = "Terms governing Premium subscription purchase and right of withdrawal";
$lastUpdated  = "March 08, 2026";
$lang         = "en";
$langSwitch   = ['url' => 'mesafeli-satis.php', 'label' => 'Türkçe', 'flag' => '🇹🇷'];
ob_start();
include 'content/distance-sales-en-content.php';
$content = ob_get_clean();
include 'template.php';
