param(
    [Parameter(Mandatory = $true)]
    [string]$Aria2Exe,
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$Aria2Exe = [IO.Path]::GetFullPath($Aria2Exe)
if (-not (Test-Path -LiteralPath $Aria2Exe -PathType Leaf)) {
    throw "aria2c.exe was not found: $Aria2Exe"
}

$ownsOutputDirectory = [string]::IsNullOrWhiteSpace($OutputDirectory)
if ($ownsOutputDirectory) {
    $OutputDirectory = Join-Path ([IO.Path]::GetTempPath()) "cove-aria2-tls-$([guid]::NewGuid().ToString('N'))"
}
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

function Invoke-Aria2TlsCase {
    param(
        [string]$Name,
        [string]$Url,
        [bool]$ShouldSucceed
    )

    $outputName = "$Name.download"
    $outputPath = Join-Path $OutputDirectory $outputName
    if (Test-Path -LiteralPath $outputPath) {
        Remove-Item -LiteralPath $outputPath -Force
    }

    $arguments = @(
        "--check-certificate=true",
        "--max-tries=1",
        "--connect-timeout=20",
        "--timeout=45",
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--summary-interval=0",
        "--console-log-level=notice",
        "--split=1",
        "--max-connection-per-server=1",
        "--dir=$OutputDirectory",
        "--out=$outputName",
        $Url
    )
    $output = @(& $Aria2Exe @arguments 2>&1)
    $exitCode = $LASTEXITCODE
    $created = Test-Path -LiteralPath $outputPath
    $nonEmpty = $created -and (Get-Item -LiteralPath $outputPath).Length -gt 0

    if ($ShouldSucceed -and ($exitCode -ne 0 -or -not $nonEmpty)) {
        throw "$Name should succeed but aria2 exited $exitCode.`n$($output -join [Environment]::NewLine)"
    }
    if (-not $ShouldSucceed -and $exitCode -eq 0) {
        throw "$Name should reject the invalid certificate but succeeded."
    }

    [pscustomobject]@{
        Name = $Name
        Expected = if ($ShouldSucceed) { "success" } else { "certificate rejection" }
        ExitCode = $exitCode
        Passed = $true
    }
}

try {
    $results = @(
        Invoke-Aria2TlsCase `
            -Name "github-archive" `
            -Url "https://github.com/germondai/trawl/archive/refs/tags/v1.0.0.zip" `
            -ShouldSucceed $true
        Invoke-Aria2TlsCase `
            -Name "codeload-archive" `
            -Url "https://codeload.github.com/germondai/trawl/zip/refs/tags/v1.0.0" `
            -ShouldSucceed $true
        Invoke-Aria2TlsCase `
            -Name "expired-certificate" `
            -Url "https://expired.badssl.com/" `
            -ShouldSucceed $false
        Invoke-Aria2TlsCase `
            -Name "wrong-host-certificate" `
            -Url "https://wrong.host.badssl.com/" `
            -ShouldSucceed $false
        Invoke-Aria2TlsCase `
            -Name "self-signed-certificate" `
            -Url "https://self-signed.badssl.com/" `
            -ShouldSucceed $false
    )
    $results | Format-Table -AutoSize
}
finally {
    if ($ownsOutputDirectory -and (Test-Path -LiteralPath $OutputDirectory)) {
        Remove-Item -LiteralPath $OutputDirectory -Recurse -Force
    }
}

# Expected rejection cases leave $LASTEXITCODE set to aria2's non-zero result.
# Reaching here means every assertion passed, so return success explicitly.
exit 0
