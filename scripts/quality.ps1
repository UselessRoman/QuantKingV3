[CmdletBinding()]
param(
    [string]$ToolchainRoot = "E:\Apps\QuantKingV3Toolchain"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-NativeStep {
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [Parameter(Mandatory)]
        [string]$FilePath,
        [Parameter(Mandatory)]
        [string[]]$ArgumentList
    )

    Write-Output "==> $Name"
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$PreviousLocation = Get-Location
try {
    Set-Location -LiteralPath $RepositoryRoot
    . (Join-Path $PSScriptRoot "activate-toolchain.ps1") -ToolchainRoot $ToolchainRoot

    $Uv = Join-Path $ToolchainRoot "uv\uv.exe"
    $Npm = Join-Path $ToolchainRoot "node\24.18.0\node-v24.18.0-win-x64\npm.cmd"

    Invoke-NativeStep "uv lock check" $Uv @("lock", "--check", "--offline")
    Invoke-NativeStep "frozen dependency sync" $Uv @(
        "sync", "--frozen", "--all-groups", "--offline"
    )
    Invoke-NativeStep "Ruff lint" $Uv @("run", "--offline", "--frozen", "ruff", "check", ".")
    Invoke-NativeStep "Ruff format check" $Uv @(
        "run", "--offline", "--frozen", "ruff", "format", "--check", "."
    )
    Invoke-NativeStep "mypy" $Uv @(
        "run", "--offline", "--frozen", "mypy", "src/quantking"
    )
    Invoke-NativeStep "offline pytest" $Uv @(
        "run", "--offline", "--frozen", "pytest",
        "-m", "not real_external and not real_akshare and not real_legacy_data and not real_backup",
        "-q", "-p", "no:cacheprovider"
    )
    Invoke-NativeStep "architecture boundary check" $Uv @(
        "run", "--offline", "--frozen", "python", "scripts/check_architecture_boundaries.py"
    )
    Invoke-NativeStep "Markdown link check" $Uv @(
        "run", "--offline", "--frozen", "python", "scripts/check_markdown_links.py"
    )

    Invoke-NativeStep "npm clean install" $Npm @(
        "--prefix", "frontend", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"
    )
    Invoke-NativeStep "frontend typecheck" $Npm @("--prefix", "frontend", "run", "typecheck")
    Invoke-NativeStep "frontend unit tests" $Npm @("--prefix", "frontend", "run", "test")
    Invoke-NativeStep "frontend production build" $Npm @("--prefix", "frontend", "run", "build")
    Invoke-NativeStep "frontend e2e" $Npm @("--prefix", "frontend", "run", "test:e2e")
    Invoke-NativeStep "Git whitespace check" "git" @("diff", "--check")
}
finally {
    Set-Location -LiteralPath $PreviousLocation
}
