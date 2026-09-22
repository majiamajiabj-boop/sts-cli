param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$attemptRoot = (Resolve-Path (Join-Path $root 'logs/attempts')).Path
$acceptanceRoot = (Resolve-Path (Join-Path $root 'logs/start-acceptances')).Path
$live = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^python.*\.exe$' -and $_.CommandLine -match 'autoplay(?:_runner|_shared_v4)?\.py|campaign_attempt\.py' -and
    $_.CommandLine -match [regex]::Escape($root)
}
if ($live) { throw 'A workspace controller is active; retention must wait.' }
$all = @(Get-ChildItem -LiteralPath $attemptRoot -Directory | Sort-Object LastWriteTimeUtc,Name)
if ($all.Count -lt 6) { throw 'Fewer than six attempts; refusing retention.' }
$keep = @($all | Select-Object -Last 6)
$selectionIds = @()
$hashes = @{}
foreach ($dir in $keep) {
    foreach ($name in @('selection.json','run-result.json','terminal-state.json','run-audit.json','autoplay.log')) {
        if (!(Test-Path -LiteralPath (Join-Path $dir.FullName $name))) { throw "Incomplete retained attempt: $($dir.Name)/$name" }
    }
    $selectionIds += (Get-Content -LiteralPath (Join-Path $dir.FullName 'selection.json') -Raw | ConvertFrom-Json).selection_id
    foreach ($file in (Get-ChildItem -LiteralPath $dir.FullName -File -Recurse)) {
        $hashes[$file.FullName] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}
$remove = @($all | Where-Object { $_.Name -notin $keep.Name })
$retainedAcceptances = 0
foreach ($dir in (Get-ChildItem -LiteralPath $acceptanceRoot -Directory)) {
    $record = Get-Content -LiteralPath (Join-Path $dir.FullName 'start-acceptance.json') -Raw | ConvertFrom-Json
    if ($record.selection_id -in $selectionIds) { $retainedAcceptances++; continue }
    $remove += $dir
}
if ($retainedAcceptances -ne 6) { throw "Expected six matching acceptances, got $retainedAcceptances" }
$bytes = [long]0
foreach ($dir in $remove) {
    $resolved = (Resolve-Path -LiteralPath $dir.FullName).Path
    $parent = Split-Path -Parent $resolved
    if ($parent -notin @($attemptRoot,$acceptanceRoot)) { throw "Outside retention scope: $resolved" }
    if ($dir.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Linked target: $resolved" }
    $children = @(Get-ChildItem -LiteralPath $resolved -Recurse -Force)
    if ($children | Where-Object { $_.Attributes -band [IO.FileAttributes]::ReparsePoint }) { throw "Linked descendant: $resolved" }
    $bytes += ($children | Where-Object { !$_.PSIsContainer } | Measure-Object -Property Length -Sum).Sum
}
$plan = [ordered]@{kept_attempts=@($keep.Name); retained_acceptances=$retainedAcceptances;
    removed_paths=@($remove.FullName); removed_bytes=$bytes; applied=[bool]$Apply; completed=$false; retained_file_sha256=$hashes;
    preserves='Recent six attempts, their acceptances, self-contained regression corpus, infrastructure logs, all production code'}
if ($Apply) {
    $reportDir = Join-Path $root 'experiments/results'
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
    $report = Join-Path $reportDir ('retention-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json')
    $plan | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report -Encoding utf8
    foreach ($dir in $remove) { Remove-Item -LiteralPath $dir.FullName -Recurse -Force }
    foreach ($path in $hashes.Keys) {
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $hashes[$path]) { throw "Retained file changed: $path" }
    }
    if (@(Get-ChildItem -LiteralPath $attemptRoot -Directory).Count -ne 6) { throw 'Post-retention count mismatch' }
    $plan.completed = $true
    $plan | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $report -Encoding utf8
    Write-Output "Manifest: $report"
}
[pscustomobject]@{KeptAttempts=($keep.Name -join ','); DeletedAttempts=$all.Count-6;
    DeletedAcceptances=$remove.Count-($all.Count-6); ReclaimedMiB=[math]::Round($bytes/1MB,1); Applied=[bool]$Apply} | Format-List
