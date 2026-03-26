<?php
$pageTitle    = "KVKK Aydınlatma Metni";
$pageSubtitle = "6698 sayılı Kişisel Verilerin Korunması Kanunu kapsamında aydınlatma";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'privacy-policy.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/kvkk-content.php';
$content = ob_get_clean();
include 'template.php';
