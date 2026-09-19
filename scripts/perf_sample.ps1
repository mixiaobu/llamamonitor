# burn-in resource sampler for the LlamaMonitor process.
# Usage: powershell -File perf_sample.ps1 [-Samples 4] [-Interval 30]
# Output: one CSV line per sample: time, RSS_MB, handles, threads, CPU_s_per_interval.
# NOTE: keep this file pure ASCII (Windows PowerShell 5.1 reads BOM-less .ps1 as ANSI).
param(
    [int]$Samples = 4,
    [int]$Interval = 30
)
$procs = Get-Process -Name "LlamaMonitor" -ErrorAction SilentlyContinue
if (-not $procs) {
    Write-Error "LlamaMonitor process not found"
    exit 1
}
$p = $procs | Select-Object -First 1
$lines = @()
for ($i = 0; $i -lt $Samples; $i++) {
    if ($i -gt 0) { Start-Sleep -Seconds $Interval }
    $cur = Get-Process -Id $p.Id
    $cpu = $cur.TotalProcessorTime.TotalSeconds / ($Interval + 0.01)
    $lines += ("{0},{1},{2},{3},{4:N2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), [math]::Round($cur.WorkingSet64 / 1MB, 1), $cur.HandleCount, $cur.Threads.Count, $cpu)
}
$lines | ForEach-Object { Write-Host $_ }
