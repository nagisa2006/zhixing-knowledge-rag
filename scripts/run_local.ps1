param(
    [string]$Config = 'artifacts/local/knowledge-env.json',
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$repoPath = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $repoPath $Config }
$configValues = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
foreach ($entry in $configValues.PSObject.Properties) {
    [Environment]::SetEnvironmentVariable($entry.Name, [string]$entry.Value, 'Process')
}
Set-Location -LiteralPath $repoPath
$env:PYTHONUTF8 = '1'
& $Python -m knowledge_service.server
exit $LASTEXITCODE
