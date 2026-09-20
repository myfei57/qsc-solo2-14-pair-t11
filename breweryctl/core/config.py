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
    event_relay_enabled: bool = True
    event_endpoint: str | None = None
    event_token: str | None = None
    event_timeout_s: float = 5.0
    event_batch_size: int = 16
    event_retry_base_s: float = 1.0
    event_retry_max_s: float = 60.0
    event_stale_claim_s: int = 120
    log_level: str = "INFO"

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
        if self.event_endpoint is not None:
            clean_endpoint = require_text(self.event_endpoint, field="event_endpoint", max_length=300)
            if not clean_endpoint.startswith(("http://", "https://")):
                raise ValidationError("event_endpoint 必须是 http(s) 地址", value=self.event_endpoint)
        require_number(self.event_timeout_s, field="event_timeout_s", minimum=0.5, maximum=60.0)
        require_int(self.event_batch_size, field="event_batch_size", minimum=1, maximum=256)
        require_number(self.event_retry_base_s, field="event_retry_base_s", minimum=0.05, maximum=30.0)
        require_number(self.event_retry_max_s, field="event_retry_max_s", minimum=1.0, maximum=3600.0)
        if self.event_retry_max_s < self.event_retry_base_s:
            raise ValidationError("event_retry_max_s 不能小于 event_retry_base_s")
        require_int(self.event_stale_claim_s, field="event_stale_claim_s", minimum=5, maximum=86_400)
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValidationError("log_level 取值不合法", field="log_level", value=self.log_level)
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
            "event_relay_enabled": self.event_relay_enabled,
            "event_endpoint": self.event_endpoint,
            "event_token_configured": bool(self.event_token),
            "event_timeout_s": self.event_timeout_s,
            "event_batch_size": self.event_batch_size,
            "event_retry_base_s": self.event_retry_base_s,
            "event_retry_max_s": self.event_retry_max_s,
            "event_stale_claim_s": self.event_stale_claim_s,
            "log_level": self.log_level.upper(),
        }

    @classmethod
    def from_env(cls) -> "Settings":
        """从 ``BREWERYCTL_*`` 环境变量读取配置。"""

        base = cls()
        text_keys = ("host", "log_level", "event_endpoint", "event_token")
        int_keys = ("port", "max_active_batches", "cip_certificate_ttl_min", "event_batch_size", "event_stale_claim_s")
        float_keys = (
            "temp_tolerance_c",
            "pitch_temp_max_c",
            "pressure_limit_bar",
            "hop_window_slack_min",
            "event_timeout_s",
            "event_retry_base_s",
            "event_retry_max_s",
        )
        bool_keys = ("event_relay_enabled",)
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
        for key in bool_keys:
            raw = os.environ.get(ENV_PREFIX + key.upper())
            if raw is not None:
                values[key] = raw.strip().lower() not in {"0", "false", "no"}
        return base.with_overrides(**values)
