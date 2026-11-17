# backend/infra/sandbox.py
# 沙箱执行器：把 LLM 生成的测试放进一次性容器里跑。
# 安全边界：--network none（断网）、内存/CPU/PID 限额、只挂载临时工作区、非 root、超时强杀。
# 设计取舍：docker 不可用时允许 local 模式（仅本机开发），生产必须 docker。

import asyncio
import json
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import get_settings
from backend.core.exceptions import SandboxError, SandboxTimeout
from backend.core.logger import get_logger

logger = get_logger(__name__)

_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "venv", "node_modules", "__pycache__", "*.pyc",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".idea",
)
_SUMMARY_RE = re.compile(r"(\d+) (passed|failed|error)")


@dataclass
class SandboxResult:
    status: str = "skipped"           # passed | failed | error | skipped
    passed: int = 0
    failed: int = 0
    coverage: float | None = None
    stdout_tail: str = ""
    error: str | None = None
    duration_ms: int = 0
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status, "passed": self.passed, "failed": self.failed,
            "coverage": self.coverage, "stdout_tail": self.stdout_tail,
            "error": self.error, "duration_ms": self.duration_ms, "files": self.files,
        }


class SandboxRunner:
    def __init__(self):
        self.s = get_settings()

    async def run(self, repo_path: str | None, test_files: dict[str, str],
                  target: str | None = None) -> SandboxResult:
        """test_files: {相对路径: 文件内容}。target: 只跑某个测试文件。"""
        mode = self.s.sandbox_mode.lower()
        if mode == "off":
            return SandboxResult(status="skipped", error="sandbox_disabled")
        if not test_files:
            return SandboxResult(status="skipped", error="no_test_files")

        workspace = Path(self.s.sandbox_workspace_root).resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="cp_sandbox_", dir=str(workspace)))
        started = time.monotonic()
        try:
            if repo_path:
                src = Path(repo_path).resolve()
                if not src.exists():
                    return SandboxResult(status="error", error=f"repo_path_not_found: {repo_path}")
                # 只复制一次，沙箱内可写；跳过 .git / venv / node_modules
                await asyncio.to_thread(shutil.copytree, src, tmp / "repo", dirs_exist_ok=True,
                                        ignore=_IGNORE, symlinks=True)
                root = tmp / "repo"
            else:
                root = tmp / "repo"
                root.mkdir(parents=True, exist_ok=True)

            for rel, content in test_files.items():
                dest = (root / rel).resolve()
                if root.resolve() not in dest.parents:
                    return SandboxResult(status="error", error=f"unsafe_test_path: {rel}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8")

            if mode == "docker":
                result = await self._run_docker(root, list(test_files), target)
            else:
                result = await self._run_local(root, list(test_files), target)

            result.duration_ms = int((time.monotonic() - started) * 1000)
            result.files = list(test_files)
            logger.info("sandbox.executed", status=result.status, passed=result.passed,
                        failed=result.failed, coverage=result.coverage,
                        duration_ms=result.duration_ms)
            return result
        except SandboxTimeout:
            raise
        except Exception as e:      # noqa: BLE001
            logger.error("sandbox.failed", error=str(e), exc_info=True)
            return SandboxResult(status="error", error=str(e)[:1000],
                                 duration_ms=int((time.monotonic() - started) * 1000))
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, True)

    def _docker_cmd(self, root: Path, files: list[str]) -> list[str]:
        s = self.s
        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", s.sandbox_memory,
            "--cpus", s.sandbox_cpus,
            "--pids-limit", str(s.sandbox_pids_limit),
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
            "--user", f"{__import__('os').getuid()}:{__import__('os').getgid()}",
            "-v", f"{root}:/workspace:rw",
            "-w", "/workspace",
            "--tmpfs", "/tmp:rw,size=64m",
            s.sandbox_image,
            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "--cov=.", "--cov-report=json:/workspace/.codepilot_coverage.json",
            "--junitxml=/workspace/.codepilot_junit.xml",
            *files,
        ]
        return cmd

    async def _run_docker(self, root: Path, files: list[str], target: str | None) -> SandboxResult:
        cmd_files = [target] if target else files
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._docker_cmd(root, cmd_files),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as e:
            raise SandboxError("docker 不可用，请安装 Docker 或设置 SANDBOX_MODE=local") from e

        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.s.sandbox_timeout_seconds
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise SandboxTimeout(f"沙箱执行超过 {self.s.sandbox_timeout_seconds}s") from e

        return self._collect(root, (out or b"").decode("utf-8", "replace"), proc.returncode)

    async def _run_local(self, root: Path, files: list[str], target: str | None) -> SandboxResult:
        cmd_files = [target] if target else files
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "--cov=.", "--cov-report=json:.codepilot_coverage.json", *cmd_files]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.s.sandbox_timeout_seconds
            )
        except asyncio.TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise SandboxTimeout(f"本地沙箱执行超过 {self.s.sandbox_timeout_seconds}s") from e
        return self._collect(root, (out or b"").decode("utf-8", "replace"), proc.returncode)

    def _collect(self, root: Path, stdout: str, code: int | None) -> SandboxResult:
        passed = failed = 0
        for num, kind in _SUMMARY_RE.findall(stdout):
            if kind == "passed":
                passed += int(num)
            else:
                failed += int(num)

        coverage = None
        cov_file = root / ".codepilot_coverage.json"
        if cov_file.exists():
            try:
                data = json.loads(cov_file.read_text(encoding="utf-8"))
                coverage = round(float(data["totals"]["percent_covered"]), 2)
            except Exception:       # noqa: BLE001
                coverage = None

        if passed and not failed and code == 0:
            status = "passed"
        elif failed:
            status = "failed"
        elif code not in (0, 1):        # 2 = 内部错误 / 5 = 没收集到用例
            status = "error"
        else:
            status = "failed"

        return SandboxResult(
            status=status, passed=passed, failed=failed, coverage=coverage,
            stdout_tail="\n".join(stdout.strip().splitlines()[-60:]),
            error=None if code in (0, 1) else f"exit_code={code}",
        )
