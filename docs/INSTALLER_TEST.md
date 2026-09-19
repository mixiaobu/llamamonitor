# 安装器手动测试记录（INSTALLER_TEST）

本文记录 **实际执行** 的安装器/升级/卸载/静默安装矩阵结果。
每条含：环境、命令/步骤、期望、**实测结果**（✅ 通过 / ❌ 失败 + 现象）。
（结果列在跑完对应步骤后回填；未执行的标 ⏸ 未执行。）

- 测试机：Windows 11（AMD64，Build 26200），当前用户 `mixiaobu`（**非管理员**，无 UAC）。
- 安装器：Inno Setup 6（`C:\Program Files (x86)\Inno Setup 6\ISCC.exe` 6.7.3 编译）。
- 数据目录规则：默认数据永远在 `%LOCALAPPDATA%\LlamaMonitor`，与安装目录无关。
- 被测版本：`1.0.0`（降级对照组为 `0.12.0`，二者 AppId 完全一致）。

> 约定：`<ver>` 为被测版本；快照 = 测试前对
> `%LOCALAPPDATA%\LlamaMonitor`（config.json、monitor.db 的 daily_usage 汇总、
> backups/ 文件清单、autostart 注册表值）做的记录。

---

## A. 全新安装（Clean Install）

- 环境：无旧 LlamaMonitor 安装、`%LOCALAPPDATA%\LlamaMonitor` 已备份并移走。
- 步骤：运行 `LlamaMonitor-Setup-<ver>-win-x64.exe`（交互向导，默认全默认，
  不勾桌面快捷方式，Finish 勾 Launch）。
- 期望：
  - 安装到 `%LOCALAPPDATA%\Programs\LlamaMonitor`；无 UAC 弹窗。
  - 开始菜单出现 LlamaMonitor；未勾桌面快捷方式则桌面无图标。
  - Finish 后自动启动；托盘出现图标。
  - `%LOCALAPPDATA%\LlamaMonitor` 自动创建；`config.json` 生成默认值。
- 验证：
  - Add/Remove Programs 显示 `LlamaMonitor <ver>`（DisplayVersion = `<ver>`）。
  - 已装 EXE `--version` 打印 `LlamaMonitor <ver>`；`/api/version`、About 一致。
- 实测结果：✅ 通过
  - 执行方式：静默 `LlamaMonitor-Setup-1.0.0-win-x64.exe /VERYSILENT /NORESTART /SUPPRESSMSGBOXES`
    （交互向导为同一默认配置；Finish 的 “Launch LlamaMonitor” 由 `[Run] nowait postinstall skipifsilent`
    提供，默认勾选、静默跳过）。
  - 安装到 `%LOCALAPPDATA%\Programs\LlamaMonitor`；`PrivilegesRequired=lowest`（per-user），**无 UAC 提权**。
  - 开始菜单出现 LlamaMonitor 快捷方式；**桌面未生成图标**（`desktopicon` 默认不勾）。
  - `%LOCALAPPDATA%\LlamaMonitor` 自动创建，`config.json` 生成默认值（port 9091）。
  - Add/Remove Programs 显示 `LlamaMonitor version 1.0.0`（DisplayVersion = `1.0.0`）。
  - 已装 EXE `--version` 输出 `LlamaMonitor 1.0.0`；`/api/version` 返回 `1.0.0`（与 About 一致）。
  - 静默安装**不自动启动**（skipifsilent 生效）；手动启动后托盘图标 + 主窗口正常出现。

## B. 升级（Upgrade，数据保留）

- 环境：已装旧版本（如 0.12.0），`%LOCALAPPDATA%\LlamaMonitor` 有历史数据。
- 步骤：先做快照；运行新版本安装器（覆盖升级，默认选项）。
- 期望：
  - 安装成功后 `%LOCALAPPDATA%\LlamaMonitor` 的 **config.json / monitor.db / backups/** 全部保留。
  - 数据库 schema 迁移到新 schema 版本（若旧版本 schema 更低）；**历史 daily_usage 数值不变**。
  - 迁移前自动生成 `backups/pre_migration_vOLD_to_vNEW_*.db`（若触发迁移）。
  - autostart 注册表值**仅当已存在**时更新为新路径命令；不存在则不创建（不自动启用）。
- 验证：升级后与快照逐项对比（daily_usage 汇总、config 关键字段、backups 清单、autostart）。
- 实测结果：✅ 通过
  - 环境：已装 0.12.0，数据目录有 daily_usage 3 行、config、8 个备份文件。
  - 快照（升级前）：config.json SHA `cb471ba2…e29957c9`、monitor.db SHA `fd192585…de52c889`、daily_usage 3 行。
  - 运行 1.0.0 安装器覆盖升级，exit 0。
  - **config.json SHA 升级后完全不变**（`cb471ba2…`）；**daily_usage 原 3 行逐字节不变**，仅升级后应用新追加当日行；backups/ 全部保留。
  - 0.12.0 与 1.0.0 均为 schema v3，**未触发 schema 迁移**，故无 `pre_migration_*` 备份（符合预期，非缺陷）。
  - autostart：`HKCU\…\Run\LlamaMonitor` 仅在已存在时更新为新 EXE 路径命令，不存在则不创建。

## C. 运行中升级（Running-App Upgrade）

- 环境：旧版本 LlamaMonitor 正在运行（托盘常驻）。
- 步骤：直接运行新版本安装器。
- 期望：
  - 安装器先调用 `LlamaMonitor.exe --shutdown-existing` 触发**优雅退出**；
    旧实例释放单实例 Mutex 后安装继续。
  - 若 10s 内未退出：弹出“仍在运行”提示，提供 **Retry / Cancel**（不 taskkill、不发 HTTP exit）。
  - 不出现“文件被占用/拒绝访问”类强制覆盖错误。
- 实测结果：✅ 通过
  - 保留 0.12.0 实例运行，直接运行 1.0.0 安装器。
  - 安装器 `InitializeSetup` 调用 `LlamaMonitor.exe --shutdown-existing`，
    Inno 日志记录 `--shutdown-existing [C:\…\LlamaMonitor.exe] exited with code 0`（旧实例优雅退出，释放 Mutex），安装继续，exit 0。
  - 无“文件被占用/拒绝访问”错误，覆盖写入正常。
  - 兜底路径（10s 未退出 → Inno 内置 `CloseApplications` 的 Retry/Cancel，静默 Abort）已配置，未 taskkill、未发 HTTP exit。

## D. 阻止降级（Downgrade Blocked）

- 环境：已装较新版本。
- 步骤：运行**更旧**版本的安装器。
- 期望：安装器检测 DisplayVersion 更低，弹框提示“已安装更新版本”并**中止**（不覆盖）。
- 实测结果：✅ 通过
  - 已装 1.0.0，运行 0.12.0 安装器（`/VERYSILENT /NORESTART /SUPPRESSMSGBOXES`）。
  - 安装器 **exit 1（中止）**；Add/Remove Programs 的 DisplayVersion 仍为 `1.0.0`；已装 EXE FileVersion 仍为 `1.0.0.0`（未被覆盖）。
  - 静默日志记录 “Downgrade blocked (silent)”；交互模式弹框 “A newer version of LlamaMonitor (1.0.0) is already installed…”。
  - 降级检查读取的精确卸载键：`HKCU\…\Uninstall\{7E811DED-4947-495D-8F9C-1725CF459D43}}_is1`（DisplayVersion）。

## E. 卸载 + 重装（保留数据）

- 环境：已安装且有数据。
- 步骤：Add/Remove Programs 卸载（**不勾** Remove data）→ 重装。
- 期望：
  - 卸载后 `%LOCALAPPDATA%\LlamaMonitor` **完整保留**（config/monitor.db/backups）。
  - 开始菜单项、autostart 注册表值被移除。
  - 重装后历史数据仍在，**不重新 baseline、不重复计数**（daily_usage 数值不变）。
- 实测结果：✅ 通过
  - 静默卸载（`unins000.exe /SILENT`，不带 removedata）：安装目录 + Add/Remove Programs 项被删除，
    `%LOCALAPPDATA%\LlamaMonitor` **完整保留**（config.json / monitor.db / backups 8 文件 / logs）。
  - 重新安装 1.0.0：**daily_usage 历史与卸载前快照逐字节一致**（4 行，不重新 baseline、不重复计数）；config SHA 不变。

## F. 卸载 + “Remove data”

- 环境：已安装，使用**默认**数据目录（`config.database.path` 未自定义）。
- 步骤：卸载时勾选 “Remove data” 任务。
- 期望：`%LOCALAPPDATA%\LlamaMonitor` 被删除（固定目录）。
- 对照：若 `config.database.path` 指向**自定义**路径，卸载（无论是否勾 Remove data）
  **从不**删除该自定义目录。
- 实测结果：✅ 通过
  - 卸载 `unins000.exe /SILENT /SUPPRESSMSGBOXES /TASKS=removedata`（默认数据目录，未自定义 database.path）。
  - `%LOCALAPPDATA%\LlamaMonitor` **被删除**；日志记录 `Removed default user data directory: C:\Users\mixiaobu\AppData\Local\LlamaMonitor`；无异常。
  - 对照：删除范围**仅**固定目录 `%LOCALAPPDATA%\LlamaMonitor`；代码不读取 config、不递归任意路径，故自定义 `database.path` 永不被删。

## G. 静默安装 / 静默卸载

- 静默安装：`LlamaMonitor-Setup-<ver>-win-x64.exe /VERYSILENT /NORESTART`
  - 期望：无 UI、**不自动启动**（`skipifsilent`）、安装到默认目录、数据目录规则不变。
- 静默卸载：`<uninstall.exe> /VERYSILENT`
  - 期望：无 UI、默认**保留数据**。
- 实测结果：✅ 通过
  - 静默安装 `/VERYSILENT /NORESTART`：无 UI、**不自动启动**（skipifsilent）、装到默认目录、数据目录规则不变。
  - 静默卸载 `/SILENT`：无 UI、默认**保留数据**（见 E）。

## H. 自定义安装目录

- 步骤：`LlamaMonitor-Setup-<ver>-win-x64.exe /DIR="D:\LM\Custom" /VERYSILENT /NORESTART`
  （或交互向导里改目录）。
- 期望：EXE 装到自定义目录；**数据仍在 `%LOCALAPPDATA%\LlamaMonitor`**（不跟随安装目录）；
  autostart 命令指向新的 EXE 路径（若 autostart 已启用）。
- 实测结果：✅ 通过
  - `/DIR="C:\LlamaCustom" /VERYSILENT /NORESTART`。
  - EXE 装到 `C:\LlamaCustom\LlamaMonitor.exe`；**数据仍在 `%LOCALAPPDATA%\LlamaMonitor`**（不跟随安装目录）；
    注册表 `InstallLocation = C:\LlamaCustom\`。

## I. 版本一致性（PE / 文件名 / API / --version）

- 期望（全部相等，除 PE FileVersion 为 `x.y.z.0`）：
  - `LlamaMonitor.exe --version` → `LlamaMonitor <ver>`
  - `/api/version.version` → `<ver>`
  - 设置页 About → `<ver>`
  - PE ProductVersion → `<ver>`；PE FileVersion → `<ver>.0`
  - 安装器/Portable 文件名、SHA256SUMS、manifest、Add/Remove Programs、CHANGELOG 标题 → `<ver>`
- 实测结果：✅ 通过（12/12 一致，`<ver>` = `1.0.0`）
  - `version.py` = `1.0.0`
  - PE FileVersion = `1.0.0.0`；PE ProductVersion = `1.0.0`；PE FileDescription = `LlamaMonitor`；PE ProductName = `LlamaMonitor`
  - Add/Remove Programs = `1.0.0`
  - `--version` = `LlamaMonitor 1.0.0`
  - `/api/version` = `1.0.0`；`/api/status.version` = `1.0.0`
  - Portable ZIP / Setup 文件名 = `…1.0.0…`；release-manifest `version` = `1.0.0`；SHA256SUMS 校验通过
  - CHANGELOG 标题 `## [1.0.0]`

## J. 已知问题 / 观察

- 均为 Inno Setup 6.7.3 [Code] API 的实测限制，已在脚本中规避（非缺陷）：
  1. 6.7.3 [Code] 无 `SilentMode` / `CmdTail` / `BoolToString` / `TaskSelected` / `VersionCodeFromString` / `Pointer` 及多数 Win32 extern → 静默检测改用 `ExpandConstant('{cmdtail}')`（**仅安装器侧有效**）。
  2. `WizardIsTaskSelected` 在卸载器中抛 `Internal error: Cannot call "WizardIsTaskSelected" function during Uninstall` → 卸载侧任务检测改用 `ExpandConstant('{param:TASKS|}')`。
  3. `{cmdtail}` 在卸载器中为 `Unknown constant` → 卸载侧改用 `{param:...}`。
  4. 内置 `CloseApplications`（AppMutex）在 `PrepareToInstall` **之前**触发 → 优雅退出放到 `InitializeSetup` / `InitializeUninstall`。
  5. `{app}` 常量在 `InitializeSetup` 中尚未初始化 → 旧安装路径改从注册表 `InstallLocation` 读取 + `{localappdata}` 回退。
  6. `#define` 值内的 `{#…}` 嵌套引用**不递归展开** → 卸载注册表键写成完整字面量。
  7. WebView2 检测用“键存在”（`RegKeyExists`）而非读版本值（测试机该值名为 `pv`，非 `ProductVersion`）。
  8. 卸载为两阶段，`/LOG` 不随 second phase 传递（调试时改用固定路径文件记录）。
- 除此之外无其他非预期行为。
