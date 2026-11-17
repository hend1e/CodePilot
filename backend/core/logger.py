# backend/core/logger.py
# 结构化日志：logger.info("event.name", key=value)，输出单行 JSON，便于 ELK/Loki 采集。

import json
import logging
import sys
import time

from backend.config import get_settings

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module",
    "exc_info", "exc_text", "stack_info", "lineno", "funcName", "created", "msecs",
    "relativeCreated", "thread", "threadName", "processName", "process", "taskName",
}


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化成一行 JSON；额外字段来自 logger.info(..., extra={...})。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _Logger:
    """统一日志门面：支持 logger.info("事件", key=value) 的结构化写法。"""

    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, exc_info: bool = False, **kw) -> None:
        self._log.log(level, event, extra=kw, exc_info=exc_info, stacklevel=3)

    def debug(self, event: str, **kw) -> None:
        self._emit(logging.DEBUG, event, **kw)

    def info(self, event: str, **kw) -> None:
        self._emit(logging.INFO, event, **kw)

    def warning(self, event: str, **kw) -> None:
        self._emit(logging.WARNING, event, **kw)

    def error(self, event: str, exc_info: bool = False, **kw) -> None:
        self._emit(logging.ERROR, event, exc_info=exc_info, **kw)

    def critical(self, event: str, **kw) -> None:
        self._emit(logging.CRITICAL, event, **kw)


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for noisy in ("httpx", "httpcore", "sqlalchemy.engine", "sqlalchemy.pool", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # pymilvus 在连不上时刷 ERROR，但我们已经降级处理，不需要它刷屏
    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)


def get_logger(name: str) -> _Logger:
    return _Logger(name)
