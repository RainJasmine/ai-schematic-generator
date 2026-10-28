"""Create an allowlisted PCM archive using only the Python standard library."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import zipfile


def package(root: Path, output: Path | None = None) -> Path:
    root = root.resolve()
    metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    version = metadata["versions"][0]["version"]
    output = output or root / "dist" / f"rf-eda-ai-schematic-{version}.zip"
    output = output.resolve()
    paths = [root / name for name in ("metadata.json", "README.md", "CHANGELOG.md", "plugins/plugin.json", "plugins/requirements.txt")]
    paths.append(root / "docs/手动更新与清空图页接口.md")
    for folder in ("plugins", "resources"):
        for path in (root / folder).rglob("*"):
            relative = path.relative_to(root)
            if (path.is_file() and "__pycache__" not in relative.parts
                    and relative.parts[:2] != ("plugins", "tests")
                    and path.suffix.lower() in {".py", ".png", ".svg", ".proto"}):
                paths.append(path)
    required = ("workflow.py", "snapshot.py", "session_store.py", "management_ui.py", "requirements_ui.py")
    if any(not (root / "plugins/ai_schematic" / name).is_file() for name in required):
        raise ValueError("Missing required plugin modules")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(set(paths)):
                if path.is_symlink() or not path.is_file():
                    raise ValueError(f"Invalid package source: {path.name}")
                archive.write(path, path.relative_to(root).as_posix())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("Archive integrity check failed")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(package(Path(__file__).resolve().parents[1], args.output))
