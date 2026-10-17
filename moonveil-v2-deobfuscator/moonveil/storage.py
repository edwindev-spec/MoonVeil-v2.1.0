"""Atomic output replacement and protection of input/output aliases."""

import json
import os
import tempfile
from pathlib import Path

from .errors import MoonVeilError
from .source import MAX_SOURCE_BYTES


def read_source(path: Path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise MoonVeilError(f"input exceeds {MAX_SOURCE_BYTES} bytes")
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MoonVeilError("input must contain UTF-8 Lua source") from exc
    return raw, source


def _same(first: Path, second: Path):
    return first.resolve() == second.resolve() or (first.exists() and second.exists() and os.path.samefile(first, second))


def check_destinations(source: Path, destinations):
    checked = []
    for destination in destinations:
        if _same(source, destination):
            raise MoonVeilError(f"refusing to overwrite the input: {destination}")
        if destination.is_dir():
            raise MoonVeilError(f"output is a directory: {destination}")
        for other in checked:
            if _same(other, destination) or other.resolve() in destination.resolve().parents or destination.resolve() in other.resolve().parents:
                raise MoonVeilError("output and artifact paths overlap")
        for ancestor in destination.parents:
            if ancestor.exists() and not ancestor.is_dir():
                raise MoonVeilError(f"output parent is not a directory: {ancestor}")
            if _same(source, ancestor):
                raise MoonVeilError("artifact directory overlaps the input")
        checked.append(destination)


def write_text(path: Path, value: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".moonveil-", delete=False) as stream:
            name = stream.name
            stream.write(value)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
