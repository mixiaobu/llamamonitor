# 等 CI 完成并验证 v1.3.3 Release（5 资产 + manifest version）
# 自读 PAT（Windows 凭据管理器 git:https://github.com），无交互。
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class CredWait133
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct CRED {
        public int dwFlags;
        public int Type;
        public IntPtr lpTargetName;
        public IntPtr lpComment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public int cbCredentialBlob;
        public IntPtr lpCredentialBlob;
        public int dwAttributes;
        public IntPtr TargetAlias;
    }
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CredReadW(string target, int type, int reserved, out IntPtr credPtr);
    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool CredFree(IntPtr p);
    public static string ReadGeneric(string target)
    {
        IntPtr p;
        if (!CredReadW(target, 1, 0, out p)) { return null; }
        try {
            var c = Marshal.PtrToStructure<CRED>(p);
            if (c.lpCredentialBlob == IntPtr.Zero) { return null; }
            return Marshal.PtrToStringUni(c.lpCredentialBlob, c.cbCredentialBlob / 2);
        } finally { CredFree(p); }
    }
}
"@
$pat = [CredWait133]::ReadGeneric("git:https://github.com")
if (-not $pat) { "NO PAT in credential manager"; exit 4 }
$h = @{ Authorization = "token $pat" }
$api = "https://api.github.com/repos/mixiaobu/llamamonitor"

# 找到 v1.3.3 tag 触发的 run
$runId = $null
for ($i = 0; $i -lt 12; $i++) {
    $runs = Invoke-RestMethod -Uri "$api/actions/runs?per_page=10" -Headers $h
    foreach ($r in $runs.workflow_runs) {
        if ($r.head_branch -eq "v1.3.3") { $runId = $r.id; break }
    }
    if ($runId) { break }
    "  ... no run yet (waiting $i)"
    Start-Sleep -Seconds 15
}
if (-not $runId) { "NO RUN FOUND for v1.3.3"; exit 4 }
"runId: $runId  url: $((Invoke-RestMethod -Uri "$api/actions/runs/$runId" -Headers $h).html_url)"

$deadline = (Get-Date).AddMinutes(50)
$r = $null
while ((Get-Date) -lt $deadline) {
    $r = Invoke-RestMethod -Uri "$api/actions/runs/$runId" -Headers $h
    # 当前 job 状态（进度提示）
    $jobs = Invoke-RestMethod -Uri "$api/actions/runs/$runId/jobs?per_page=10" -Headers $h
    $cur = ($jobs.jobs | ForEach-Object { "$($_.name):$($_.status)" }) -join ","
    "  ... run=$($r.status) [$cur]"
    if ($r.status -eq "completed") { break }
    Start-Sleep -Seconds 60
}
"CI status=$($r.status) conclusion=$($r.conclusion)"
if ($r.status -ne "completed") { "TIMEOUT"; exit 2 }
if ($r.conclusion -ne "success") { "CI FAILED: $($r.conclusion)"; exit 3 }

# 验证 Release（5 资产 + manifest version）
$rel = Invoke-RestMethod -Uri "$api/releases/tags/v1.3.3" -Headers $h
"release: tag=$($rel.tag_name) assets=" + $rel.assets.Count
$names = @($rel.assets | ForEach-Object { $_.name })
$expected = @(
  "LlamaMonitor-1.3.3-win-x64.zip",
  "LlamaMonitor-Setup-1.3.3-win-x64.exe",
  "release-manifest.json",
  "release-manifest.sig",
  "SHA256SUMS.txt"
)
$missing = $expected | Where-Object { $names -notcontains $_ }
"missing: [$($missing -join ', ')]"
$rel.assets | ForEach-Object { "  {0}  {1} bytes" -f $_.name, $_.size }

$man = Invoke-WebRequest -Uri "https://github.com/mixiaobu/llamamonitor/releases/download/v1.3.3/release-manifest.json" -Headers @{ Accept = "application/octet-stream" } -UseBasicParsing
$txt = if ($man.Content -is [byte[]]) { [System.Text.Encoding]::UTF8.GetString($man.Content) } else { $man.Content }
$manJson = $txt | ConvertFrom-Json
"manifest version: $($manJson.version)"

if ($missing.Count -eq 0 -and $manJson.version -eq "1.3.3") {
    "VERIFIED: v1.3.3 release OK (5 assets + manifest version=1.3.3)"
    exit 0
} else {
    "VERIFICATION FAILED"
    exit 1
}
