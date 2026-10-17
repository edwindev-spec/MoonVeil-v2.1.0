"""Install the pinned official Luau release after verifying its SHA-256."""
from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import sys
import urllib.request
import zipfile
from pathlib import Path

VERSION = "0.722"
ASSETS = {
    "Linux": ("luau-ubuntu.zip", "db7af020f6a712be1609f71a43b5add8f13ca3ca021282b462ebd2f5484e91fa"),
    "Darwin": ("luau-macos.zip", "94cc44f050a4f89f17ecee0ad6475098f58dad19f3629d17bf996cd41eec9f24"),
    "Windows": ("luau-windows.zip", "e4959151b92ed4f8d822588dea0f3eb72e4b96d5568b2eea32a0a8452488c82b"),
}


def install(directory: Path) -> None:
    system = platform.system()
    if system not in ASSETS:
        raise ValueError(f"unsupported operating system: {system}")
    if system != "Darwin" and platform.machine().lower() not in {"x86_64", "amd64"}:
        raise ValueError("this release requires x86-64; build official Luau from source on this architecture")
    asset, checksum = ASSETS[system]
    url = f"https://github.com/luau-lang/luau/releases/download/{VERSION}/{asset}"
    with urllib.request.urlopen(url, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != checksum:
        raise ValueError("Luau release checksum mismatch; no files were installed")
    suffix = ".exe" if system == "Windows" else ""
    names = ("luau" + suffix, "luau-compile" + suffix)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        # Read only known filenames. No archive paths are extracted.
        tools = {name: bundle.read(name) for name in names}
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in tools.items():
        target = directory / name
        target.write_bytes(content)
        if os.name != "nt":
            target.chmod(0o755)
    print(f"Installed official Luau {VERSION} in {directory.resolve()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parent.parent / ".tools" / f"luau-{VERSION}")
    args = parser.parse_args()
    try:
        install(args.directory.expanduser())
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
