import sys
from pathlib import Path as _P
_root = str(_P(__file__).resolve().parents[1])
sys.path.insert(0, _root)
sys.path.insert(0, _root + "/tests")
import asyncio, sqlite3, tempfile
from pathlib import Path
from unittest import mock
from clock import FakeClock
from db import Database
from configutil import make_config
from gpu_collector import GpuCollector

T0 = 1_700_000_000.0
tmp = Path(tempfile.mkdtemp())
cfg = make_config()
cfg.gpu.poll_interval_seconds = 5.0
db = Database(tmp / "audit.db", wal=False)

clock = FakeClock(start_wall=T0, start_mono=T0)
powers = [280.0, 300.0, 320.0]

async def runner(args, timeout):
    joined = " ".join(args)
    if "--query-compute-apps" in joined:
        return 0, ""
    if "ecc.mode.current" in joined:
        return 0, "GPU-R1, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A\n"
    if "pci.bus_id" in joined:
        return 0, "00000000:AF:00.0, GPU-R1, Default, [N/A]\n"
    row = ("0, GPU-R1, X, 100, 2048, 50, 60, [N/A], %.1f, 350.0, 60, "
           "1700, 9501, P8, 3, 16, 3, 16, 550.55, 0x0") % powers[0]
    powers.pop(0)
    return 0, row

c = GpuCollector(cfg, db, runner=runner, clock=clock)
smi_patch = mock.patch("gpu_collector.find_nvidia_smi", return_value=Path("/fake/nvidia-smi"))

def run(fn):
    return asyncio.run(fn())

with smi_patch:
    r1 = run(c.poll_once)
    print("r1:", len(r1), "prev:", c._prev)
clock.advance(5)
with smi_patch, mock.patch.object(db, "save_gpu_samples", side_effect=sqlite3.OperationalError("boom")):
    r2 = run(c.poll_once)
    print("r2:", len(r2), "prev:", c._prev)
clock.advance(5)
with smi_patch:
    r3 = run(c.poll_once)
    print("r3:", len(r3), "prev:", c._prev)
print("daily:", db.get_gpu_daily())
