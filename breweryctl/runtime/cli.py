"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Sequence

from breweryctl import __version__

from ..core.config import Settings
from .app import Application


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""

    parser = argparse.ArgumentParser(
        prog="breweryctl",
        description="BreweryCtl 啤酒酿造糖化与发酵控制平台",
    )
    parser.add_argument("command", nargs="?", default="serve", choices=("serve", "check", "snapshot", "outbox-flush", "reconcile", "version"))
    parser.add_argument("--host", default=None, help="监听地址，默认读取 BREWERYCTL_HOST")
    parser.add_argument("--port", type=int, default=None, help="监听端口")
    parser.add_argument("--data-dir", default=None, help="数据目录")
    parser.add_argument("--log-level", default=None, choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    parser.add_argument("--no-fsync", action="store_true", help="关闭落盘 fsync，仅用于调试")
    parser.add_argument("--outbound-endpoint", default=None, help="关键事件外发地址")
    parser.add_argument("--outbound-token", default=None, help="外发鉴权令牌")
    parser.add_argument("--batch-id", default=None, help="对账时限定批次")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数并执行子命令。"""

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    settings = _settings_from_args(args)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    application = Application(settings)
    try:
        if args.command == "check":
            print(json.dumps(application.check(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "snapshot":
            print(json.dumps(application.snapshot(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "outbox-flush":
            application.bootstrap()
            print(json.dumps(application.registry.flush_outbox(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.command == "reconcile":
            application.bootstrap()
            registry = application.registry
            if registry.reconcile_client is not None:
                report = registry.reconciler.remote_check(
                    registry.reconcile_client, batch_id=args.batch_id
                )
                requeued = registry.reconciler.requeue_missing(report)
                payload = {"scope": "remote", "requeued": requeued, "report": report.as_dict()}
            else:
                report = registry.reconciler.local_check(batch_id=args.batch_id)
                payload = {"scope": "local", "report": report.as_dict()}
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        return application.serve()
    finally:
        application.close()


def _settings_from_args(args: argparse.Namespace) -> Settings:
    overrides: dict[str, object] = {
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
    }
    if args.data_dir is not None:
        overrides["data_dir"] = Path(args.data_dir)
    if args.no_fsync:
        overrides["fsync"] = False
    if args.outbound_endpoint is not None:
        overrides["outbound_enabled"] = True
        overrides["outbound_endpoint"] = args.outbound_endpoint
    if args.outbound_token is not None:
        overrides["outbound_token"] = args.outbound_token
    return Settings.from_env().with_overrides(**overrides)
