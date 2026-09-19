"""临时脚本：从当前环境 dist-info METADATA 生成 THIRD_PARTY_NOTICES.txt（人工审核后保留）。"""
import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
freeze = subprocess.run([sys.executable, "-m", "pip", "freeze"], capture_output=True, text=True, check=True).stdout
packages = {}
for line in freeze.splitlines():
    if "==" in line:
        name, ver = line.split("==", 1)
        packages[name.lower()] = (name, ver)

def _first_lines(path: Path, n: int = 2) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    parts = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith(("/*", " *", "/*", "<", "#  ")):
            parts.append(s)
        if len(parts) >= n:
            break
    return " ".join(parts)

def license_for(name, ver):
    try:
        dist = md.distribution(name)
    except Exception:
        return "VERIFY"
    lic = dist.metadata.get("License")
    if lic:
        lic = " ".join(lic.split())
        if len(lic) > 120:
            lic = lic[:117] + "..."
        return lic
    # 新式打包只有 License-File：读许可证文件头（实际文本，不是猜测）
    # dist._path 指向 dist-info 目录本身
    try:
        base = Path(dist._path)
        raw = (base / "METADATA").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^License-File: ?(.+)$", raw, re.M)
        if m:
            licfile = (base / m.group(1).strip()).resolve()
            if licfile.is_file():
                head = _first_lines(licfile)
                if head:
                    if len(head) > 120:
                        head = head[:117] + "..."
                    return f"{head} (full text: {licfile.name})"
        # 无法本地确认：附上项目主页（人工确认入口，不猜许可证）
        home = dist.metadata.get("Home-page")
        if not home:
            purl = dist.metadata.get("Project-URL")  # "标签, https://..."
            if purl and "," in purl:
                home = purl.split(",", 1)[1].strip()
            elif purl:
                home = purl
        if home:
            return f"VERIFY (project: {home})"
    except Exception:
        pass
    return "VERIFY"

lines = [
    "LlamaMonitor THIRD-PARTY NOTICES",
    "=",
    "",
    "基于 1.0.0 构建环境（pip freeze + importlib.metadata）生成；",
    "\"VERIFY\" 表示未能自动确认许可证文本，发布前需人工确认。",
    "第三方代码版权归各自作者所有。",
    "",
]
for key in sorted(packages):
    display, ver = packages[key]
    lines.append(f"- {display} {ver} — License: {license_for(display, ver)}")

# ECharts（本地静态资源，非 pip 包）
echarts_path = ROOT / "static" / "echarts.min.js"
if echarts_path.exists():
    head = echarts_path.read_text(encoding="utf-8", errors="replace")[:400]
    lic = "Apache-2.0" if re.search(r"Apache License|Apache-2\.0|Apache 2", head) else "VERIFY (check file header)"
    lines.append(f"- Apache ECharts (static/echarts.min.js, bundled) — License: {lic}")

(ROOT / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"wrote {len(packages)} packages")
