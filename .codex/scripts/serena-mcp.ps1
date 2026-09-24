param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArguments
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$serenaPath = Join-Path $codexHome 'tools\integrations\serena\.venv\Scripts\serena.exe'
$serenaHome = Join-Path $repoRoot '.serena\runtime'
$localTemp = Join-Path $serenaHome 'tmp'
New-Item -ItemType Directory -Force -Path `
    $serenaHome, `
    (Join-Path $serenaHome 'uv-cache'), `
    (Join-Path $serenaHome 'uv-python'), `
    (Join-Path $serenaHome 'npm-cache'), `
    $localTemp | Out-Null
$env:SERENA_HOME = $serenaHome
$env:UV_CACHE_DIR = Join-Path $serenaHome 'uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $serenaHome 'uv-python'
$env:NPM_CONFIG_CACHE = Join-Path $serenaHome 'npm-cache'
$env:TEMP = $localTemp
$env:TMP = $localTemp
Set-Location $repoRoot

if (-not (Test-Path -LiteralPath $serenaPath)) {
    Write-Error "Serena is not installed at $serenaPath. Follow .codex/README.md."
    exit 1
}

& $serenaPath @CliArguments
exit $LASTEXITCODE
