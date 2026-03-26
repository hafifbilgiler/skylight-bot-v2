<?php
$pageTitle    = "Çerez Politikası";
$pageSubtitle = "Çerezleri nasıl kullandığımız ve nasıl kontrol edebileceğiniz";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'cookie-policy.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/cerez-content.php';
$content = ob_get_clean();
include 'template.php';
