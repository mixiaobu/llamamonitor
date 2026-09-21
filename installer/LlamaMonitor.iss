; ============================================================================
; LlamaMonitor — Inno Setup 6 安装脚本（Phase 12）
;
; 构建方式（不要手工改 AppId）：
;     ISCC /DAppVersion=1.0.0 LlamaMonitor.iss     （由 scripts/build_release.py 调用）
;
; 设计要点（与 docs/RELEASE.md、docs/INSTALLER_TEST.md 对应）：
; - 固定 AppId：升级被识别为同一软件；**永久不要改**；
; - 默认安装到 {localappdata}\Programs\LlamaMonitor（per-user，无需管理员/UAC）；
; - 用户数据永远在 %LOCALAPPDATA%\LlamaMonitor（安装目录外）：
;   升级/卸载绝不触碰；卸载默认保留数据，仅当用户显式勾选
;   "Remove LlamaMonitor monitoring data..." 才删除该固定目录；
; - 自定义 database.path（如 D:\MyData\llama.db）卸载时**从不**自动删除
;   （卸载只删默认数据目录，不读 config 里的任意路径）；
; - 升级/卸载前：PrepareToInstall 先尝试 `LlamaMonitor.exe --shutdown-existing`
;   （Named Event Local\LlamaMonitor.Shutdown 请求优雅退出，等 Mutex 释放 ≤10s）；
;   仍在运行 -> Inno 内置 AppMutex + CloseApplications 提示（Retry/Cancel，
;   不 taskkill）；静默模式 -> 请求优雅退出，失败则 Abort（不覆盖运行中文件）；
; - 阻止降级：InitializeSetup 读已安装版本，已安装 > 当前 -> 拒绝；
; - 升级后若 autostart（HKCU Run "LlamaMonitor"）已启用：把命令更新为新
;   {app}\LlamaMonitor.exe（不新启用、不碰其他 Run 值）；
; - 不自动开启 Start with Windows（默认 Disabled，用户自行在 Settings 开启）；
; - Finish 页 "Launch LlamaMonitor" 默认勾选；/VERYSILENT 不启动（skipifsilent）；
; - 不安装 Python / NVIDIA 组件 / llama.cpp；不建防火墙规则；不注册文件关联；
; - 无 telemetry / analytics。
; ============================================================================

#ifndef AppVersion
  #error "必须传入版本: ISCC /DAppVersion=x.y.z LlamaMonitor.iss（版本唯一来源: version.py）"
#endif

#define AppName "LlamaMonitor"
#define AppPublisher "LlamaMonitor Project"
; 固定 AppId（2026-09-18 生成，永久不变 —— 改它 Windows 就认为是另一个软件）。
; 这里存"纯 GUID"；[Setup] 里用 {#AppIdBraced} 注入（带花括号）。
#define AppIdGuid "7E811DED-4947-495D-8F9C-1725CF459D43"
; 用预处理器拼出双花括号，让 Inno [Setup] 解析器把 GUID 当作字面值（{ -> {{）
#define AppIdBraced "{{" + AppIdGuid + "}}"
; Inno 卸载注册表键名固定为 "<AppId>_is1"（AppId 存储形态带花括号）。
; 实测（§87）：安装后真实键名为 {7E811DED-4947-495D-8F9C-1725CF459D43}}_is1
; （注意 AppId 尾部是双花括号 }}，再拼 _is1 后缀 —— 不能少）。
; [Code] 的降级检查 / GetOldInstallDir 必须读这个精确键（不能用 AppName 猜）。
; 注意：#define 值不做递归展开，所以这里写完整字面量（不能嵌套 {#UninstallKeyName}）。
#define UninstallRegKey "Software\Microsoft\Windows\CurrentVersion\Uninstall\{7E811DED-4947-495D-8F9C-1725CF459D43}}_is1"
#define RunRegKey "Software\Microsoft\Windows\CurrentVersion\Run"
#define SingleInstanceMutex "Local\LlamaMonitor.SingleInstance"

[Setup]
AppId={#AppIdBraced}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppComments=Local-only monitor for llama.cpp /metrics (Token / MTP / GPU statistics)
DefaultDirName={localappdata}\Programs\LlamaMonitor
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; per-user 安装：不请求管理员（应用不装驱动/服务、不写 HKLM）
PrivilegesRequired=lowest
; 注意：不设 PrivilegesRequiredOverridesAllowed=dialog —— 实测 Inno 6.7.3 在
; /SILENT 模式下**仍会弹出** "Select Setup Install Mode" 模态对话框并无限阻塞
; （会卡死更新器的静默安装）。应用设计就是 per-user（固定 %LOCALAPPDATA% 数据
; 目录、不写 HKLM），强制 per-user 是正确行为，无需让用户选 all-users。
; 只发布 Windows x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; 输出：release\LlamaMonitor-Setup-<ver>-win-x64.exe
OutputDir=..\release
OutputBaseFilename=LlamaMonitor-Setup-{#AppVersion}-win-x64
SetupIconFile=..\assets\LlamaMonitor.ico
UninstallDisplayIcon={app}\LlamaMonitor.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; 内置运行中检测：与应用 Named Mutex 对应（PrepareToInstall 会先尝试优雅退出）
AppMutex={#SingleInstanceMutex}
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 桌面快捷方式：默认**不勾选**（避免污染桌面，§20）
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
; 可选删除用户数据：默认**不勾选**（安全默认 = 保留数据，§34/§35）。
; Inno Setup 6（本项目固定的 6.7.3）无"仅卸载器显示"的任务旗标，
; 故该任务在安装向导与卸载向导都会出现、默认不勾选：
;   - 安装时勾它无副作用（只有卸载的 DeinitializeUninstall 会执行删除）；
;   - 卸载时用户可勾它删除固定数据目录；静默卸载用 /TASKS=removedata 选择。
Name: "removedata"; Description: "Remove LlamaMonitor monitoring data and settings (Token history, GPU history, configuration, backups, logs). This cannot be undone. Custom database locations are NOT affected."; GroupDescription: "User data:"; Flags: unchecked

[Files]
; dist\LlamaMonitor\* 完整安装（含 static / assets / PyInstaller 运行时依赖）。
; PyInstaller onedir 输出不含源码/tests/tools/用户数据（monitor.db、config.json、
; backups、logs 都在 %LOCALAPPDATA%\LlamaMonitor，永不进包）。
Source: "..\dist\LlamaMonitor\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; 开始菜单（固定组名 LlamaMonitor）+ 可选桌面快捷方式
Name: "{group}\{#AppName}"; Filename: "{app}\LlamaMonitor.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\LlamaMonitor.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Finish 页默认勾选 Launch；静默安装不启动（skipifsilent，§21/§55）
Filename: "{app}\LlamaMonitor.exe"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
; Phase 13：更新器触发的升级完成后自动启动新版（update_service 用 /SILENT /NORESTART
; /APPUPDATE[_BG] 启动安装器）。普通手工 /SILENT 安装不受影响（无 /APPUPDATE 参数，
; skipifsilent 条目照旧不启动；以下两条只在对应 Check 为真时出现在 Finish 页）。
Filename: "{app}\LlamaMonitor.exe"; Description: "Start LlamaMonitor after update"; Flags: nowait postinstall; Check: IsAppUpdateInstall
Filename: "{app}\LlamaMonitor.exe"; Parameters: "--background"; Description: "Start LlamaMonitor (background) after update"; Flags: nowait postinstall; Check: IsAppUpdateBg

[Code]
// 说明：本 [Code] 只用 Inno 内置函数（RegQueryStringValue / RegWriteStringValue /
// RegDeleteValue / Exec / DelTree / TaskSelected / VersionCodeFromString ...），
// 不声明 Win32 external —— 运行中检测与优雅退出全部交给
// `LlamaMonitor.exe --shutdown-existing`（其内部做 Mutex 所有权检测，
// 返回 0=无实例或已优雅退出 / 1=等待 10s 后仍在运行）。
const
  WEBVIEW2_CLIENT_KEY_64 = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WEBVIEW2_CLIENT_KEY_32 = 'Software\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

// Phase 13：更新器触发的升级自动启动判定（读安装器命令行 /APPUPDATE 参数）。
// update_service.install() 以 [exe, "/SILENT", "/NORESTART", "/APPUPDATE"]（前台）或
// "/APPUPDATE_BG"（后台 -> 新实例带 --background）启动本安装器。
// 取参用 **GetCmdTail 函数**（Inno 6.3+ 内置；实测 6.7.3 的 {cmdline}/{cmdtail}
// ExpandConstant 常量均抛 "Unknown constant" 运行时异常，绝不能用）。
// 注意：GetCmdTail 的返回内容包含 Inno 内部自举参数 /SL5="..." —— 匹配时用
// Pos() 子串搜索即可，/SL5 不会与 /APPUPDATE、/SILENT 等产生误匹配。
// [Run] 的 Check 只在安装向导求值（卸载器不执行 [Run]）。
function IsAppUpdateBg: Boolean;
var
  C: String;
begin
  C := GetCmdTail;
  Result := Pos('/APPUPDATE_BG', C) > 0;
end;

function IsAppUpdateInstall: Boolean;
var
  C: String;
begin
  C := GetCmdTail;
  Result := (Pos('/APPUPDATE', C) > 0) and (Pos('/APPUPDATE_BG', C) = 0);
end;

function GetOldInstallDir: String;
var
  S: String;
begin
  Result := '';
  if RegQueryStringValue(HKCU, '{#UninstallRegKey}', 'InstallLocation', S) and (S <> '') then
  begin
    Result := S;
    Exit;
  end;
  if RegQueryStringValue(HKLM, '{#UninstallRegKey}', 'InstallLocation', S) then
    Result := S;
end;

// 定位已安装 EXE（供 InitializeSetup/InitializeUninstall 用，两者 {app} 常量
// 在 InitializeSetup 阶段尚未初始化）。优先级：
//   1) 注册表 InstallLocation（权威位置，覆盖同目录 + 自定义目录升级）；
//   2) 默认目录 {localappdata}\Programs\LlamaMonitor（环境常量，早期可用）。
function GetInstallExePath: String;
var
  Dir: String;
begin
  Result := '';
  Dir := GetOldInstallDir;
  if Dir <> '' then
  begin
    // 注册表 InstallLocation 带尾部反斜杠，去掉避免拼接出双反斜杠
    while (Length(Dir) > 1) and (Dir[Length(Dir)] = '\') do
      Dir := Copy(Dir, 1, Length(Dir) - 1);
    if FileExists(Dir + '\LlamaMonitor.exe') then
    begin
      Result := Dir + '\LlamaMonitor.exe';
      Exit;
    end;
  end;
  Dir := ExpandConstant('{localappdata}\Programs\LlamaMonitor');
  if FileExists(Dir + '\LlamaMonitor.exe') then
    Result := Dir + '\LlamaMonitor.exe';
end;

function GracefulShutdownRunningInstance: Boolean;
var
  Exe: String;
  ResultCode: Integer;
begin
  // 返回 True = 发出请求后仍在运行；False = 无实例或已优雅退出（或无可信 EXE）。
  Result := False;
  Exe := GetInstallExePath;
  if Exe = '' then
    Exit; // 全新安装且无旧目录：无实例可请求
  // --shutdown-existing：发 Named Event 请求优雅退出；子进程内部做 Mutex 所有权
  // 检测并最多等 10s。返回 0=无实例/已退出，1=仍在运行。它不会启动新的应用实例。
  if Exec(Exe, '--shutdown-existing', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Log('--shutdown-existing [' + Exe + '] exited with code ' + IntToStr(ResultCode));
    Result := (ResultCode = 1);
  end
  else
    Log('Failed to launch --shutdown-existing (Exec error) for ' + Exe);
end;

// Inno [Code] 无内置版本比较：手动解析 "x.y.z" 逐段比较（数字，无 prerelease）。
function GetVersionInt(V: String; Part: Integer): Integer;
var
  I, Seen: Integer;
  C: Char;
  Cur: String;
begin
  Result := 0;
  Seen := 0;
  Cur := '';
  for I := 1 to Length(V) do
  begin
    C := V[I];
    if C = '.' then
    begin
      if Seen = Part then
      begin
        Result := StrToIntDef(Cur, 0);
        Exit;
      end;
      Inc(Seen);
      Cur := '';
    end
    else
      Cur := Cur + C;
  end;
  if Seen = Part then
    Result := StrToIntDef(Cur, 0);
end;

function InstalledIsNewer(Installed: String; New: String): Boolean;
var
  I, a, b: Integer;
begin
  Result := False;
  for I := 0 to 2 do
  begin
    a := GetVersionInt(Installed, I);
    b := GetVersionInt(New, I);
    if a > b then
    begin
      Result := True;
      Exit;
    end;
    if a < b then
    begin
      Result := False;
      Exit;
    end;
  end;
end;

// 静默检测（Inno [Code] 无 SilentMode 全局）：命令行含 /VERYSILENT 或 /SILENT。
// 用 GetCmdTail 函数（{cmdtail} 常量在 Inno 6.7.3 不可用，见 IsAppUpdateBg 注释）。
// 必须在 InitializeSetup 之前定义（Inno [Code] 单遍编译：先用后定义会报 Unknown identifier）。
function IsSilentInstall: Boolean;
var
  C: String;
begin
  C := GetCmdTail;
  Result := (Pos('/VERYSILENT', C) > 0) or (Pos('/SILENT', C) > 0);
end;

function InitializeSetup: Boolean;
var
  InstalledVersion: String;
  KeyPath: String;
  Found: Boolean;
begin
  Result := True;
  // 降级保护（§40）：已安装版本比当前 Setup 更新 -> 拒绝安装
  KeyPath := '{#UninstallRegKey}';
  Found := RegQueryStringValue(HKCU, KeyPath, 'DisplayVersion', InstalledVersion)
       or RegQueryStringValue(HKLM, KeyPath, 'DisplayVersion', InstalledVersion);
  if Found and InstalledIsNewer(InstalledVersion, '{#AppVersion}') then
  begin
    if IsSilentInstall then
    begin
      // 静默：不弹阻塞框（会卡住 /VERYSILENT），只记录原因并中止（§40/§55）
      Log('Downgrade blocked (silent): installed=' + InstalledVersion + ' > new={#AppVersion}. Aborting.');
    end
    else
      MsgBox('A newer version of LlamaMonitor (' + InstalledVersion + ') is already installed.' + #13#10 +
             'Installing an older version (' + '{#AppVersion}' + ') may be incompatible with the newer database schema.' + #13#10#13#10 +
             'Upgrade (or wait for a newer build) instead, or uninstall the current version first.',
             mbError, MB_OK);
    Result := False;
    Exit;
  end;
  // 升级前优雅退出运行中的实例（§23-§25）。**必须放在 InitializeSetup**：
  // Inno 内置 CloseApplications(AppMutex) 在 PrepareToInstall 之前触发（实测
  // 日志 0.005s 内 EAbort），放 PrepareToInstall 会来不及。这里尽力而为请求
  // 优雅退出（--shutdown-existing，等 Mutex 释放 ≤10s）：成功则后续
  // CloseApplications 看不到运行实例；失败则由 CloseApplications 兜底
  // （交互 Retry/Cancel，静默 Abort，均不 taskkill）。
  GracefulShutdownRunningInstance;
end;

function WebView2Present: Boolean;
begin
  // Evergreen WebView2 Runtime 的注册表标记（系统级 64/32 视图 + 当前用户）。
  // 以"键是否存在"判定：运行时安装即在 Clients\{GUID} 写键；版本值名随版本
  // 不同（新版为 pv、旧版为 ProductVersion），故不依赖具体值名（§60）。
  Result := RegKeyExists(HKLM64, WEBVIEW2_CLIENT_KEY_64)
         or RegKeyExists(HKLM, WEBVIEW2_CLIENT_KEY_64)
         or RegKeyExists(HKLM, WEBVIEW2_CLIENT_KEY_32)
         or RegKeyExists(HKCU, WEBVIEW2_CLIENT_KEY_64);
end;

procedure UpdateAutostartIfEnabled;
var
  Current: String;
  NewCmd: String;
begin
  // 升级后维护 autostart（§29-§31）：
  // - 只在值**已存在**时更新（安装不新启用 Start with Windows）；
  // - 只动 HKCU Run 的 "LlamaMonitor" 这一个值（§30 所有权）；
  // - 命令固定为 "<新EXE>" --background（与应用自身 Repair 行为一致）。
  if not RegQueryStringValue(HKCU, '{#RunRegKey}', 'LlamaMonitor', Current) then
    Exit;
  NewCmd := '"' + ExpandConstant('{app}\LlamaMonitor.exe') + '" --background';
  if Current <> NewCmd then
  begin
    if RegWriteStringValue(HKCU, '{#RunRegKey}', 'LlamaMonitor', NewCmd) then
      Log('Updated autostart entry to new install location: ' + NewCmd);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // 升级后修复 autostart 指向（安装目录变化时）
    UpdateAutostartIfEnabled;
    // WebView2 检测（§60/§61）：缺失只提示不拒绝（应用有默认浏览器回退，
    // 监控核心不受影响）；不静默联网下载。静默安装不弹框（检测非阻塞）。
    if (not WebView2Present) and (not IsSilentInstall) then
      MsgBox('Microsoft Edge WebView2 Runtime was not detected.' + #13#10 +
             'The dashboard window may fall back to your default browser;' + #13#10 +
             'core monitoring is not affected. Most Windows 11 systems already include it.',
             mbInformation, MB_OK);
  end;
end;

// 卸载器入口：卸载前优雅退出运行中的实例（§23-§25），确保文件不被占用。
// 尽力而为：失败时卸载器自身的运行中检测会兜底（提示关闭应用）。
function InitializeUninstall: Boolean;
begin
  Result := True;
  GracefulShutdownRunningInstance;
end;

procedure DeinitializeUninstall;
var
  DataDir: String;
  Sel: Boolean;
  DelOk: Boolean;
  Cmd: String;
begin
  // 1) 删除应用自己的 autostart 值（存在才删，幂等，§38）
  RegDeleteValue(HKCU, '{#RunRegKey}', 'LlamaMonitor');
  // 2) 用户显式勾选 "Remove data"：只删固定默认数据目录（§34-§37）。
  //    自定义 database.path 不在删除范围（不读 config、不递归任意路径）。
  //    注意：Inno 6 卸载器中不能调 WizardIsTaskSelected（会抛异常），
  //    改从命令行检测 removedata（静默 /TASKS=removedata；交互勾选后
  //    second phase 也会带该任务名）。
  Cmd := ExpandConstant('{param:TASKS|__NOPE__}');
  Sel := Pos('removedata', Cmd) > 0;
  if Sel then
  begin
    DataDir := ExpandConstant('{localappdata}\LlamaMonitor');
    DelOk := True;
    if DirExists(DataDir) then
      DelOk := DelTree(DataDir, True, True, True);
  end
  else
    DelOk := False;
  if Sel then
  begin
    if DelOk then
      Log('Removed default user data directory: ' + DataDir)
    else
      Log('WARNING: could not fully remove user data directory: ' + DataDir);
  end
  else
    Log('Uninstall: keeping user data (default).');
end;
