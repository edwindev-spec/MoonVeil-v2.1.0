"""Official Luau discovery and bounded, file-backed subprocess execution."""

from __future__ import annotations

import math
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

try:  # POSIX only
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None

from .errors import MoonVeilError

ROOT = Path(__file__).resolve().parent.parent


def positive_timeout(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise MoonVeilError("timeout must be finite and greater than zero")
    return value


def _executable(value: Path) -> Path:
    candidate = value.expanduser().resolve()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise MoonVeilError(f"not an executable file: {value}")
    return candidate


def find_luau(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return _executable(explicit)
    if os.environ.get("LUAU_PATH"):
        return _executable(Path(os.environ["LUAU_PATH"]))
    suffix = ".exe" if os.name == "nt" else ""
    installed = ROOT / ".tools" / "luau-0.722" / ("luau" + suffix)
    if installed.is_file():
        return _executable(installed)
    found = shutil.which("luau")
    if found:
        return _executable(Path(found))
    raise MoonVeilError("Luau not found; run python tools/install_luau.py, set LUAU_PATH, or pass --luau")


def find_compiler(luau: Path) -> Optional[Path]:
    if os.environ.get("LUAU_COMPILE_PATH"):
        return _executable(Path(os.environ["LUAU_COMPILE_PATH"]))
    sibling = luau.with_name("luau-compile.exe" if os.name == "nt" else "luau-compile")
    if sibling.is_file():
        return _executable(sibling)
    found = shutil.which("luau-compile")
    return _executable(Path(found)) if found else None


DEFAULT_MEMORY_MB = 1024
_MAX_BYTES = 16 * 1024 * 1024


def positive_memory(value) -> int:
    if type(value) is not int or value < 0:
        raise MoonVeilError("memory limit must be a non-negative integer number of MiB (0 disables it)")
    return value


def _environment():
    """The child gets no secrets: only what the OS loader needs."""
    keep = ("SYSTEMROOT", "PATH") if os.name == "nt" else ("PATH",)
    return {key: os.environ[key] for key in keep if key in os.environ}


def _limiter(timeout: float, memory_mb: int, max_bytes: int):
    """POSIX resource limits applied between fork and exec (best effort)."""
    if os.name != "posix" or resource is None:
        return None

    def apply():
        limits = [(resource.RLIMIT_CORE, 0),
                  # The wall-clock timeout is authoritative; CPU time is a backstop.
                  (resource.RLIMIT_CPU, int(math.ceil(timeout)) + 2),
                  (resource.RLIMIT_FSIZE, max_bytes + 4096)]
        if memory_mb:
            limits.append((resource.RLIMIT_AS, memory_mb * 1024 * 1024))
        for which, value in limits:
            try:
                _, hard = resource.getrlimit(which)
                if hard != resource.RLIM_INFINITY:
                    value = min(value, hard)
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                pass

    return apply


def _terminate(process) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)  # the child leads its own session
        else:
            process.kill()
    except OSError:
        process.kill()


def _describe_exit(code: int) -> str:
    if code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = str(-code)
        return f"was killed by {name} (CPU, memory or output limit?)"
    return f"exited with code {code}"


def run_bounded(command, *, cwd: Path, timeout: float, max_bytes: int = _MAX_BYTES,
                memory_mb: int = DEFAULT_MEMORY_MB, label: str = "runtime"):
    """Run ``command`` with a wall-clock timeout, bounded output and, on POSIX,
    CPU/memory/file-size limits, its own session and a scrubbed environment.

    This is resource containment, not a security sandbox: use a disposable
    machine or container for hostile input.
    """
    positive_timeout(timeout)
    positive_memory(memory_mb)
    # Files bound Python memory even when the child produces excessive output.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                env=_environment(), start_new_session=os.name == "posix",
                preexec_fn=_limiter(timeout, memory_mb, max_bytes))
        except OSError as exc:
            raise MoonVeilError(f"could not start {command[0]}: {exc}") from exc
        deadline = time.monotonic() + timeout
        try:
            while True:
                if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > max_bytes:
                    raise MoonVeilError(f"{label} output exceeded {max_bytes} bytes")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MoonVeilError(f"{label} exceeded the {timeout:g}s timeout")
                try:
                    process.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            _terminate(process)
            process.wait()
        if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > max_bytes:
            raise MoonVeilError(f"{label} output exceeded {max_bytes} bytes")
        stdout.seek(0)
        stderr.seek(0)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if process.returncode:
            raise MoonVeilError(f"{label} {_describe_exit(process.returncode)}: {err[-2000:] or out[-2000:]}".rstrip(": "))
        return out, err


def compile_source(source: str, luau: Path, timeout: float, memory_mb: int = DEFAULT_MEMORY_MB) -> Optional[bool]:
    """``True`` when luau-compile accepts ``source``; ``None`` when it is unavailable."""
    compiler = find_compiler(luau)
    if compiler is None:
        return None
    with tempfile.TemporaryDirectory(prefix="moonveil-compile-") as directory:
        path = Path(directory) / "recovered.luau"
        path.write_text(source, encoding="utf-8")
        run_bounded([str(compiler), "--text", str(path)], cwd=path.parent, timeout=timeout,
                    memory_mb=memory_mb, label="luau-compile")
    return True
