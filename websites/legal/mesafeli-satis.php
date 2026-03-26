<?php
$pageTitle    = "Mesafeli Satış Sözleşmesi";
$pageSubtitle = "Premium abonelik satışına ilişkin mesafeli satış sözleşmesi ve cayma hakkı";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'distance-sales.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/mesafeli-satis-content.php';
$content = ob_get_clean();
include 'template.php';
