# backend/services/metrics.py
# 极简 Prometheus 指标（不引第三方库，够用即可）：审查量、发现量、降级、成本、延迟。

import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[tuple[str, tuple], float] = defaultdict(float)


def inc(name: str, value: float = 1.0, **labels) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _lock:
        _counters[key] += value


def render() -> str:
    lines: list[str] = []
    with _lock:
        for (name, labels), value in _counters.items():
            if labels:
                label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_str}}} {value}")
            else:
                lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    with _lock:
        _counters.clear()
