$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$toolRoot = Join-Path $codexHome 'tools\integrations'
$pnpm = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd'
$python = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$uvCommand = Get-Command uv.exe -ErrorAction SilentlyContinue

if (-not (Test-Path -LiteralPath $pnpm)) { throw "Codex-bundled pnpm is missing: $pnpm" }
if (-not (Test-Path -LiteralPath $python)) { throw "Codex-bundled Python is missing: $python" }
if (-not $uvCommand) { throw 'uv is required to install Serena into the Codex-owned tool directory.' }

$packages = @(
    @{ Name = 'context7'; Manifest = 'context7'; ToolDir = 'context7'; Kind = 'node' },
    @{ Name = 'playwright-mcp'; Manifest = 'playwright'; ToolDir = 'playwright-mcp'; Kind = 'node' },
    @{ Name = 'serena'; Manifest = 'serena'; ToolDir = 'serena'; Kind = 'python' }
)

foreach ($package in $packages) {
    $source = Join-Path $repoRoot ".codex\manifests\$($package.Manifest)"
    $destination = Join-Path $toolRoot $package.ToolDir
    New-Item -ItemType Directory -Force -Path $destination | Out-Null
    Get-ChildItem -LiteralPath $source -File | Copy-Item -Destination $destination -Force
}

$pnpmStore = Join-Path $codexHome 'tools\pnpm-store'
$uvCache = Join-Path $codexHome 'tools\uv-cache'
$codexTemp = Join-Path $codexHome 'tools\tmp'
New-Item -ItemType Directory -Force -Path $codexTemp,$pnpmStore,$uvCache | Out-Null
$env:TEMP = $codexTemp
$env:TMP = $codexTemp
foreach ($packageName in @('context7', 'playwright-mcp')) {
    $destination = Join-Path $toolRoot $packageName
    Push-Location $destination
    try {
        & $pnpm install --frozen-lockfile --store-dir $pnpmStore
        if ($LASTEXITCODE -ne 0) { throw "pnpm install failed for $packageName (exit $LASTEXITCODE)." }
    }
    finally { Pop-Location }
}

$serenaDir = Join-Path $toolRoot 'serena'
& $uvCommand.Source sync --directory $serenaDir --locked --python $python --cache-dir $uvCache
if ($LASTEXITCODE -ne 0) { throw "uv sync failed for Serena (exit $LASTEXITCODE)." }

Write-Output "Installed pinned integrations under $toolRoot"
