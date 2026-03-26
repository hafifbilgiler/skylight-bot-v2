<?php
$pageTitle    = "Hizmet Şartları";
$pageSubtitle = "ONE-BUNE platformunu kullanarak kabul ettiğiniz koşullar";
$lastUpdated  = "08 Mart 2026";
$lang         = "tr";
$langSwitch   = ['url' => 'terms.php', 'label' => 'English', 'flag' => '🇬🇧'];
ob_start();
include 'content/hizmet-sozlesmesi-content.php';
$content = ob_get_clean();
include 'template.php';
