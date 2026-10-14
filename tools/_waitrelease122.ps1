# 等 CI 完成并验证 1.2.2 Release（5 资产 + manifest version）
# PAT 从环境变量读取（避免 push protection 命中硬编码密钥）：
#   $env:GH_PAT="..." ; powershell -File tools\_waitrelease122.ps1
$pat = $env:GH_PAT
$h = @{ Authorization = "token $pat" }
$runId = 38369565618
$deadline = (Get-Date).AddMinutes(50)

while ((Get-Date) -lt $deadline) {
  $r = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/actions/runs/$runId" -Headers $h
  if ($r.status -eq "completed") { break }
  Start-Sleep -Seconds 60
}
"CI status=$($r.status) conclusion=$($r.conclusion)"
if ($r.status -ne "completed") { "TIMEOUT"; exit 2 }
if ($r.conclusion -ne "success") { "CI FAILED: $($r.conclusion)"; exit 3 }

$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/releases/tags/v1.2.2" -Headers $h
"release: tag=$($rel.tag_name) assets=" + $rel.assets.Count
$names = @($rel.assets | ForEach-Object { $_.name })
$expected = @(
  "LlamaMonitor-1.2.2-win-x64.zip",
  "LlamaMonitor-Setup-1.2.2-win-x64.exe",
  "release-manifest.json",
  "release-manifest.sig",
  "SHA256SUMS.txt"
)
$missing = $expected | Where-Object { $names -notcontains $_ }
"missing: [$($missing -join ', ')]"
$rel.assets | ForEach-Object { "  {0}  {1} bytes" -f $_.name, $_.size }

# manifest version（字节流先解码成字符串再解析）
$man = Invoke-WebRequest -Uri "https://github.com/mixiaobu/llamamonitor/releases/download/v1.2.2/release-manifest.json" -Headers @{ Accept = "application/octet-stream" } -UseBasicParsing
$txt = if ($man.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($man.Content) } else { $man.Content }
$manJson = $txt | ConvertFrom-Json
"manifest version: $($manJson.version)"

if ($missing.Count -eq 0 -and $manJson.version -eq "1.2.2") {
  "VERIFIED: 1.2.2 release OK (5 assets + manifest version=1.2.2)"
  exit 0
} else {
  "VERIFICATION FAILED"
  exit 1
}
