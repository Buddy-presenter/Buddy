param([Parameter(Mandatory=$true)][string]$OutputDirectory, [switch]$BundleOnly)
$ErrorActionPreference = "Stop"
if (!$env:WINDOWS_SIGNING_PFX_BASE64 -or !$env:WINDOWS_SIGNING_PASSWORD) { throw "Production signing credentials are required" }
$certificateFile = Join-Path $env:RUNNER_TEMP "buddy-signing.pfx"
[IO.File]::WriteAllBytes($certificateFile, [Convert]::FromBase64String($env:WINDOWS_SIGNING_PFX_BASE64))
$certificate = $null
try {
    $password = ConvertTo-SecureString $env:WINDOWS_SIGNING_PASSWORD -AsPlainText -Force
    $certificate = Import-PfxCertificate -FilePath $certificateFile -CertStoreLocation Cert:\CurrentUser\My -Password $password
    $signTool = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe" | Sort-Object FullName | Select-Object -Last 1 -ExpandProperty FullName
    if (!$signTool) { throw "Windows SDK signing tool is required" }
    $files = @((Join-Path $OutputDirectory "bundle\Buddy\Buddy.exe"))
    if (!$BundleOnly) { $files += @(Get-ChildItem -LiteralPath $OutputDirectory -Filter "*.exe" | Select-Object -ExpandProperty FullName) }
    foreach ($file in $files) {
        & $signTool sign /sha1 $certificate.Thumbprint /fd SHA256 /tr https://timestamp.digicert.com /td SHA256 $file
        if ($LASTEXITCODE -ne 0) { throw "Signing failed" }
        & $signTool verify /pa $file
        if ($LASTEXITCODE -ne 0) { throw "Signature verification failed" }
    }
} finally {
    if ($certificate) { Remove-Item -LiteralPath ("Cert:\CurrentUser\My\" + $certificate.Thumbprint) }
    Remove-Item -LiteralPath $certificateFile -ErrorAction SilentlyContinue
}
