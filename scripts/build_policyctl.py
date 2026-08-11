#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import re
import zipfile
from pathlib import Path

import fastjsonschema
import yaml

from engineering_policy.repository import atomic_write_path

_FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def build(output: Path, repository_root: Path, *, version: str | None = None) -> Path:
    entries: dict[str, tuple[bytes, int]] = {
        "__main__.py": (
            b"from engineering_policy.cli import main\nraise SystemExit(main())\n",
            0o644,
        ),
        "LICENSE": (_normalize_newlines((repository_root / "LICENSE").read_bytes()), 0o644),
        "NOTICE": (_normalize_newlines((repository_root / "NOTICE").read_bytes()), 0o644),
    }
    _add_tree(entries, repository_root / "src/engineering_policy", "engineering_policy")
    if version is not None:
        init_name = "engineering_policy/__init__.py"
        source = entries[init_name][0]
        rendered, count = re.subn(
            rb'__version__ = "[^"]+"',
            f'__version__ = "{version}"'.encode(),
            source,
            count=1,
        )
        if count != 1:
            raise ValueError("engineering_policy version marker is missing or ambiguous")
        entries[init_name] = (rendered, 0o644)
    _add_tree(entries, Path(yaml.__file__).parent, "yaml", suffixes={".py"})
    _add_tree(entries, Path(fastjsonschema.__file__).parent, "fastjsonschema", suffixes={".py"})
    stream = io.BytesIO()
    stream.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, (content, mode) in sorted(entries.items()):
            info = zipfile.ZipInfo(name, _FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (mode & 0xFFFF) << 16
            archive.writestr(info, content)
    atomic_write_path(output, stream.getvalue(), 0o755)
    return output


def _normalize_newlines(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _add_tree(
    entries: dict[str, tuple[bytes, int]],
    source: Path,
    destination: str,
    *,
    suffixes: set[str] | None = None,
) -> None:
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        if path.is_symlink() or "__pycache__" in path.parts or path.suffix in {".pyc", ".so"}:
            continue
        if suffixes is not None and path.suffix not in suffixes:
            continue
        relative = path.relative_to(source).as_posix()
        entries[f"{destination}/{relative}"] = (_normalize_newlines(path.read_bytes()), 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist/policyctl.pyz"))
    parser.add_argument("--version")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    build(args.output.absolute(), root, version=args.version)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
