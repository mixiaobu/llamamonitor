"""
测试公共工具：快速构造 AppConfig / LoadedConfig（无文件 I/O、无副作用）。

项目使用标准库 unittest（非 pytest），临时目录统一用 tempfile.TemporaryDirectory
（等价于 pytest 的 tmp_path fixture），不污染真实的 %LOCALAPPDATA%\\LlamaMonitor。
"""

from __future__ import annotations

from pathlib import Path

from config import AppConfig, LoadedConfig


def make_config(url: str = "http://127.0.0.1:9", poll_interval: float = 3600.0) -> AppConfig:
    """
    测试用配置：全默认 + 不可达的 metrics 基础地址（127.0.0.1:9 连接即被拒绝）
    + 足够长的采集间隔（应用测试中后台循环不会在测试期间触发额外轮次）。
    """
    cfg = AppConfig.default()
    cfg.llama_server.url = url
    cfg.collector.poll_interval_seconds = poll_interval
    return cfg


def make_loaded(cfg: AppConfig, tmp: Path) -> LoadedConfig:
    """构造一个"已正常加载"的 LoadedConfig（路径指向 tmp 下的 config.json）。"""
    return LoadedConfig(
        config=cfg,
        path=tmp / "config.json",
        loaded=True,
        using_defaults=False,
        has_errors=False,
    )


def loopback_app(app, host: str = "127.0.0.1"):
    """
    把 ASGI 应用的客户端地址改写为本地（默认 127.0.0.1）。

    starlette 的 TestClient 默认 scope["client"] 是 ("testclient", 50000)——不是
    真实 socket 地址，而 Phase 10 的本地管理 API（PUT /api/config、/api/data/* 等）
    要求 request.client.host ∈ {127.0.0.1, ::1}。测试里调用这些"修改类"端点前用
    本包装器模拟本机请求。只读端点不强制，裸 TestClient 也可访问。
    """

    async def wrapper(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope["client"] = (host, 12345)
        await app(scope, receive, send)

    return wrapper
