"""临时 runner：固定端口 9092 的 Fake llama.cpp（Router 模式）——1.3.2 UI 验证用。"""
import sys
from pathlib import Path
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))
import fakeservers  # noqa: E402

PORT = 9092
state = fakeservers.FakeLlamaState(router=True)
handler = type("H", (fakeservers._LlamaHandler,), {"state": state})
httpd = ThreadingHTTPServer(("127.0.0.1", PORT), handler)
print(f"fake llama (router) on http://127.0.0.1:{PORT}", flush=True)
httpd.serve_forever()
