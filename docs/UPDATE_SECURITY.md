# 安全更新：信任模型与签名（UPDATE_SECURITY）

LlamaMonitor 的**应用内更新**（安装版从 GitHub Release 拉取新安装器）必须回答一个问题：
**用户凭什么相信下载到的安装器就是官方、没被篡改的？** 本文说明信任链、密钥管理、
签名/验签流程、密钥轮换，以及私钥泄漏时的响应步骤。

> 配套阅读：`docs/RELEASE.md`（构建/发布操作手册）、README 的"安全更新"章节（用户视角）。
> 本文面向**维护者**；所有密钥值以仓库 `update_keys.py` 与本地 `update-keys/` 为准。

## 0. 威胁模型（保护什么 / 不保护什么）

**保护**（更新信任链）：
- 安装器 / 便携 ZIP 的**完整性**与**真实性**：下载到的文件哈希必须与**已签名**的
  manifest 一致，且 manifest 本身必须能用**内置受信公钥**验证的 Ed25519 签名通过。
- 任一环节被篡改（GitHub Release 资产被替换、manifest 被改、签名被改、资产被注入）
  → 更新子系统拒绝，**绝不**进入 `READY_TO_INSTALL`，**绝不**执行安装器。

**明确不保护**（边界，别高估）：
- **Authenticode / SmartScreen**：本签名**不是** Windows 代码签名。安装器仍是"未签名
  的 EXE"，Windows SmartScreen 对首次运行/陌生发行者照常提示。SmartScreen 的
  " Reputation " 由 Authenticode + 下载量决定，与本文的 Ed25519 签名无关。
- **GitHub 发现环节的 MITM**：更新从 `api.github.com`（HTTPS + 系统证书库）发现
  Release；对 **GitHub 本身**的信任建立在 TLS 之上。本文签名保护的是"GitHub 上的资产
  内容"，不保护"GitHub 是否被攻破"。
- **无自动回滚**：更新是单向的（`0.13.0 → 0.13.1`）。若新版异常，用户通过
  `%LOCALAPPDATA%\LlamaMonitor\backups` 的 pre-update 快照手动恢复数据，**程序不会
  自动回滚到旧版**。
- 增量/差分下载、后台静默自动更新（当前更新需用户在 Settings→Updates 手动触发）。

## 1. 信任链（端到端）

```
┌────────────────────────────────────────────────────────────────────┐
│ LlamaMonitor.exe（安装后运行程序）                                  │
│   内置：update_keys.TRUSTED_UPDATE_KEYS（key_id -> Ed25519 公钥）   │
│        （公钥可公开，硬编码进源码/EXE）                              │
└───────────────▲────────────────────────────────────────────────────┘
                │ ③ 用 key_id 选公钥，对 manifest 原始 bytes 验签
┌───────────────┴────────────────────────────────────────────────────┐
│ release-manifest.sig（GitHub Release 资产，JSON ASCII sidecar）     │
│   {"algorithm":"Ed25519","key_id":"...","signature":"<Base64>"}     │
└───────────────▲────────────────────────────────────────────────────┘
                │ ② Ed25519 签名对象 = manifest 的 canonical bytes
┌───────────────┴────────────────────────────────────────────────────┐
│ release-manifest.json（GitHub Release 资产，schema 1）              │
│   installer{name,sha256,size} + portable{...} + version + ...       │
└───────────────▲────────────────────────────────────────────────────┘
                │ ④ 下载后逐项比对 size + sha256（流式）
┌───────────────┴────────────────────────────────────────────────────┐
│ 安装器 / 便携 ZIP（GitHub Release 资产，从固定受信仓库下载）         │
└────────────────────────────────────────────────────────────────────┘
```

**顺序不可颠倒**：先 ③ 验签（对原始 bytes），通过后**才** ② 解析 JSON、④ 校验字段、
比对下载哈希。签名不过 = 直接拒绝，连 JSON 都不解析（避免"恶意 manifest 先污染解析"）。

关键不变量（实现保证）：
- **写盘字节 == 被签名字节**：manifest 以 canonical bytes 落盘
  （`ensure_ascii=False, sort_keys=True, separators=(",",":")`, UTF-8），签名就签这串
  bytes；验签用**磁盘上的原始 bytes**，不重新序列化（`update_manifest.py`）。
- **受信仓库是代码常量**：下载目标仓库来自代码常量 `UPDATE_REPOSITORY`，**不**来自
  manifest / config / 用户输入（防"manifest 指到攻击者的仓库"）。
- **文件名白名单**：安装器/ZIP 的文件名由版本推导并校验（防路径穿越/任意名）。
- **loopback-only**：`/api/update/*` 只有 `127.0.0.1` 能触发（server 层 `_require_loopback`）。

## 2. 为什么是 Ed25519 + sidecar，而不是 RSA / Authenticode

| 维度 | 选择 | 理由 |
|---|---|---|
| 算法 | **Ed25519** | 短公钥(32B)/短签名(64B)、无填充歧义（不像 RSA-PKCS#1 有编码陷阱）、
  `cryptography` 库原生支持、验证恒定时间。更新 manifest 是小 JSON，Ed25519 足够。 |
| 密码学实现 | **`cryptography` 库** | **不自实现任何算法**。签名/验签都走 `cryptography.hazmat`。 |
| 签名载体 | **sidecar 文件 `release-manifest.sig`** | GitHub Release 资产是"文件"，把签名作为
  独立资产上传最自然；sidecar 含 `algorithm`+`key_id`，多密钥可轮换。 |
| 不选 RSA | — | 签名/公钥更大，PKCS#1 填充错误历史多；对本场景无收益。 |
| 不选 Authenticode | — | 需要证书+时间戳+（可选）付费 CA；SmartScreen 是**另一层**信任，
  与本信任链正交（见 §0）。两者可共存，但本阶段只做 manifest 级 Ed25519。 |

## 3. 公钥（可公开）

- 位置：`update_keys.py` → `TRUSTED_UPDATE_KEYS: dict[str, str]`，`key_id -> Base64(32 字节 Ed25519 原始公钥)`。
- 当前受信公钥：

  | key_id | 用途 | 公钥（Base64，32B 原始） |
  |---|---|---|
  | `key-2026-09` | 当前生产签名 | `BLzHjwHvDlNyo9BadmEjlcMV8nNJ1pcpckemBfxCv98=` |

- 默认签名 key：`update_keys.DEFAULT_KEY_ID = "key-2026-09"`（构建脚本缺省值）。
- **多公钥表**：`TRUSTED_UPDATE_KEYS` 支持**同时**内置多把受信公钥（轮换窗口期新旧并存）。
  `.sig` 里的 `key_id` 决定用哪把公钥验证；旧客户端只信任它内置的公钥。
- 公钥**可以**进源码/EXE/Repo/文档；私钥**绝不**（见 §4）。

## 4. 私钥（绝不公开）

- 位置（默认，项目目录**之外**）：
  `%LOCALAPPDATA%\LlamaMonitor\update-keys\private_key_<key-id>.pem`
  （PEM，`cryptography` 序列化；当前 `key-2026-09` 对应
  `private_key_key-2026-09.pem`）。
- **私钥绝不进入**：源码树、EXE/ZIP、GitHub Repo、Release 资产、`config.json`、
  日志、构建产物。只在**开发者离线环境**或 **GitHub Actions Secret** 里出现。
- 构建时通过**环境变量**提供（`generate_checksums.load_env_private_key`），不落盘到项目内：

  | 环境变量 | 含义 |
  |---|---|
  | `LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE` | 私钥 PEM **文件路径**（推荐；CI 把 Secret 解码到临时文件后设它） |
  | `LLAMAMONITOR_UPDATE_PRIVATE_KEY` | 私钥 PEM 的 **Base64**（二选一） |
  | `LLAMAMONITOR_UPDATE_KEY_ID` | 用哪把 key 签名（缺省 = `DEFAULT_KEY_ID`） |

  正式构建**必须**提供私钥（缺了构建失败）；开发构建可用 `--unsigned-development-build`
  跳过 `.sig`。
- **GitHub Actions**：私钥以仓库 **Secret** `LLAMAMONITOR_UPDATE_PRIVATE_KEY_B64`
  （Base64 PEM）注入；key_id 以仓库 **Variable** `LLAMAMONITOR_UPDATE_KEY_ID`
  （缺省 `key-2026-09`）注入。Runner 把 Secret 解码到临时文件，设
  `LLAMAMONITOR_UPDATE_PRIVATE_KEY_FILE`，跑 `build_release.py`。

## 5. 签名流程（构建期，`scripts/build_release.py`）

顺序（任一步失败即中止、不产出发布物）：

1. 构建安装器 + 便携 ZIP。
2. 计算两个资产的 `size` + `sha256`。
3. 组装 manifest（schema 1，含 `signing_key_id`），`canonical_manifest_bytes` → **写盘并
   返回同一串 bytes**。
4. 用 `LLAMAMONITOR_UPDATE_KEY_ID` 对应私钥对这串 bytes 做 Ed25519 签名，写
   `release-manifest.sig`（JSON ASCII sidecar）。
5. `validate_release.py` 独立复算：5 个文件齐全、**先验签**、canonical 格式、哈希一致、
   ZIP 结构、PE 元数据、`--version` CLI。

> **不要**手工编辑 `release-manifest.json` 后忘记重新签名——改了 manifest 字节而没重签，
> 验签必失败。正确做法是改源（版本/资产）后重跑 `build_release.py`。

## 6. 验签流程（运行期，`update_service.check`）

1. 从**受信仓库**（常量）的 **latest Release** 拉取 `release-manifest.json` +
   `release-manifest.sig`（原始 bytes）。
2. `verify_manifest_signature`：
   - `.sig` 必须是 JSON object；
   - `algorithm` 必须 == `Ed25519`；
   - `key_id` 必须在 `TRUSTED_UPDATE_KEYS`（未知 key → 拒绝，报 "unknown signing key"）；
   - `signature` Base64 合法；
   - 用对应公钥对 **manifest 原始 bytes** 验签。
3. 验签通过 → 才解析 manifest 并逐字段校验（schema/product/platform/arch/version/
   filename 与版本一致/size/sha256 格式）。
4. 字段有效且版本更新 → `UPDATE_AVAILABLE`；否则 `UP_TO_DATE` / `ERROR`。

## 7. 密钥轮换（计划内）

当需要**主动**换 key（定期换代、换环境）：

1. `python scripts/generate_update_key.py --key-id key-2027-01` 生成新密钥对
   （新公钥 Base64 + 私钥 PEM 写到 `update-keys/`）。
2. 把新公钥加入 `update_keys.TRUSTED_UPDATE_KEYS`（**保留**旧 key，新旧并存）。
3. 发布**包含新公钥的客户端**（旧客户端不认识新 key，无法信任新 key 的 Release）。
4. 之后 Release 用新私钥签名：构建时设 `LLAMAMONITOR_UPDATE_KEY_ID=key-2027-01`
   （CI 更新仓库 Variable）。
5. 稳定一段时间、确认所有在线客户端都已内置新公钥后，才考虑从表中移除旧 key。

> 轮换**必须**先发"带新公钥的客户端"，再切"新 key 签名"——顺序反了会让旧客户端
> 全部拒绝更新。

## 8. 私钥泄漏响应（5 步）

假设某把私钥（如 `key-2026-09`）泄漏/疑似泄漏：

1. **停止用它签名**：CI 仓库 Variable `LLAMAMONITOR_UPDATE_KEY_ID` 先切到**备用**新 key
   （若没有备用，先执行 §7 生成一把并临时加入公钥表）。
2. **生成新密钥对**（`generate_update_key.py`），新公钥加入 `TRUSTED_UPDATE_KEYS`。
3. **发布一个"仅含新公钥"的客户端**（该版本本身可用**尚未泄漏**的 key 或旧 key 签名——
   只要客户端拿到新公钥即可）。
4. **切换签名 key**：后续所有 Release 用新私钥签名。
5. **吊销/归档旧私钥**：把泄漏 key 移出工作流，从密钥目录移除/作废；在 CHANGELOG 与
   本文件记录事件与影响范围。**旧 key 的签名在公钥表移除前仍会被接受**——所以完成
   §7-4 后尽快移除旧 key 才能真正"吊销"。

> 泄漏的私钥能签名**新的** manifest；但用户客户端只认内置公钥，所以"内置新公钥 +
> 换新 key 签名 + 移除旧 key"三者齐备后，泄漏 key 才失效。

## 9. 测试与密钥卫生

- 单测（`tests/test_update_*.py`）用 `tests/update_util.py` 的 `FakeGithub`：每次生成
  **临时 Ed25519 密钥对**（`generate_update_key.generate_keypair()`），临时 key_id，
  不引用生产私钥；`.trusted()` 上下文把临时公钥注入 `TRUSTED_UPDATE_KEYS`。
- 仓库内**没有**任何私钥文件（`.gitignore` 忽略 `update-keys/`、`*.pem`）。
- 生产私钥只在本地 `%LOCALAPPDATA%\LlamaMonitor\update-keys\` 与 CI Secret。
- 验签/篡改测试（见 README 安全更新章节与 `docs/INSTALLER_TEST.md`）覆盖：
  manifest 单字节/单字段改动、签名单字节改动、错误 key_id、非法算法、缺失 `.sig`/
  manifest、文件名不符、size/hash 不符——全部**拒绝**。

## 10. 一句话总结

> 用户信任的锚点 = **EXE 内置的 Ed25519 公钥**；一切更新（manifest、安装器、ZIP）
> 都必须能用这个锚点验签 + 哈希比对才执行。私钥只在离线环境与 CI Secret；轮换靠
> "先发新公钥客户端、再切新 key 签名"；泄漏靠"新 key + 移除旧 key"吊销。
