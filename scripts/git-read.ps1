[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet("cat-file", "check-ignore", "diff", "log", "ls-files", "rev-parse", "show", "status")]
    [string]$Command,
    [Parameter(Position = 1, ValueFromRemainingArguments)]
    [string[]]$ArgumentList
)

$ErrorActionPreference = "Stop"
$GitExecutable = Get-Command git -CommandType Application -ErrorAction Stop |
    Select-Object -First 1

& $GitExecutable.Source `
    "--no-optional-locks" `
    "-c" `
    "core.excludesFile=" `
    $Command `
    @ArgumentList
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
