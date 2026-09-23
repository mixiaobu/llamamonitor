$ErrorActionPreference='Continue'
$la = $env:LOCALAPPDATA
$db = Join-Path $la 'LlamaMonitor\monitor.db'
$logdir = Join-Path $la 'LlamaMonitor\logs'
$samples = Join-Path $env:TEMP 'lm_burnin_samples.txt'

function ProcMetrics($tag){
  $owner = (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1).OwningProcess
  $p = Get-Process -Id $owner -ErrorAction SilentlyContinue
  $rss = [math]::Round($p.WorkingSet64/1MB,1)
  $threads = $p.Threads.Count
  $handles = $p.HandleCount
  # DB
  $dbSize = if(Test-Path $db){[math]::Round((Get-Item $db).Length/1KB,1)}else{0}
  $walSize = if(Test-Path "$db-wal"){[math]::Round((Get-Item "$db-wal").Length/1KB,1)}else{0}
  $logSize = 0
  if(Test-Path $logdir){ $logSize = [math]::Round(((Get-ChildItem $logdir -Filter *.log -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum/1KB),1) }
  # nvidia-smi for LlamaMonitor-relevant GPU (whole-GPU snapshot)
  $gpu = ''
  try {
    $smi = & nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu --format=csv,noheader,nounits -ErrorAction SilentlyContinue
    $gpu = ($smi -join ' | ')
  } catch { $gpu = 'nvidia-smi n/a' }
  $up = (try { (Invoke-RestMethod "http://127.0.0.1:8765/api/status" -TimeoutSec 4).application.uptime_seconds } catch { '?' })
  Write-Output ("[{0}] T={1} pid={2} rss_MB={3} threads={4} handles={5} db_KB={6} wal_KB={7} log_KB={8} uptime_s={9} gpu=[{10}]" -f $tag, (Get-Date -Format 'HH:mm:ss'), $owner, $rss, $threads, $handles, $dbSize, $walSize, $logSize, $up, $gpu)
}

Write-Output "===== T=0 BASELINE (before burn-in) ====="
ProcMetrics 'T0'
# 也写一份到 samples 供 checkpoint 对比
$line = ProcMetrics 'T0-log'
Add-Content -Path $samples -Value $line -Encoding utf8
Write-Output "=== baseline written ==="
