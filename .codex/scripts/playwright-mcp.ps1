param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArguments
)

$ErrorActionPreference = 'Stop'
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$nodePath = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$cliPath = Join-Path $codexHome 'tools\integrations\playwright-mcp\node_modules\@playwright\mcp\cli.js'
$stateRoot = Join-Path $repoRoot '.codex\runtime\playwright'
New-Item -ItemType Directory -Force -Path (Join-Path $stateRoot 'local-appdata'),(Join-Path $stateRoot 'tmp'),(Join-Path $stateRoot 'output') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $stateRoot 'browsers') | Out-Null
$outputDir = Join-Path $stateRoot 'output'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $stateRoot 'browsers'
$env:LOCALAPPDATA = Join-Path $stateRoot 'local-appdata'
$env:PLAYWRIGHT_MCP_OUTPUT_DIR = $outputDir
$env:TEMP = Join-Path $stateRoot 'tmp'
$env:TMP = $env:TEMP
Set-Location $repoRoot

if (-not (Test-Path -LiteralPath $nodePath)) {
    Write-Error "Codex-bundled Node.js is not installed at $nodePath."
    exit 1
}
if (-not (Test-Path -LiteralPath $cliPath)) {
    Write-Error "Playwright MCP is not installed at $cliPath. Follow .codex/README.md."
    exit 1
}

& $nodePath $cliPath @CliArguments
exit $LASTEXITCODE
