param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArguments
)

$ErrorActionPreference = 'Stop'
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$cliPath = Join-Path $codexHome 'tools\integrations\context7\node_modules\.bin\ctx7.cmd'

if (-not (Test-Path -LiteralPath $cliPath)) {
    Write-Error "Context7 CLI is not installed at $cliPath. Run the project setup instructions in .codex/README.md."
    exit 1
}

& $cliPath @CliArguments
exit $LASTEXITCODE
