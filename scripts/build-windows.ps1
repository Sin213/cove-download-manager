param(
    [string]$Python = "python",
    [string]$Aria2Exe = "",
    [string]$YtDlpExe = "",
    [string]$ArtifactLabel = "",
    [switch]$Setup
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location $Root

function Remove-BuildDirectory([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $Root.TrimEnd('\') + '\'
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean outside the repository: $full"
    }
    if (Test-Path -LiteralPath $full) {
        Remove-Item -LiteralPath $full -Recurse -Force
    }
}

function Write-Sha256([string]$Path) {
    $item = Get-Item -LiteralPath $Path
    $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($item.Name)" | Set-Content -LiteralPath "$($item.FullName).sha256" -Encoding ascii
}

$versionLine = Select-String -LiteralPath "cove\__init__.py" -Pattern '^__version__\s*=\s*"([^"]+)"'
if (-not $versionLine.Matches.Success) {
    throw "Could not resolve Cove version"
}
$Version = $versionLine.Matches[0].Groups[1].Value

if (-not $Aria2Exe) {
    $Aria2Exe = $env:COVE_ARIA2_EXE
}
if (-not $Aria2Exe) {
    $Aria2Exe = Join-Path $Root "build\aria2-win\aria2c.exe"
}
$Aria2Exe = [IO.Path]::GetFullPath($Aria2Exe)
if (-not (Test-Path -LiteralPath $Aria2Exe -PathType Leaf)) {
    throw "aria2c.exe was not found. Pass -Aria2Exe PATH or set COVE_ARIA2_EXE."
}

$YtDlpWasProvided = -not [string]::IsNullOrWhiteSpace($YtDlpExe)
if (-not $YtDlpWasProvided) {
    $YtDlpExe = $env:COVE_YTDLP_EXE
    $YtDlpWasProvided = -not [string]::IsNullOrWhiteSpace($YtDlpExe)
}
if (-not $YtDlpWasProvided) {
    $YtDlpExe = Join-Path $Root "build\yt-dlp-win\yt-dlp.exe"
}
$YtDlpExe = [IO.Path]::GetFullPath($YtDlpExe)
if (-not (Test-Path -LiteralPath $YtDlpExe -PathType Leaf)) {
    if ($YtDlpWasProvided) {
        throw "yt-dlp.exe was not found at the supplied path: $YtDlpExe"
    }
    $YtDlpDir = Split-Path -Parent $YtDlpExe
    New-Item -ItemType Directory -Force -Path $YtDlpDir | Out-Null
    $YtDlpDownload = "$YtDlpExe.download"
    Write-Output "Downloading yt-dlp.exe"
    try {
        Invoke-WebRequest `
            -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" `
            -OutFile $YtDlpDownload
        Move-Item -LiteralPath $YtDlpDownload -Destination $YtDlpExe -Force
    }
    finally {
        if (Test-Path -LiteralPath $YtDlpDownload) {
            Remove-Item -LiteralPath $YtDlpDownload -Force
        }
    }
}
if (-not (Test-Path -LiteralPath $YtDlpExe -PathType Leaf)) {
    throw "yt-dlp.exe was not found. Pass -YtDlpExe PATH or set COVE_YTDLP_EXE."
}

& $Python -c "import PIL, PyInstaller, PySide6, requests" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Build Python is missing Pillow, PyInstaller, PySide6, or requests."
}

& $Python -c "from PIL import Image; Image.open(r'cove_dm_icon.png').save(r'cove_dm_icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if ($LASTEXITCODE -ne 0) {
    throw "Icon generation failed"
}

$WorkRoot = Join-Path $Root "build\win-native"
Remove-BuildDirectory $WorkRoot
New-Item -ItemType Directory -Force -Path $WorkRoot, (Join-Path $Root "release") | Out-Null

$CommonArgs = @(
    "--noconfirm", "--clean", "--log-level", "WARN",
    "--windowed",
    "--icon", "cove_dm_icon.ico",
    "--paths", ".",
    "--add-data", "cove_dm_icon.png;cove",
    "--add-data", "cove_icon.png;cove",
    "--add-binary", "$Aria2Exe;.",
    "--add-binary", "$YtDlpExe;.",
    "--hidden-import", "cove",
    "--hidden-import", "cove.app",
    "--hidden-import", "cove.api_server",
    "--hidden-import", "requests",
    "--collect-submodules", "requests",
    "--exclude-module", "PySide6.QtWebEngineCore",
    "--exclude-module", "PySide6.QtWebEngineWidgets",
    "--exclude-module", "PySide6.QtQml",
    "--exclude-module", "PySide6.QtQuick",
    "--exclude-module", "PySide6.QtPdf",
    "--exclude-module", "PySide6.Qt3DCore",
    "--exclude-module", "PySide6.QtCharts",
    "--exclude-module", "PySide6.QtDataVisualization",
    "--exclude-module", "PySide6.QtMultimedia",
    "--exclude-module", "PySide6.QtMultimediaWidgets",
    "--exclude-module", "tkinter"
)

$PortableDist = Join-Path $WorkRoot "portable-dist"
$PortableWork = Join-Path $WorkRoot "portable-work"
& $Python -m PyInstaller @CommonArgs `
    --onefile `
    --name "cove-download-manager-portable" `
    --distpath $PortableDist `
    --workpath $PortableWork `
    "packaging\portable_launcher.py"
if ($LASTEXITCODE -ne 0) {
    throw "Portable PyInstaller build failed"
}

$label = if ($ArtifactLabel) { "-$ArtifactLabel" } else { "" }
$PortableOutput = Join-Path $Root "release\Cove-Download-Manager-$Version$label-Portable.exe"
Copy-Item -LiteralPath (Join-Path $PortableDist "cove-download-manager-portable.exe") -Destination $PortableOutput -Force
Write-Sha256 $PortableOutput

if ($Setup) {
    $Iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path -LiteralPath $Iscc -PathType Leaf)) {
        throw "Inno Setup 6 is required for -Setup."
    }
    $OneDirDist = Join-Path $WorkRoot "onedir-dist"
    $OneDirWork = Join-Path $WorkRoot "onedir-work"
    & $Python -m PyInstaller @CommonArgs `
        --name "cove-download-manager" `
        --distpath $OneDirDist `
        --workpath $OneDirWork `
        "packaging\launcher.py"
    if ($LASTEXITCODE -ne 0) {
        throw "One-directory PyInstaller build failed"
    }
    $Bundle = Join-Path $OneDirDist "cove-download-manager"
    Copy-Item -LiteralPath "cove_dm_icon.png" -Destination $Bundle -Force
    Copy-Item -LiteralPath "README.md" -Destination $Bundle -Force
    Copy-Item -LiteralPath "LICENSE" -Destination $Bundle -Force
    $ReleaseDir = Join-Path $Root "release"
    & $Iscc "/DAppVersion=$Version" "/DSourceDir=$Bundle" "/DOutputDir=$ReleaseDir" "/DIconFile=$(Join-Path $Root 'cove_dm_icon.ico')" "packaging\installer.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup build failed"
    }
    Write-Sha256 (Join-Path $ReleaseDir "Cove-Download-Manager-$Version-Setup.exe")
}

$ClientStage = Join-Path $WorkRoot "cove-api"
New-Item -ItemType Directory -Force -Path $ClientStage | Out-Null
foreach ($name in @("cove-api.cmd", "cove_api.py", "wrapper_config.json", "README.md", "AI_WRAPPER_OPERATING_RULES.md", "AI_DIRECT_API_OPERATING_RULES.md")) {
    Copy-Item -LiteralPath (Join-Path $Root "tools\cove-api\$name") -Destination $ClientStage -Force
}
$ClientZip = Join-Path $Root "release\Cove-AI-Client-$Version.zip"
if (Test-Path -LiteralPath $ClientZip) {
    Remove-Item -LiteralPath $ClientZip -Force
}
Compress-Archive -Path (Join-Path $ClientStage "*") -DestinationPath $ClientZip
Write-Sha256 $ClientZip

Write-Output "Built: $PortableOutput"
Write-Output "Client: $ClientZip"
