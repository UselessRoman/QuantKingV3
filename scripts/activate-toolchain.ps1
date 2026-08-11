[CmdletBinding()]
param(
    [string]$ToolchainRoot = "E:\Apps\QuantKingV3Toolchain",
    [string]$UvCacheDirectory
)

$UvVersion = "0.11.28"
$NodeVersion = "24.18.0"
$NpmVersion = "11.16.0"
$PythonVersion = "3.12.13"

$UvDirectory = Join-Path $ToolchainRoot "uv"
$NodeDirectory = Join-Path $ToolchainRoot "node\$NodeVersion\node-v$NodeVersion-win-x64"
$PythonInstallDirectory = Join-Path $ToolchainRoot "python"
$PythonDirectory = Join-Path $PythonInstallDirectory "cpython-$PythonVersion-windows-x86_64-none"
$UvExecutable = Join-Path $UvDirectory "uv.exe"
$NodeExecutable = Join-Path $NodeDirectory "node.exe"
$NpmExecutable = Join-Path $NodeDirectory "npm.cmd"
$PythonExecutable = Join-Path $PythonDirectory "python.exe"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot

foreach ($Executable in @($UvExecutable, $NodeExecutable, $NpmExecutable, $PythonExecutable)) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
        throw "Required tool is missing: $Executable"
    }
}

if ([string]::IsNullOrWhiteSpace($UvCacheDirectory)) {
    if ([string]::IsNullOrWhiteSpace($env:QUANTKING_UV_CACHE_DIR)) {
        $UvCacheDirectory = Join-Path $ToolchainRoot "cache\uv"
    }
    else {
        $UvCacheDirectory = $env:QUANTKING_UV_CACHE_DIR
    }
}
if (-not [System.IO.Path]::IsPathFullyQualified($UvCacheDirectory)) {
    $UvCacheDirectory = Join-Path $RepositoryRoot $UvCacheDirectory
}
$UvCacheDirectory = [System.IO.Path]::GetFullPath($UvCacheDirectory)

$env:QUANTKING_TOOLCHAIN_ROOT = $ToolchainRoot
$env:QUANTKING_UV_EXECUTABLE = $UvExecutable
$env:QUANTKING_NODE_EXECUTABLE = $NodeExecutable
$env:QUANTKING_NPM_EXECUTABLE = $NpmExecutable
$env:QUANTKING_PYTHON_EXECUTABLE = $PythonExecutable
$env:QUANTKING_UV_CACHE_DIR = $UvCacheDirectory
$env:UV_CACHE_DIR = $UvCacheDirectory
$env:UV_PYTHON_INSTALL_DIR = $PythonInstallDirectory
$env:UV_PYTHON_PREFERENCE = "managed"

$RequiredPathEntries = @($UvDirectory, $NodeDirectory, $PythonDirectory)
$ExistingPathEntries = @($env:PATH -split ";" | Where-Object {
        $_ -and $_ -notin $RequiredPathEntries
    })
$env:PATH = (@($RequiredPathEntries) + $ExistingPathEntries) -join ";"

$ActualUv = (& $UvExecutable --version).Trim()
$ActualNode = (& $NodeExecutable --version).Trim()
$ActualNpm = (& $NpmExecutable --version).Trim()
$ActualPython = (& $PythonExecutable --version).Trim()

if ($ActualUv -notmatch "^uv $([regex]::Escape($UvVersion))\b") {
    throw "Expected uv $UvVersion, found: $ActualUv"
}
if ($ActualNode -ne "v$NodeVersion") {
    throw "Expected Node v$NodeVersion, found: $ActualNode"
}
if ($ActualNpm -ne $NpmVersion) {
    throw "Expected npm $NpmVersion, found: $ActualNpm"
}
if ($ActualPython -ne "Python $PythonVersion") {
    throw "Expected Python $PythonVersion, found: $ActualPython"
}

Write-Output (
    "QuantKing toolchain active in current shell: " +
    "uv $UvVersion, Python $PythonVersion, Node $NodeVersion, npm $NpmVersion; " +
    "uv cache $UvCacheDirectory"
)
