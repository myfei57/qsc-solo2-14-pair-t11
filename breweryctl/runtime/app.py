"""应用对象：装配注册表与控制台服务器。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..console.api import ApiRouter
from ..console.serializers import page_banner
from ..console.server import ConsoleServer
from ..core.clock import Clock
from ..core.config import Settings
from ..service.registry import ComponentRegistry

LOGGER = logging.getLogger("breweryctl.app")


def web_root() -> Path:
    """返回内置前端页面目录。"""

    return Path(__file__).resolve().parent.parent / "web"


class Application:
    """把配置、领域组件与 HTTP 服务器组合成可运行进程。"""

    def __init__(self, settings: Settings, clock: Clock | None = None) -> None:
        self.settings = settings.validate()
        self.registry = ComponentRegistry(self.settings, clock=clock)
        self.router = ApiRouter(self.registry)
        self.server = ConsoleServer(
            self.router,
            web_root(),
            self.settings.host,
            self.settings.port,
        )
        self._booted = False

    def bootstrap(self) -> dict[str, Any]:
        """初始化默认真实数据并完成一次崩溃恢复。"""

        layout = self.settings.ensure_layout()
        created = self.registry.bootstrap()
        self.registry.store.set_meta("layout", layout)
        self._booted = True
        return created

    def serve(self) -> int:
        """启动控制台并阻塞在请求循环。"""

        self.bootstrap()
        self.registry.start_relay()
        LOGGER.info("BreweryCtl 启动，数据目录 %s", self.settings.data_dir)
        try:
            self.server.start()
            host, port = self.server.address
            LOGGER.info("控制台地址 http://%s:%s", host, port)
            self.server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("收到中断信号，准备退出")
            self.server.stop()
        finally:
            self.close()
        return 0

    def check(self) -> dict[str, Any]:
        """执行一次自检，返回平台摘要。"""

        created = self.bootstrap()
        overview = self.registry.state_overview()
        return {
            "boot": created,
            "banner": page_banner(overview),
            "store": overview["store"],
            "batches": overview["batches"],
            "alarms": overview["alarms"],
        }

    def snapshot(self) -> dict[str, Any]:
        """写出一次快照并返回路径信息。"""

        self.bootstrap()
        document = self.registry.store.snapshot_now()
        stats = self.registry.store.stats()
        return {
            "snapshot_path": stats["snapshot_path"],
            "journal_path": stats["journal_path"],
            "sequence": document.get("seq"),
            "collections": stats["collections"],
        }

    def close(self) -> None:
        """关闭存储。"""

        if self._booted:
            self.registry.close()
            self._booted = False
