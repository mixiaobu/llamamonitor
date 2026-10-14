# 等 CI 完成并验证 1.2.1 Release（5 资产 + manifest version）
# PAT 从环境变量读取（避免 push protection 命中硬编码密钥）：
#   $env:GH_PAT="..." ; powershell -File tools\_waitrelease121.ps1
$pat = $env:GH_PAT
$h = @{ Authorization = "token $pat" }
$runId = 38100984114
$deadline = (Get-Date).AddMinutes(65)

while ((Get-Date) -lt $deadline) {
  $r = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/actions/runs/$runId" -Headers $h
  if ($r.status -eq "completed") { break }
  Start-Sleep -Seconds 60
}
"CI status=$($r.status) conclusion=$($r.conclusion)"
if ($r.status -ne "completed") { "TIMEOUT - 未完成"; exit 2 }
if ($r.conclusion -ne "success") { "CI FAILED: $($r.conclusion)"; exit 3 }

# Release 资产
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/mixiaobu/llamamonitor/releases/tags/v1.2.1" -Headers $h
"release: tag=$($rel.tag_name) assets=" + $rel.assets.Count
$names = $rel.assets | ForEach-Object { $_.name }
$expected = @(
  "LlamaMonitor-1.2.1-win-x64.zip",
  "LlamaMonitor-Setup-1.2.1-win-x64.exe",
  "release-manifest.json",
  "release-manifest.sig",
  "SHA256SUMS.txt"
)
$missing = $expected | Where-Object { $names -notcontains $_ }
"missing: $($missing -join ', ')"
$rel.assets | ForEach-Object { "  {0}  {1} bytes" -f $_.name, $_.size }

# manifest version（走 downloads URL，Accept octet-stream）
$manUrl = "https://github.com/mixiaobu/llamamonitor/releases/download/v1.2.1/release-manifest.json"
$man = Invoke-WebRequest -Uri $manUrl -Headers @{ Accept = "application/octet-stream" } -UseBasicParsing
$manJson = $man.Content | ConvertFrom-Json
"manifest version: $($manJson.version)"

# SHA256SUMS 内容
$shaUrl = "https://github.com/mixiaobu/llamamonitor/releases/download/v1.2.1/SHA256SUMS.txt"
$sha = (Invoke-WebRequest -Uri $shaUrl -Headers @{ Accept = "application/octet-stream" } -UseBasicParsing).Content
"SHA256SUMS lines: " + ($sha -split "`n").Count

if ($r.conclusion -eq "success" -and $missing.Count -eq 0 -and $manJson.version -eq "1.2.1") {
  "VERIFIED: 1.2.1 release OK (5 assets + manifest version=1.2.1)"
  exit 0
} else {
  "VERIFICATION FAILED"
  exit 1
}
