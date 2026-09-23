$ErrorActionPreference = 'Continue'
Set-Location 'C:\Users\mixiaobu\Desktop\ai\LlamaMonitor'
$env:PYTHONIOENCODING = 'utf-8'
$py = Join-Path (Get-Location) '.venv-final\Scripts\python.exe'
$port = 9222
$name = 'dbg-theme-dark'
$w=1920; $h=1080; $dsf=1.0; $scheme='dark'
$ud = Join-Path $env:TEMP "lm-ui-$name"
Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*$ud*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Process 'msedge.exe' -ArgumentList @("--inprivate","--remote-debugging-port=$port","--user-data-dir=$ud","--window-size=$w,$h","about:blank")
$ws = $null
for ($t=0;$t -lt 30;$t++){ Start-Sleep -Milliseconds 500
  try { $r = Invoke-RestMethod "http://127.0.0.1:$port/json" -TimeoutSec 3
        $pg = ($r | Where-Object { $_.type -eq 'page' } | Select-Object -First 1)
        if ($pg -and $pg.webSocketDebuggerUrl) { $ws = $pg.webSocketDebuggerUrl } } catch {}
  if ($ws) { break }
}
Write-Output "ws=$ws"
if ($ws) {
  Write-Output "=== minimal theme probe ==="
  & $py (Join-Path (Get-Location) 'tools\cdp_ui_probe.py') $ws "http://127.0.0.1:8765" $w $h $dsf $scheme (Join-Path (Get-Location) 'tools\_probe_theme.js') 9 2>&1
}
Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*$ud*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Write-Output "=== done ==="
