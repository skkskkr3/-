$ErrorActionPreference = 'Stop'
$release = Join-Path $PSScriptRoot 'release'
$stage = Join-Path $release 'Zhixu-Test'
$zip = Join-Path $release 'Zhixu-Test.zip'

New-Item -ItemType Directory -Force -Path $release | Out-Null
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
New-Item -ItemType Directory -Path $stage | Out-Null

foreach ($name in @('app.py','jwxt_adapter.py','launcher.py','run.ps1','Start-Zhixu.cmd','启动知序.cmd','requirements.txt','README.md','PRODUCT_MANUAL.md')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination $stage
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'static') -Destination $stage -Recurse
Compress-Archive -LiteralPath $stage -DestinationPath $zip
Remove-Item -LiteralPath $stage -Recurse -Force
Write-Host "Created: $zip"
