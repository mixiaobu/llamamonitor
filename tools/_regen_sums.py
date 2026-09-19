import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_release as br

rel = br.ROOT / "release"
artifacts = [rel / "LlamaMonitor-1.0.0-win-x64.zip", rel / "LlamaMonitor-Setup-1.0.0-win-x64.exe"]
for a in artifacts:
    assert a.exists(), f"missing {a}"
br.write_checksums_and_manifest(artifacts, "1.0.0")
