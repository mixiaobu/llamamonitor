# Phase 14 审计规格 §113：连续 5 次完整测试运行（稳定性证据）。
# 每次运行都必须是 "Ran 396 tests ... OK"；任何一次失败 -> 脚本 exit 1。
$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
Set-Location $PSScriptRoot\..
$failures = 0
for ($i = 1; $i -le 5; $i++) {
    Write-Host "===== RUN $i/5  $(Get-Date -Format 'HH:mm:ss') ====="
    $out = python -m unittest discover -s tests 2>&1 | Out-String
    $tail = ($out -split "`n") | Select-Object -Last 4
    $tail | ForEach-Object { Write-Host "  $_" }
    if ($out -match "Ran 397 tests" -and $out -match "(?m)^\s*OK\s*$") {
        Write-Host "  RUN $i : PASS"
    } else {
        Write-Host "  RUN $i : FAIL"
        $failures++
    }
}
Write-Host "===== 5x RESULT: $([Math]::Max(0,5-$failures))/5 PASS ====="
if ($failures -gt 0) { exit 1 } else { exit 0 }
