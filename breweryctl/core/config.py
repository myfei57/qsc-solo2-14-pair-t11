"""运行时配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .validators import require_int, require_number, require_text

ENV_PREFIX = "BREWERYCTL_"


@dataclass(frozen=True)
class Settings:
    """平台启动参数与工艺阈值。"""

    host: str = "127.0.0.1"
    port: int = 8080
    data_dir: Path = Path("var/breweryctl")
    fsync: bool = True
    max_active_batches: int = 4
    temp_tolerance_c: float = 0.8
    pitch_temp_max_c: float = 12.0
    cip_certificate_ttl_min: int = 240
    pressure_limit_bar: float = 1.8
    hop_window_slack_min: float = 5.0
    log_level: str = "INFO"
    # 关键事件外发
    outbound_enabled: bool = False
    outbound_endpoint: str = ""
    outbound_token: str = ""
    outbound_reconcile_endpoint: str = ""
    outbound_batch_size: int = 32
    outbound_idle_sec: float = 2.0

    def validate(self) -> "Settings":
        """校验配置取值并返回自身，便于启动时链式调用。"""

        require_text(self.host, field="host", max_length=120)
        require_int(self.port, field="port", minimum=0, maximum=65535)
        require_int(self.max_active_batches, field="max_active_batches", minimum=1, maximum=64)
        require_number(self.temp_tolerance_c, field="temp_tolerance_c", minimum=0.05, maximum=10.0)
        require_number(self.pitch_temp_max_c, field="pitch_temp_max_c", minimum=2.0, maximum=30.0)
        require_int(self.cip_certificate_ttl_min, field="cip_certificate_ttl_min", minimum=5, maximum=2880)
        require_number(self.pressure_limit_bar, field="pressure_limit_bar", minimum=0.1, maximum=10.0)
        require_number(self.hop_window_slack_min, field="hop_window_slack_min", minimum=0.0, maximum=60.0)
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValidationError("log_level 取值不合法", field="log_level", value=self.log_level)
        if self.outbound_enabled:
            require_text(self.outbound_endpoint, field="outbound_endpoint", max_length=300)
            if not self.outbound_endpoint.startswith(("http://", "https://")):
                raise ValidationError("outbound_endpoint 必须是 http(s) 地址")
        require_int(self.outbound_batch_size, field="outbound_batch_size", minimum=1, maximum=500)
        require_number(self.outbound_idle_sec, field="outbound_idle_sec", minimum=0.1, maximum=300.0)
        return self

    def ensure_layout(self) -> dict[str, str]:
        """创建数据目录并返回关键路径。"""

        root = self.data_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "snapshots").mkdir(exist_ok=True)
        return {
            "data_dir": str(root),
            "snapshot": str(root / "state.json"),
            "journal": str(root / "journal.jsonl"),
        }

    def with_overrides(self, **overrides: Any) -> "Settings":
        """返回带命令行覆盖值的新配置对象。"""

        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean).validate()

    def describe(self) -> dict[str, Any]:
        """输出可公开的配置摘要。"""

        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "fsync": self.fsync,
            "max_active_batches": self.max_active_batches,
            "temp_tolerance_c": self.temp_tolerance_c,
            "pitch_temp_max_c": self.pitch_temp_max_c,
            "cip_certificate_ttl_min": self.cip_certificate_ttl_min,
            "pressure_limit_bar": self.pressure_limit_bar,
            "hop_window_slack_min": self.hop_window_slack_min,
            "log_level": self.log_level.upper(),
            "outbound_enabled": self.outbound_enabled,
            "outbound_endpoint": self.outbound_endpoint,
            "outbound_reconcile_endpoint": self.outbound_reconcile_endpoint,
            "outbound_batch_size": self.outbound_batch_size,
            "outbound_idle_sec": self.outbound_idle_sec,
        }

    @classmethod
    def from_env(cls) -> "Settings":
        """从 ``BREWERYCTL_*`` 环境变量读取配置。"""

        base = cls()
        text_keys = ("host", "log_level")
        int_keys = ("port", "max_active_batches", "cip_certificate_ttl_min")
        float_keys = (
            "temp_tolerance_c",
            "pitch_temp_max_c",
            "pressure_limit_bar",
            "hop_window_slack_min",
        )
        values: dict[str, Any] = {}
        for key in text_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = raw
        for key in int_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = int(raw)
        for key in float_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = float(raw)
        data_dir = os.environ.get(ENV_PREFIX + "DATA_DIR")
        if data_dir:
            values["data_dir"] = Path(data_dir)
        fsync = os.environ.get(ENV_PREFIX + "FSYNC")
        if fsync is not None:
            values["fsync"] = fsync.strip().lower() not in {"0", "false", "no"}
        for key in ("outbound_endpoint", "outbound_token", "outbound_reconcile_endpoint"):
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = raw
        enabled = os.environ.get(ENV_PREFIX + "OUTBOUND_ENABLED")
        if enabled is not None:
            values["outbound_enabled"] = enabled.strip().lower() not in {"0", "false", "no"}
        raw_batch = os.environ.get(ENV_PREFIX + "OUTBOUND_BATCH_SIZE")
        if raw_batch is not None:
            values["outbound_batch_size"] = int(raw_batch)
        raw_idle = os.environ.get(ENV_PREFIX + "OUTBOUND_IDLE_SEC")
        if raw_idle is not None:
            values["outbound_idle_sec"] = float(raw_idle)
        return base.with_overrides(**values)
