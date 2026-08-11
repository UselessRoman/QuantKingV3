$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "activate-toolchain.ps1")

& $env:QUANTKING_NPM_EXECUTABLE @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
