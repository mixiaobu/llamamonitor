cd C:\Users\mixiaobu\Desktop\ai\LlamaMonitor
$dirs = @("artifacts\edge-ovr-r2","artifacts\gpu-r4-audit","artifacts\history-r5-audit","artifacts\overview-r6-audit","artifacts\ovr-audit","artifacts\ovr-r4","artifacts\perf-r5-audit","artifacts\r7_settings-audit","artifacts\r8_final-audit","artifacts\r8_mobile-audit","artifacts\sys-r3-audit")
$all = @()
foreach ($d in $dirs) { $all += Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue }
$all = $all | Where-Object { $_ -ne $null }
"===== files > 1MB (excluding browser-profile dirs) ====="
$all | Where-Object { $_.Length -gt 1MB -and $_.FullName -notmatch "\\_profile\\|\\_p2\\|\\_p3\\|\\_p4\\|\\profile-r2\\|\\profile\\|\\dbgprofile\\|\\edge-profile\\" } | Select-Object -First 20 | ForEach-Object { "{0,8:N1} KB  {1}" -f ($_.Length/1KB), $_.FullName.Replace("$PWD\", "") }
"===== curated artifacts size (exclude profile dirs + >1MB) ====="
$kept = $all | Where-Object { $_.FullName -notmatch "\\_profile\\|\\_p2\\|\\_p3\\|\\_p4\\|\\profile-r2\\|\\profile\\|\\dbgprofile\\|\\edge-profile\\" -and $_.Length -le 1MB }
$tot = 0
foreach ($f in $kept) { $tot += $f.Length }
"artifacts (curated): {0:N1} MB, {1} files" -f ($tot/1MB), $kept.Count
"===== untracked tools+scripts sizes ====="
$u = git status --short | Where-Object { $_ -match "^\?\?" } | ForEach-Object { ($_ -replace '^\?\? ','') } | Where-Object { $_ -match "^tools/|^scripts/" }
$tsz = 0
$big = @()
foreach ($p in $u) {
  if (Test-Path $p -PathType Container) {
    $fs = Get-ChildItem $p -Recurse -File
    $tsz += ($fs | Measure-Object Length -Sum).Sum
    $big += $fs
  } elseif (Test-Path $p) {
    $it = Get-Item $p
    $tsz += $it.Length
    $big += $it
  }
}
"tools+scripts untracked: {0:N1} MB" -f ($tsz/1MB)
"big tools/scripts files:"
$big | Where-Object { $_.Length -gt 500KB } | ForEach-Object { "{0,8:N1} KB  {1}" -f ($_.Length/1KB), $_.FullName.Replace("$PWD\", "") }
