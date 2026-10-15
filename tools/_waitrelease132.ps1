# 等 CI 完成并验证 1.3.2 Release（5 资产 + manifest version）
# PAT 从环境变量读取：$env:GH_PAT="..." ; powershell -File tools\_waitrelease132.ps1
$pat = $env:GH_PAT
$h = @{ Authorization = "token $pat" }

# 找到 v1.3.2 tag 触发的 run
$runId = $null
for ($i = 0; $i -lt 12; $i++) {
  $runs = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/actions/runs?per_page=10" -Headers $h
  foreach ($r in $runs.workflow_runs) {
    if ($r.head_branch -eq "v1.3.2") { $runId = $r.id; break }
  }
  if ($runId) { break }
  Start-Sleep -Seconds 15
}
if (-not $runId) { "NO RUN FOUND for v1.3.2"; exit 4 }
"runId: $runId"

$deadline = (Get-Date).AddMinutes(50)
$r = $null
while ((Get-Date) -lt $deadline) {
  $r = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/actions/runs/$runId" -Headers $h
  if ($r.status -eq "completed") { break }
  "  ... status=$($r.status) (waiting)"
  Start-Sleep -Seconds 60
}
"CI status=$($r.status) conclusion=$($r.conclusion)"
if ($r.status -ne "completed") { "TIMEOUT"; exit 2 }
if ($r.conclusion -ne "success") { "CI FAILED: $($r.conclusion)"; exit 3 }

$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/releases/tags/v1.3.2" -Headers $h
"release: tag=$($rel.tag_name) assets=" + $rel.assets.Count
$names = @($rel.assets | ForEach-Object { $_.name })
$expected = @(
  "LlamaMonitor-1.3.2-win-x64.zip",
  "LlamaMonitor-Setup-1.3.2-win-x64.exe",
  "release-manifest.json",
  "release-manifest.sig",
  "SHA256SUMS.txt"
)
$missing = $expected | Where-Object { $names -notcontains $_ }
"missing: [$($missing -join ', ')]"
$rel.assets | ForEach-Object { "  {0}  {1} bytes" -f $_.name, $_.size }

# manifest version（字节流先解码成字符串再解析）
$man = Invoke-WebRequest -Uri "https://github.com/mixiaobu/llamamonitor/releases/download/v1.3.2/release-manifest.json" -Headers @{ Accept = "application/octet-stream" } -UseBasicParsing
$txt = if ($man.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($man.Content) } else { $man.Content }
$manJson = $txt | ConvertFrom-Json
"manifest version: $($manJson.version)"

if ($missing.Count -eq 0 -and $manJson.version -eq "1.3.2") {
  "VERIFIED: 1.3.2 release OK (5 assets + manifest version=1.3.2)"
  exit 0
} else {
  "VERIFICATION FAILED"
  exit 1
}
