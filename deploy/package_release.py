"""Build a code-only update archive; never collect runtime data or credentials."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
files = [
    root / name
    for name in (
        "bot.py",
        "config.py",
        "manage.py",
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "README.md",
    )
]
for folder in ("app", "services", "tests"):
    files.extend((root / folder).rglob("*.py"))
files.extend((root / "app" / "locales").glob("*.json"))
files.extend(
    root / "deploy" / name for name in ("UPGRADE.md", "LINUX.md", "package_release.py")
)
destination = root / "dist" / "tautopbot-update.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for path in sorted(set(files)):
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != root.parent
        ):
            raise ValueError(f"Symlinks are not packaged: {path.name}")
        archive.write(path, path.relative_to(root).as_posix())
print(
    f"Created {destination.name}: {len(files)} code/resource files; no database or environment files"
)
