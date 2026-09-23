$ErrorActionPreference = 'Continue'
Set-Location 'C:\Users\mixiaobu\Desktop\ai\LlamaMonitor'
$env:PYTHONIOENCODING = 'utf-8'
$py = Join-Path (Get-Location) '.venv-final\Scripts\python.exe'
$port = 9222
$base = 'http://127.0.0.1:8765'
$outdir = 'C:\Users\mixiaobu\AppData\Local\Temp\lm_final_ui'
if (Test-Path $outdir) { Remove-Item $outdir -Recurse -Force }
New-Item -ItemType Directory -Path $outdir | Out-Null
$outfile = Join-Path $outdir 'results.jsonl'

$scenarios = @(
  @('theme-dark',        1920,1080, 1.0,'dark',  '_probe_theme.js'),
  @('theme-light',       1920,1080, 1.0,'light', '_probe_theme.js'),
  @('theme-sys-dark',    1920,1080, 1.0,'dark',  '_probe_theme.js'),
  @('theme-sys-light',   1920,1080, 1.0,'light', '_probe_theme.js'),
  @('dpi-100',           1920,1080, 1.0,'dark',  '_probe_ui_layout.js'),
  @('dpi-125',           1920,1080, 1.25,'dark', '_probe_ui_layout.js'),
  @('dpi-150',           1920,1080, 1.5,'dark',  '_probe_ui_layout.js'),
  @('dpi-200',           1920,1080, 2.0,'dark',  '_probe_ui_layout.js'),
  @('res-1366x768',      1366, 768, 1.0,'dark',  '_probe_ui_layout.js'),
  @('res-1920x1080',     1920,1080, 1.0,'dark',  '_probe_ui_layout.js'),
  @('res-2560x1440',     2560,1440, 1.0,'dark',  '_probe_ui_layout.js'),
  @('res-3840x2160',     3840,2160, 1.0,'dark',  '_probe_ui_layout.js')
)

function Get-PageWs {
  for ($t=0; $t -lt 30; $t++) {
    Start-Sleep -Milliseconds 500
    try {
      $r = Invoke-RestMethod "http://127.0.0.1:$port/json" -TimeoutSec 3
      $pg = ($r | Where-Object { $_.type -eq 'page' } | Select-Object -First 1)
      if ($pg -and $pg.webSocketDebuggerUrl) { return $pg.webSocketDebuggerUrl }
    } catch {}
  }
  return $null
}

foreach ($s in $scenarios) {
  $name = $s[0]; $w = $s[1]; $h = $s[2]; $dsf = $s[3]; $scheme = $s[4]; $jsf = $s[5]
  $ud = Join-Path $env:TEMP "lm-ui-$name"
  Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*lm-ui-$name*" } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
  Start-Process 'msedge.exe' -ArgumentList @("--inprivate","--remote-debugging-port=$port","--user-data-dir=$ud","--window-size=$w,$h","about:blank")
  $ws = Get-PageWs
  if (-not $ws) {
    Add-Content $outfile "{`"scenario`":`"$name`",`"ui`":null,`"bad_http`":[],`"console`":[],`"error`":`"no page`"}"
    Write-Output "[$name] NO PAGE"
    Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*lm-ui-$name*" } | Stop-Process -Force -ErrorAction SilentlyContinue
    continue
  }
  $env:LM_UI_SCENARIO = $name
  $res = & $py (Join-Path (Get-Location) 'tools\cdp_ui_probe.py') $ws $base $w $h $dsf $scheme (Join-Path (Get-Location) "tools\$jsf") 10 2>&1 | Out-String
  Add-Content $outfile $res
  Write-Output "[$name] done"
  Get-Process msedge -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*lm-ui-$name*" } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 1000
}
Write-Output "=== UI PROBE DONE -> $outfile ==="
