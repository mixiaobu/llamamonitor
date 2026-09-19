# UI screenshot helper for Phase 15 visual regression.
# Usage: pwsh -File screenshot.ps1 -Name overview_dark
# Captures the visible LlamaMonitor window into docs/screenshots/<Name>.png
# Pure ASCII on purpose (Windows PowerShell 5.1 ANSI quirk, see perf_sample.ps1).
param(
  [Parameter(Mandatory=$true)][string]$Name,
  [int]$DelayMs = 500
)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class CapWin32 {
  [DllImport("user32.dll", CharSet=CharSet.Auto)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L,T,R,B; }
}
"@
$procs = Get-Process LlamaMonitor -ErrorAction SilentlyContinue
$found = $false
foreach ($p in $procs) {
  if ($p.MainWindowHandle -eq [IntPtr]::Zero) { continue }
  $sb = New-Object System.Text.StringBuilder 256
  [CapWin32]::GetWindowText($p.MainWindowHandle, $sb, 256) | Out-Null
  if ($sb.ToString() -ne "LlamaMonitor") { continue }
  $hwnd = $p.MainWindowHandle
  [void][CapWin32]::ShowWindow($hwnd, 9)   # SW_RESTORE
  [void][CapWin32]::SetForegroundWindow($hwnd)
  Start-Sleep -Milliseconds $DelayMs
  $rect = New-Object CapWin32+RECT
  [void][CapWin32]::GetWindowRect($hwnd, [ref]$rect)
  $w = $rect.R - $rect.L; $h = $rect.B - $rect.T
  if ($w -le 0 -or $h -le 0) { continue }
  $bmp = New-Object System.Drawing.Bitmap($w, $h)
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.CopyFromScreen($rect.L, $rect.T, 0, 0, (New-Object System.Drawing.Size($w, $h)))
  $dir = Join-Path (Join-Path $PSScriptRoot "..") "docs\screenshots"
  $dir = [System.IO.Path]::GetFullPath($dir)
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
  $out = Join-Path $dir "$Name.png"
  $bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
  $g.Dispose(); $bmp.Dispose()
  Write-Host "saved: $out (${w}x${h})"
  $found = $true
  break
}
if (-not $found) { Write-Host "LlamaMonitor window not found"; exit 1 }
