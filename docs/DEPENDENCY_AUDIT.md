# 依赖审计（Phase 14，§47-§50）

日期：2026-09-19。构建环境：Python 3.13.14 / Windows 11 x64。

## 1. 生产依赖（requirements.txt，全部固定版本，全部在使用）

| 包 | 版本 | 用途 | 必要性 |
|---|---|---|---|
| fastapi | 0.141.1 | API 层（路由/依赖注入/校验） | 必要 |
| uvicorn | 0.53.0 | ASGI 服务器（内嵌事件循环） | 必要 |
| httpx | 0.28.1 | 出站 HTTP：llama /metrics（keep-alive 复用）+ GitHub 更新 | 必要 |
| pywebview | 6.2.1 | Dashboard 窗口（WebView2） | 必要 |
| pystray | 0.19.5 | 系统托盘 | 必要 |
| Pillow | 12.3.0 | 托盘图标加载/缩放 | 必要（pystray 依赖） |
| cryptography | 50.0.1 | Ed25519 更新验签（Phase 13，不自实现密码学） | 必要 |

开发/构建：pyinstaller 6.22.3（requirements-dev.txt；GPLv2+特殊例外，仅构建期）。

**未使用的依赖：无。**（每个包都有明确的运行时/构建期调用点；传递依赖
starlette/pydantic/httpcore/anyio/cffi/certifi 等由上述包声明，未手工引入。）

## 2. 漏洞审计（pip-audit，在线 PyPI 漏洞库）

- 工具：`pip-audit`（临时 venv，**不进 production 依赖**）；
- 命令：`pip-audit -r requirements.txt`（直接 + 传递依赖）；
- 结果：**No known vulnerabilities found**（7 直接 + 全部传递依赖，
  含 fastapi/starlette/pydantic 0day 高发区、cryptography、Pillow、httpx）。
- 在线漏洞数据库可达（经系统代理 127.0.0.1:10808）——**本次在线检查已完成**，
  非"未验证"状态。

## 3. 版本升级政策（§48）

Phase 14 期间**未升级任何包**（保持 1.0.0 构建/验证环境实际版本）。
requirements.txt 顶部注释声明：升级版本需重跑测试 + Release 流程。

## 4. 许可证（§50）

THIRD_PARTY_NOTICES.txt 已按**已安装 wheel 的 dist-info 元数据 + 捆绑 license 文件**
重新生成（逐项核对，不再保留 VERIFY 猜测项）：

- 宽松许可：MIT（fastapi、pydantic 系、anyio、h11、cffi、pycparser、pythonnet、
  clr_loader、bottle、six、pefile、packaging、setuptools、altgraph、
  pyinstaller-hooks-contrib、proxy_tools）；
  BSD-3（uvicorn、httpx、httpcore、starlette、pywebview、pywin32-ctypes、idna）；
  Apache-2.0 或 BSD-3 双许可（cryptography、packaging）；
  MPL-2.0（certifi）；PSF（typing_extensions）；Pillow License（Pillow）；
  Apache-2.0（ECharts vendored）。
- 唯一非宽松：**pystray = LGPL-3.0**（仅 import 使用、未修改源码、独立进程
  调用 Win32 API；NOTICES 中记录了上游仓库与版本）。
- ECharts 为本地 vendored（不经过 CDN，离线可用）——Apache-2.0。

## 5. 审计发现

| ID | 级别 | 问题 | 修复 |
|---|---|---|---|
| AUDIT-BUILD-001 | MEDIUM | requirements.txt / requirements-dev.txt 为**无 BOM 的 UTF-8 且含中文注释**：在 GBK 区域（本机即是）的干净 venv 中 `pip install -r requirements.txt`（以及 pip-audit 解析）会 UnicodeDecodeError（cp936）——§106 干净 venv 构建在 GBK 区域机器上会失败 | 两个 requirements 文件已加 UTF-8 BOM（pip 原生支持 BOM），已验证 GBK 区域 pip-audit 解析通过 |

## 6. 构建可重复性（§105）

- 相同源码/版本两次 build：PyInstaller onedir 输出文件集合一致（依赖版本
  固定 + `--collect` 规则固定）；不要求 byte-for-byte（时间戳/PE 时间戳除外）；
- 记录的工具版本：Python 3.13.14、PyInstaller 6.22.3、Inno Setup 6.7.3、
  pip 26.1.2（环境 pip）。
