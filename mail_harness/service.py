from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "com.mail-harness.backlog"
PROJECT_DIR = Path(__file__).resolve().parent.parent
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = PROJECT_DIR / "logs" / "harness.log"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=check)


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            "Автозапуск через launchd есть только на macOS.\n"
            "На Windows создай задачу в Планировщике заданий: при входе в систему запускать\n"
            f"  {sys.executable} -m mail_harness -c <config> serve\n"
            f"с рабочей папкой {PROJECT_DIR}"
        )


def install(config: Path, port: int, live: bool) -> None:
    _require_macos()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, "-m", "mail_harness", "-c", str(config.resolve()), "serve", "--port", str(port)]
    if live:
        args.append("--live")
    plist = {
        "Label": LABEL,
        "ProgramArguments": args,
        "WorkingDirectory": str(PROJECT_DIR),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    with PLIST.open("wb") as f:
        plistlib.dump(plist, f)
    result = _launchctl("bootstrap", _domain(), str(PLIST))
    if result.returncode != 0:
        raise SystemExit(f"launchctl bootstrap: {result.stderr.strip() or result.stdout.strip()}")
    print(f"Установлено и запущено: {LABEL}")
    print(f"  режим:     {'БОЕВОЙ (--live)' if live else 'dry-run'}")
    print(f"  бэклог:    http://127.0.0.1:{port}/")
    print(f"  лог:       {LOG}")
    print(f"  автозапуск при входе в систему, перезапуск при падении")


def uninstall() -> None:
    _require_macos()
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    if PLIST.exists():
        PLIST.unlink()
    print(f"Остановлено и удалено из автозапуска: {LABEL}")


def status() -> None:
    _require_macos()
    result = _launchctl("print", f"{_domain()}/{LABEL}")
    if result.returncode != 0:
        print("Не установлено (python3 -m mail_harness service install)")
        return
    info = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in ("state", "pid", "last exit code"):
            info.setdefault(key, value)
    print(f"{LABEL}: {info.get('state', '?')}" + (f", pid {info['pid']}" if "pid" in info else ""))
    if "last exit code" in info:
        print(f"  последний код выхода: {info['last exit code']}")
    print(f"  лог: {LOG}")
