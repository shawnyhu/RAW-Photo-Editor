"""Run the installed 1.1.1 release tests with Blender's bundled Python."""
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = "1.1.1"
COMMIT = "4effe368557965ba374d4be7b0566c16b321ec99"
INSTALLER = ROOT / "raw_photo_editor-1.1.1.zip"
SHA256 = "3690cca8ecee03bae2b8f7a75e4809309d052947843782fbf12de7996b49f038"


def step(report, directory, name, command, env, marker):
    print(f"Running {name}...", flush=True)
    log = directory / (name + ".log")
    start = time.monotonic()
    record = {"name": name, "log": log.name, "status": "FAIL"}
    report["steps"].append(record)
    try:
        with log.open("wb") as stream:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=1200, check=False)
        record["exit_code"] = result.returncode
        if result.returncode:
            raise RuntimeError(f"Blender exited with {result.returncode}")
        if marker and marker.encode() not in log.read_bytes():
            raise RuntimeError(f"Missing completion message: {marker}")
        record["status"] = "PASS"
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        record["error"] = str(error)
    record["seconds"] = round(time.monotonic() - start, 2)
    print(f"{record['status']}  {name}", flush=True)
    return record["status"] == "PASS"


def suite(blender, output, scratch, report):
    # Blender's more specific path overrides take precedence over USER_RESOURCES.
    env = {key: value for key, value in os.environ.items() if not key.startswith("BLENDER_USER_")}
    profile = scratch / "profile"
    profile.mkdir()
    presets = scratch / "presets"
    presets.mkdir()
    env.update(BLENDER_USER_RESOURCES=str(profile), PYTHONUNBUFFERED="1",
               RPE_RUNTIME_INFO=str(output / "runtime.json"))
    base = [str(blender), "-b", "--offline-mode"]
    setup = base + ["--factory-startup", "--command", "extension", "install-file",
                    "--repo", "user_default", "--enable", str(INSTALLER)]
    if not step(report, output, "install", setup, env, ""):
        return
    execute = base + ["--python-exit-code", "1", "--python"]
    if not step(report, output, "verify-install", execute + [str(ROOT / "verify_install.py")],
                env, "PASS isolated 1.1.1 install"):
        return
    report["runtime"] = json.loads((output / "runtime.json").read_text(encoding="utf-8"))
    cases = [
        ("stock-switch", "spektrafilm_stock_defaults", ["--pixels"], "PASS installed Film recommendation acceptance with GPU pixels"),
        ("native-export", "native_install_smoke", ["--require-gpu"], "PASS installed ZIP"),
        ("film-menu", "spektrafilm_stock_picker_ui", ["--installed", "--require-bw"], "PASS stock picker target/compatibility"),
        ("personal-settings", "spektrafilm_user_settings", [], "PASS Lite user settings"),
        ("calibration", "spektrafilm_calibration", [], "PASS native Portra-to-Velvia"),
        ("processing", "spektrafilm_processing", ["--installed", "--pixels"], "PASS actual GPU changes"),
        ("preset-write", "spektrafilm_user_preset_flow", ["write", str(presets)], "PASS installed Spektrafilm Lite user preset library write"),
        ("preset-read", "spektrafilm_user_preset_flow", ["use", str(presets)], "PASS installed Spektrafilm Lite user preset library use"),
    ]
    for name, script, args, marker in cases:
        command = execute + [str(ROOT / "tests" / (script + ".py"))]
        if args:
            command.extend(["--", *args])
        step(report, output, name, command, env, marker)


def main():
    blender, output = map(Path, sys.argv[1:])
    report = {"status": "FAIL", "version": VERSION, "commit": COMMIT,
              "os": platform.platform(), "architecture": platform.machine(),
              "started_utc": datetime.now(timezone.utc).isoformat(), "steps": []}
    try:
        if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
            raise RuntimeError("Windows x64 is required for this acceptance report")
        with INSTALLER.open("rb") as stream:
            report["installer_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        if report["installer_sha256"] != SHA256:
            raise RuntimeError("1.1.1 installer SHA-256 does not match the Mac-tested ZIP")
        with tempfile.TemporaryDirectory(prefix="rpe111-", ignore_cleanup_errors=True) as folder:
            suite(blender, output, Path(folder), report)
        if report["steps"] and all(s["status"] == "PASS" for s in report["steps"]):
            report["status"] = "PASS"
    except Exception as error:
        report["error"] = str(error)
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    path = output / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{report['status']}  {path}", flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
