"""Opt-in Linux filesystem isolation; never mounts the bot's data or .env."""

import os
import shutil
import sys
from pathlib import Path


def worker_command(folder, environment):
    command = [sys.executable, "-m", "app.download_worker"]
    project = Path(__file__).resolve().parents[1]
    mode = os.getenv("DOWNLOAD_SANDBOX", "off")
    if mode == "off":
        return command, project
    if mode != "required" or sys.platform != "linux" or not shutil.which("bwrap"):
        raise ValueError("DOWNLOAD_SANDBOX=required needs Linux and bubblewrap")
    folder = Path(folder).resolve()
    code = folder / "worker-code"
    code.mkdir(exist_ok=True)
    # Copy only code and locales, never credentials, database files or symlinks.
    for package in ("app", "services"):
        for file in (project / package).rglob("*"):
            if (
                file.is_file()
                and not file.is_symlink()
                and (
                    file.suffix == ".py"
                    or (file.suffix == ".json" and "locales" in file.parts)
                )
                and "__pycache__" not in file.parts
            ):
                destination = code / file.relative_to(project)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, destination)
    shutil.copyfile(project / "config.py", code / "config.py")
    wrapper = [
        shutil.which("bwrap"),
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--cap-drop",
        "ALL",
    ]
    # Keep network access for downloads; Python public-IP restrictions still apply.
    # This filesystem boundary is not an independent network firewall.
    for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64"):
        if Path(path).exists():
            if project.is_relative_to(Path(path).resolve()):
                raise ValueError(
                    "Project must be outside system runtime mounts for sandbox isolation"
                )
            wrapper += ["--ro-bind", path, path]
    prefix = Path(sys.prefix).resolve()
    if not str(prefix).startswith("/usr/") and prefix != Path("/usr"):
        if prefix == Path("/") or project.is_relative_to(prefix):
            raise ValueError("Unsafe Python prefix for worker sandbox")
        wrapper += ["--ro-bind", str(prefix), str(prefix)]
    wrapper += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/etc"]
    for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl"):
        if Path(path).exists():
            wrapper += ["--ro-bind", path, path]
    wrapper += ["--bind", str(folder), str(folder)]
    for key in ("FFMPEG_PATH", "DENO_PATH", "DOWNLOAD_COOKIES_FILE"):
        value = environment.get(key)
        if value:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"Missing configured worker file: {key}")
            environment[key] = str(path)
            wrapper += ["--ro-bind", str(path), str(path)]
    environment["HOME"] = str(folder)
    wrapper += ["--chdir", str(code), "--", *command]
    return wrapper, code
