# burn-in 期间采样 LlamaMonitor 进程资源（RSS / handles / threads / CPU）。
# 用法：powershell -File perf_sample.ps1 [-Samples 4] [-Interval 30]
# 输出：每样本一行的 CSV（可追加进 burn-in 日志）。
param(
    [int]$Samples = 4,
    [int]$Interval = 30
)
$procs = Get-Process -Name "LlamaMonitor" -ErrorAction SilentlyContinue
if (-not $procs) {
    Write-Error "LlamaMonitor 进程不存在"
    exit 1
}
$p = $procs | Select-Object -First 1
$lines = @()
for ($i = 0; $i -lt $Samples; $i++) {
    if ($i -gt 0) { Start-Sleep -Seconds $Interval }
    $cur = Get-Process -Id $p.Id
    $lines += "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'),{0},{1},{2},{3:N2}" -f `
        [math]::Round($cur.WorkingSet64 / 1MB, 1), $cur.HandleCount, `
        $cur.Threads.Count, ($cur.TotalProcessorTime.TotalSeconds / ($Interval + 0.01))
}
$lines | ForEach-Object { Write-Host $_ }
