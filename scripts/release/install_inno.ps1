$ErrorActionPreference = "Stop"
$directory = Join-Path $env:RUNNER_TEMP "buddy-inno-6.6.1"
New-Item -ItemType Directory -Path $directory -Force | Out-Null
$installer = Join-Path $directory "setup.exe"
Invoke-WebRequest "https://github.com/jrsoftware/issrc/releases/download/is-6_6_1/innosetup-6.6.1.exe" -OutFile $installer
if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne "d243ce440c02705530699554fb9612b9b2bd7a2a90629cdb7f41e66f5faeb91f") { throw "Compiler installer integrity check failed" }
$arguments = @("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER", "/NOICONS", '/TASKS=""', ('/DIR="' + $directory + '"'))
$process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "Compiler installation failed" }
$compiler = Join-Path $directory "ISCC.exe"
if (!(Test-Path -LiteralPath $compiler)) { throw "Compiler executable is missing" }
if ($env:GITHUB_ENV) { "INNO_SETUP_COMPILER=$compiler" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append }
Write-Output $compiler
