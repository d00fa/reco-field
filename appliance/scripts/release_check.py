#!/usr/bin/env python3
"""Keep appliance changes tied to a new package version and release record."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION_PATH = "appliance/VERSION"


def native_files(root: Path) -> dict[str, str]:
    paths = [root / "Cargo.toml", root / "Cargo.lock"]
    paths.extend(p for p in (root / "crates").rglob("*") if p.is_file() and
                 (p.name == "Cargo.toml" or p.suffix in {".rs", ".wgsl", ".cpp", ".h", ".hpp", ".c"}))
    paths.append(root / "resources/profiles.cbor.gz")
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes() if p.suffix == ".gz" else p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for p in sorted(paths)}


def write_native_manifest(binary: Path, output: Path) -> None:
    value = {"binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "files": native_files(ROOT)}
    output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def check(base: str | None = None) -> str:
    version = (ROOT / VERSION_PATH).read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("appliance/VERSION must contain a numeric major.minor.patch version")
    for path, pattern in (("appliance/pyproject.toml", r'^version\s*=\s*"([^"]+)"'),
                          ("appliance/reco_field/__init__.py", r'^__version__\s*=\s*"([^"]+)"')):
        match = re.search(pattern, (ROOT / path).read_text(), re.MULTILINE)
        if not match or match[1] != version:
            raise ValueError(f"{path} must match appliance/VERSION ({version})")
    changelog = (ROOT / "docs/RECO-FIELD-CHANGELOG.md").read_text()
    if not re.search(rf"^## .*\b{re.escape(version)}\b", changelog, re.MULTILINE):
        raise ValueError(f"Add a {version} entry to docs/RECO-FIELD-CHANGELOG.md")
    if base:
        old = subprocess.run(["git", "show", f"{base}:{VERSION_PATH}"], cwd=ROOT, text=True, capture_output=True)
        changed = subprocess.check_output(["git", "diff", "--name-only", base, "--", "appliance", "crates", "resources", "Cargo.lock", "Cargo.toml", ".github/workflows/reco-field-package.yml"], cwd=ROOT, text=True).splitlines()
        relevant = [p for p in changed if not p.endswith((".md", ".txt")) or p.endswith(("requirements.txt", "requirements-dev.txt"))]
        if old.returncode == 0 and relevant and tuple(map(int, version.split("."))) <= tuple(map(int, old.stdout.strip().split("."))):
            raise ValueError("Runtime or packaging changed: increase appliance/VERSION, app/pyproject versions and the changelog before merging")
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="Git base revision for pull-request release enforcement")
    parser.add_argument("--native-binary", type=Path, help="Record native source provenance immediately after compiling this binary")
    parser.add_argument("--native-manifest-output", type=Path)
    args = parser.parse_args()
    try:
        print(f"Reco Field package version {check(args.base)} is consistent.")
        if args.native_binary:
            if not args.native_manifest_output:
                raise ValueError("--native-manifest-output is required with --native-binary")
            write_native_manifest(args.native_binary, args.native_manifest_output)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Release check failed: {exc}\n")
