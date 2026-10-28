import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import psutil

from app.core.database import Database, database
from app.online_learning import RECOVERY_ACTIONS, process_features

PROTECTED_PROCESS_NAMES = {
    "csrss.exe", "lsass.exe", "services.exe", "smss.exe", "system",
    "wininit.exe", "winlogon.exe",
}
class HostRecoveryError(RuntimeError):
    pass


def _process(pid: int, process_started_at: float) -> psutil.Process:
    if pid in {0, 4, os.getpid()}:
        raise HostRecoveryError("This critical or PreemptGuard process cannot be controlled.")
    try:
        if any(
            child.pid == pid
            for child in psutil.Process(os.getpid()).children(recursive=False)
        ):
            raise HostRecoveryError(
                "PreemptGuard-owned workers use their separate managed recovery controls."
            )
        process = psutil.Process(pid)
        if abs(process.create_time() - process_started_at) > 0.01:
            raise HostRecoveryError("The PID now belongs to a different process.")
        name = process.name().lower()
    except psutil.NoSuchProcess as exc:
        raise HostRecoveryError("The selected process is no longer running.") from exc
    except psutil.AccessDenied as exc:
        raise HostRecoveryError("Windows denied access to the selected process.") from exc
    if not process.is_running():
        raise HostRecoveryError("The selected process is no longer running.")
    if name in PROTECTED_PROCESS_NAMES:
        raise HostRecoveryError("PreemptGuard will not control this protected Windows process.")
    return process


def _process_if_running(pid: int, process_started_at: float) -> psutil.Process | None:
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - process_started_at) > 0.01:
            raise HostRecoveryError("The PID now belongs to a different process.")
        if not process.is_running():
            return None
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise HostRecoveryError("Windows denied access to the selected process.") from exc
    return _process(pid, process_started_at)


def _configuration(
    db: Database,
    pid: int,
    process_started_at: float,
) -> dict[str, Any]:
    rows = db.query(
        """SELECT config_json FROM host_action_configs
        WHERE pid=? AND process_started_at=?""",
        (pid, process_started_at),
    )
    return json.loads(rows[0]["config_json"]) if rows else {}


def _save_configuration(
    db: Database,
    pid: int,
    process_started_at: float,
    config: dict[str, Any],
) -> None:
    db.execute(
        """INSERT INTO host_action_configs(pid,process_started_at,config_json,created_at)
        VALUES(?,?,?,?) ON CONFLICT(pid,process_started_at)
        DO UPDATE SET config_json=excluded.config_json,created_at=excluded.created_at""",
        (pid, process_started_at, json.dumps(config), time.time()),
    )


def configure_restart(
    *,
    pid: int,
    process_started_at: float,
    executable: str,
    arguments: list[str],
    working_directory: str | None,
    db: Database = database,
) -> None:
    _process(pid, process_started_at)
    executable_path = Path(executable).expanduser().resolve()
    if not executable_path.is_absolute() or not executable_path.is_file():
        raise ValueError("Restart executable must be an existing file with an absolute path.")
    cwd = None
    if working_directory:
        cwd_path = Path(working_directory).expanduser().resolve()
        if not cwd_path.is_dir():
            raise ValueError("Restart working directory must be an existing directory.")
        cwd = str(cwd_path)
    config = _configuration(db, pid, process_started_at)
    config["restart"] = {
        "executable": str(executable_path),
        "arguments": arguments,
        "working_directory": cwd,
    }
    _save_configuration(db, pid, process_started_at, config)


def _latest_sample(
    db: Database,
    pid: int,
    process_started_at: float,
) -> dict[str, Any]:
    rows = db.query(
        """SELECT * FROM host_process_samples
        WHERE pid=? AND process_started_at=? ORDER BY sampled_at DESC LIMIT 1""",
        (pid, process_started_at),
    )
    if not rows:
        raise HostRecoveryError("No current process sample is available.")
    return rows[0]


def _record_run(
    db: Database,
    *,
    pid: int,
    process_started_at: float,
    process_name: str,
    action: str,
    status: str,
    details: dict[str, Any],
    features: dict[str, float],
    retry_of: int | None = None,
    duration_s: float | None = None,
) -> int:
    return db.execute(
        """INSERT INTO host_action_runs
        (pid,process_started_at,process_name,action,status,details,features_json,retry_of,duration_s,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            pid,
            process_started_at,
            process_name,
            action,
            status,
            json.dumps(details),
            json.dumps(features, separators=(",", ":")),
            retry_of,
            duration_s,
            time.time(),
        ),
    )


def _execute_action(
    *,
    process: psutil.Process,
    action: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if action == "resume":
        if process.status() != psutil.STATUS_STOPPED:
            raise HostRecoveryError("The selected process is no longer stopped.")
        process.resume()
        return {"message": "Resume requested for the stopped process."}
    if action == "lower_priority":
        original = int(process.nice())
        if config.get("original_priority") is None:
            config["original_priority"] = original
        if original == psutil.BELOW_NORMAL_PRIORITY_CLASS:
            return {"message": "Process priority is already below normal."}
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        return {
            "message": "Process priority lowered to below normal.",
            "original_priority": config["original_priority"],
        }
    if action == "restore_priority":
        original = config.get("original_priority")
        if original is None:
            raise HostRecoveryError("No saved original priority is available to restore.")
        process.nice(int(original))
        return {"message": "Original process priority restored."}
    if action == "terminate":
        process.terminate()
        return {"message": "Termination requested. The process may lose unsaved work."}
    if action == "restart_configured":
        launch = config.get("restart")
        if not isinstance(launch, dict):
            raise HostRecoveryError("Configure an executable and arguments before restart.")
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except psutil.TimeoutExpired as exc:
            raise HostRecoveryError(
                "The old process did not exit within five seconds; replacement was not launched."
            ) from exc
        return _launch_configured(launch)
    raise HostRecoveryError("Unsupported recovery action.")


def _launch_configured(launch: dict[str, Any]) -> dict[str, Any]:
    executable = launch.get("executable")
    arguments = launch.get("arguments")
    if not isinstance(executable, str) or not isinstance(arguments, list):
        raise HostRecoveryError("Saved restart configuration is invalid.")
    child = subprocess.Popen(
        [executable, *[str(item) for item in arguments]],
        cwd=launch.get("working_directory"),
        shell=False,
        close_fds=True,
    )
    return {"message": "Configured replacement process launched.", "new_pid": child.pid}


def _consent_exists(
    db: Database,
    pid: int,
    process_started_at: float,
    action: str,
) -> bool:
    return bool(db.query(
        """SELECT 1 FROM host_action_consents
        WHERE pid=? AND process_started_at=? AND action=?""",
        (pid, process_started_at, action),
    ))


def execute_recovery(
    *,
    pid: int,
    process_started_at: float,
    action: str,
    allow_action: bool = False,
    remember_action: bool = True,
    db: Database = database,
) -> dict[str, Any]:
    if action not in RECOVERY_ACTIONS:
        raise ValueError("Unsupported recovery action.")
    sample = _latest_sample(db, pid, process_started_at)
    if not _consent_exists(db, pid, process_started_at, action):
        if not allow_action:
            raise PermissionError("This action requires your explicit first-time approval.")
        if remember_action:
            db.execute(
                """INSERT OR IGNORE INTO host_action_consents
                (pid,process_started_at,action,created_at) VALUES(?,?,?,?)""",
                (pid, process_started_at, action, time.time()),
            )

    retry_of = None
    target_action = action
    if action == "retry":
        failed = db.query(
            """SELECT id,action FROM host_action_runs
            WHERE pid=? AND process_started_at=? AND status='failed'
            ORDER BY created_at DESC LIMIT 1""",
            (pid, process_started_at),
        )
        if not failed:
            raise HostRecoveryError("There is no failed recovery action to retry.")
        retry_of = int(failed[0]["id"])
        already_retried = db.query(
            "SELECT 1 FROM host_action_runs WHERE retry_of=? LIMIT 1",
            (retry_of,),
        )
        if already_retried:
            raise HostRecoveryError("This failed recovery has already been retried once.")
        target_action = str(failed[0]["action"])
        if target_action == "retry":
            raise HostRecoveryError("A retry cannot retry another retry.")
        if not _consent_exists(db, pid, process_started_at, target_action):
            raise PermissionError("The original recovery action has not been approved.")
        process = (
            _process_if_running(pid, process_started_at)
            if target_action == "restart_configured"
            else _process(pid, process_started_at)
        )
        if process is None and target_action != "restart_configured":
            raise HostRecoveryError("The selected process is no longer running.")
    else:
        process = _process(pid, process_started_at)

    config = _configuration(db, pid, process_started_at)
    features = process_features(sample)
    started_at = time.perf_counter()
    try:
        if process is None:
            launch = config.get("restart")
            if not isinstance(launch, dict):
                raise HostRecoveryError("Configure an executable and arguments before retrying restart.")
            details = _launch_configured(launch)
        else:
            details = _execute_action(
                process=process,
                action=target_action,
                config=config,
            )
        _save_configuration(db, pid, process_started_at, config)
        status = "requested"
        result = details
    except (HostRecoveryError, psutil.Error, OSError, subprocess.SubprocessError) as exc:
        status = "failed"
        details = {"error": str(exc)}
        result = details
    duration_s = time.perf_counter() - started_at
    run_id = _record_run(
        db,
        pid=pid,
        process_started_at=process_started_at,
        process_name=sample["name"],
        action=action,
        status=status,
        details=details,
        features=features,
        retry_of=retry_of,
        duration_s=duration_s,
    )
    if status == "failed":
        raise HostRecoveryError(result["error"])
    return {
        "run_id": run_id,
        "action": action,
        "status": status,
        **result,
    }
