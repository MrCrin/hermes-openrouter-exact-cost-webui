#!/usr/bin/env python3
"""Register a manually-installed WebUI extension in its install manifest.

Why this exists
---------------
The Hermes WebUI builds its **Installed** list from
``extension-install-manifest.json`` (see ``_gallery_installed_runtime_manifest``
in the WebUI's ``api/extensions.py``), not by scanning the extensions directory.
Its gallery installer writes an entry for everything it installs; a folder copied
in by hand has no entry, so it never appears — no matter that the files are all
in the right place.

This writes the identical entry shape the installer produces::

    {"version": <from extension.json>, "files": [<paths relative to the ext dir>],
     "installed_at": <iso8601>}

It backs the manifest up first and re-reads the result before claiming success.

Usage
-----
    python3 register_extension.py [EXTENSION_DIR]

``EXTENSION_DIR`` defaults to the directory containing this script. The extension
id is read from that directory's ``extension.json``.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# The WebUI refuses manifests above this size; fail here rather than corrupt it.
_MAX_MANIFEST_BYTES = 128 * 1024


def main(argv: list[str]) -> int:
    ext_dir = Path(argv[1]).expanduser().resolve() if len(argv) > 1 else Path(__file__).resolve().parent

    descriptor = ext_dir / "extension.json"
    if not ext_dir.is_dir():
        print(f"error: extension directory not found: {ext_dir}", file=sys.stderr)
        return 1
    if not descriptor.is_file():
        print(f"error: {descriptor} not found — is this an extension directory?", file=sys.stderr)
        return 1

    meta = json.loads(descriptor.read_text(encoding="utf-8"))
    ext_id = meta.get("id")
    if not ext_id:
        print("error: extension.json has no 'id'", file=sys.stderr)
        return 1
    version = meta.get("version", "unknown")

    # The state dir holds the manifest; extensions live in ./extensions beneath it,
    # so the extension's parent's parent is where the manifest belongs.
    state_dir = ext_dir.parent.parent
    manifest_path = state_dir / "extension-install-manifest.json"

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"error: {manifest_path} is not valid JSON: {exc}", file=sys.stderr)
            return 1
        backup = manifest_path.with_suffix(".json.bak-register")
        if not backup.exists():
            shutil.copy2(manifest_path, backup)
            print(f"backed up -> {backup}")
    else:
        manifest = {"version": 1, "installed": {}}

    files = sorted(
        p.relative_to(ext_dir).as_posix()
        for p in ext_dir.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )

    manifest.setdefault("version", 1)
    manifest.setdefault("installed", {})
    manifest["installed"][ext_id] = {
        "version": version,
        "files": files,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }

    encoded = json.dumps(manifest, ensure_ascii=False, indent=2)
    if len(encoded.encode("utf-8")) > _MAX_MANIFEST_BYTES:
        print("error: result would exceed the WebUI's manifest size limit — aborting", file=sys.stderr)
        return 1

    manifest_path.write_text(encoded, encoding="utf-8")
    print(f"registered {ext_id} v{version} with {len(files)} files:")
    for name in files:
        print(f"  {name}")

    checked = json.loads(manifest_path.read_text(encoding="utf-8")).get("installed", {})
    print(f"\nre-read OK: {len(checked)} installed extension(s) -> {sorted(checked)}")
    if checked.get(ext_id, {}).get("version") != version:
        print("warning: re-read did not contain the entry as written", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
