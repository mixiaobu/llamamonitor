"""
validate_release.py — Release 产物验证（Phase 12，§88/§87）。

验证内容（不修改系统安装状态）：
1. 产物文件存在且文件名版本与 version.py 一致（含 release-manifest.sig）；
2. SHA256SUMS.txt 中每个 hash 与文件实际值一致；
3. **Phase 13 签名验证**：release-manifest.sig 用内置公钥（update_keys.py）对
   release-manifest.json 的**原始 bytes** 验签（Ed25519，key_id 受信，算法正确）；
   manifest 字段验证（schema/product/platform/arch/version/filename）；
   manifest 中 installer/portable 的 size + sha256 与实际文件一致；
   文件内容为 canonical 格式（重新序列化 == 原始 bytes）；
4. Portable ZIP 可打开、顶层目录唯一（LlamaMonitor/）、包含 LlamaMonitor.exe，
   且不含用户数据文件名（monitor.db / config.json / ...）；
5. Installer EXE 存在且非 0 长度；
6. EXE PE 版本资源：FileDescription / ProductName / ProductVersion /
   FileVersion（x.y.z.0 形式）与 version.py 一致（版本一致性，§87）；
7. `LlamaMonitor.exe --version` 输出 `LlamaMonitor <version>` 且退出码 0
   （在临时 LOCALAPPDATA 下运行，不碰真实数据）。

用法：python scripts/validate_release.py [release_dir] [version]
退出码：0 = 全部通过；1 = 有失败。
"""

from __future__ import annotations

import ctypes
import io
import os
import platform
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_checksums import sha256_file  # noqa: E402

EXCLUDED_FILE_NAMES = {
    "monitor.db",
    "monitor.db-wal",
    "monitor.db-shm",
    "config.json",
    "monitor.log",
}

# langid/codepage 040904B0 由 scripts/build_release.py 生成的 version_info.txt 固定
# （StringTable('040904B0', ...)）；VerQueryValue 通配符 * 不可靠，用显式 langid。
_PE_LANG = r"040904B0"
PE_VERSION_INFO = {
    "FILEDESC": r"\\StringFileInfo\\" + _PE_LANG + r"\\FileDescription",
    "PRODUCTNAME": r"\\StringFileInfo\\" + _PE_LANG + r"\\ProductName",
    "PRODUCTVERSION": r"\\StringFileInfo\\" + _PE_LANG + r"\\ProductVersion",
}


def _pe_version_string(exe: Path, subblock: str) -> str | None:
    """
    用 Win32 API 读 PE 版本资源字符串（ctypes，无额外依赖）。
    subblock: 'FILEVERSION' | 'PRODUCTVERSION' | 'FILEDESC' | 'PRODUCTNAME'。
    """
    if not os.name == "nt":
        return None
    version = ctypes.windll.version
    fn = str(exe).encode("ascii") + b"\x00"

    # GetFileVersionInfoSizeA 的返回值即缓冲区大小（第二参数 lpdwHandle 可为 NULL）
    size = version.GetFileVersionInfoSizeA(fn, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoA(fn, 0, size, buf):
        return None
    out_len = ctypes.c_uint()
    # 资源键为纯 ASCII；用 ANSI 变体（VerQueryValueA）——其返回的字符串值
    # 也是 ANSI 字节（长度=字节数），直接按 latin-1 解码，避免 UTF-16 陷阱
    pattern = PE_VERSION_INFO[subblock].encode("ascii") + b"\x00"
    ptr = ctypes.c_void_p()
    if not version.VerQueryValueA(buf, pattern, ctypes.byref(ptr), ctypes.byref(out_len)):
        return None
    raw = ctypes.string_at(ptr, out_len.value)
    return raw.decode("latin-1").rstrip("\x00")


def _pe_version_number(exe: Path, which: str = "FileVersion") -> str | None:
    """
    读 PE 版本资源根块 VS_FIXEDFILEINFO 的 4 元组数字版本（x.y.z.0 形式）。
    which: 'FileVersion' | 'ProductVersion'。
    VS_FIXEDFILEINFO 位于根块（wType=0xFFFF）Value 字段：
    偏移 0: dwSignature, 4: dwStrucVersion, 8: fileVersionMS,
    12: fileVersionLS, 16: productVersionMS, 20: productVersionLS（各 4 字节）。
    """
    if not os.name == "nt":
        return None
    version = ctypes.windll.version
    fn = str(exe).encode("ascii") + b"\x00"
    size = version.GetFileVersionInfoSizeA(fn, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoA(fn, 0, size, buf):
        return None
    ptr = ctypes.c_void_p()
    if not version.VerQueryValueA(buf, b"\\\x00", ctypes.byref(ptr), ctypes.byref(ctypes.c_uint())):
        return None
    base = ctypes.cast(ptr, ctypes.c_void_p).value
    ms_offset = 8 if which == "FileVersion" else 16
    hi, lo = struct.unpack_from("<II", ctypes.string_at(base, 24), ms_offset)
    # hi 高 16 位 = major，低 16 位 = minor；lo 同理
    return f"{hi >> 16}.{hi & 0xFFFF}.{lo >> 16}.{lo & 0xFFFF}"


def check_pe_metadata(exe: Path, version: str) -> list[str]:
    """EXE 版本资源一致性（§87：除 FileVersion 4 元组外必须与 version.py 相同）。"""
    failures = []
    if not os.name == "nt":
        return ["(skip) PE metadata check needs Windows"]
    expected_filever = version + ".0"
    checks = {
        "FILEDESC": ("LlamaMonitor", "FileDescription"),
        "PRODUCTNAME": ("LlamaMonitor", "ProductName"),
        "PRODUCTVERSION": (version, "ProductVersion"),
    }
    for sub, (expected, label) in checks.items():
        actual = _pe_version_string(exe, sub)
        if actual != expected:
            failures.append(f"PE {label}: expected {expected!r}, got {actual!r}")
    actual_filever = _pe_version_number(exe, "FileVersion")
    if actual_filever != expected_filever:
        failures.append(f"PE FileVersion (numeric): expected {expected_filever!r}, got {actual_filever!r}")
    return failures


def validate_release(release_dir: str | Path, version: str) -> list[str]:
    """
    执行全部验证，返回失败列表（空 = 通过）。
    """
    failures: list[str] = []
    release_dir = Path(release_dir)
    zip_name = f"LlamaMonitor-{version}-win-x64.zip"
    setup_name = f"LlamaMonitor-Setup-{version}-win-x64.exe"
    zip_path = release_dir / zip_name
    setup_path = release_dir / setup_name
    sums_path = release_dir / "SHA256SUMS.txt"
    manifest_path = release_dir / "release-manifest.json"
    sig_path = release_dir / "release-manifest.sig"

    # 1) 文件存在（Phase 13：签名 sidecar 是正式 Release 的必要产物）
    for p in (zip_path, setup_path, sums_path, manifest_path, sig_path):
        if not p.is_file():
            failures.append(f"missing: {p}")
    if failures:
        return failures  # 缺文件时后续检查无意义

    # 2) SHA256SUMS 一致性
    for line in sums_path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        target = release_dir / name
        if not target.is_file():
            failures.append(f"SHA256SUMS references missing file: {name}")
            continue
        actual = sha256_file(target)
        if actual != digest.strip():
            failures.append(f"SHA256 mismatch for {name}")

    # 3) Phase 13：签名验证 + manifest 字段/产物一致性（验签在字段验证**之前**）
    try:
        import json
        from update_manifest import (
            canonical_manifest_bytes,
            verify_manifest_signature,
            validate_manifest_fields,
        )

        manifest_bytes = manifest_path.read_bytes()
        ok, key_id, err = verify_manifest_signature(manifest_bytes, sig_path.read_bytes())
        if not ok:
            failures.append(f"manifest 签名校验失败：{err} (key_id={key_id!r})")
            return failures  # 签名不过关：字段校验无意义（未信任内容）
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        # canonical 格式：原始 bytes 必须与规范化序列化一致（构建/签名同一字节序列）
        if canonical_manifest_bytes(manifest) != manifest_bytes:
            failures.append("manifest 不是 canonical 格式（重新序列化结果不一致）")
        selected, field_errors = validate_manifest_fields(manifest)
        if field_errors:
            failures.extend(f"manifest 字段：{e}" for e in field_errors)
        if selected.get("version") != version:
            failures.append(f"manifest 版本不匹配：{selected.get('version')!r} != {version!r}")
        # installer/portable：filename + size + sha256 与实际产物一致
        for section, expected_file in (("installer", setup_path), ("portable", zip_path)):
            entry = selected.get(section)
            if not entry:
                failures.append(f"manifest missing {section} entry")
                continue
            target = release_dir / entry["filename"]
            if not target.is_file():
                failures.append(f"manifest {section} file missing: {entry['filename']}")
                continue
            if target.stat().st_size != entry["size"]:
                failures.append(f"manifest {section} size mismatch: {entry['filename']}")
            if sha256_file(target) != entry["sha256"]:
                failures.append(f"manifest {section} sha256 mismatch: {entry['filename']}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"manifest/signature validation error: {exc!r}")

    # 4) ZIP 结构
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            tops = {n.split("/", 1)[0] for n in names}
            if tops != {"LlamaMonitor"}:
                failures.append(f"ZIP top-level dirs not exactly 'LlamaMonitor': {sorted(tops)}")
            if "LlamaMonitor/LlamaMonitor.exe" not in names:
                failures.append("ZIP missing LlamaMonitor/LlamaMonitor.exe")
            bad = [n for n in names if n.split("/")[-1] in EXCLUDED_FILE_NAMES]
            if bad:
                failures.append(f"ZIP contains user data files: {bad[:5]}")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"ZIP unreadable: {exc!r}")

    # 5) Installer 非 0 长度
    if setup_path.stat().st_size == 0:
        failures.append("installer EXE is empty")

    # 6) PE 版本资源
    exe = release_dir / ".." / "dist" / "LlamaMonitor" / "LlamaMonitor.exe"
    if exe.is_file():
        failures.extend(check_pe_metadata(exe, version))
    else:
        failures.append(f"dist EXE missing for PE check: {exe}")

    # 7) --version CLI（临时 LOCALAPPDATA，不碰真实数据）
    try:
        with tempfile.TemporaryDirectory(prefix="llamamonitor_validate_") as td:
            env = dict(os.environ)
            env["LOCALAPPDATA"] = td
            env.pop("LLAMAMONITOR_CONFIG", None)
            proc = subprocess.run(
                [str(exe), "--version"],
                capture_output=True, text=True, timeout=120, env=env,
                cwd=str(exe.parent),
            )
            expected = f"LlamaMonitor {version}"
            got = (proc.stdout or "").strip()
            if proc.returncode != 0:
                failures.append(f"--version exit code {proc.returncode}: {proc.stderr.strip()[:200]}")
            elif expected not in got:
                failures.append(f"--version output mismatch: {got!r} (expected to contain {expected!r})")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"--version CLI check failed: {exc!r}")

    return failures


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))  # update_manifest / update_keys / version（项目根）
    release_dir = Path(argv[0]) if len(argv) > 0 else root / "release"
    version = argv[1] if len(argv) > 1 else None
    if version is None:
        from version import __version__
        version = __version__
    failures = validate_release(release_dir, version)
    if failures:
        print("RELEASE VALIDATION FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"RELEASE VALIDATION OK: {release_dir} (version {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
