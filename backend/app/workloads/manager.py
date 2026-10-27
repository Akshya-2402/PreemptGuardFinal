import asyncio
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

from app.core.config import DATA_DIR, settings_store
from app.core.database import Database, database
from app.monitoring.features import extract_features
from app.prediction.risk_estimator import estimate_risk
from app.prediction.train_model import load_trained_model
from app.recovery.decision_engine import choose_action
from app.schemas import WorkloadRequest
from app.workloads.files import atomic_replace_with_retry

CPU_OVERLOAD_THRESHOLD_PERCENT = 35.0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProcessManager:
    def __init__(self, db: Database = database) -> None:
        self.db = db
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.process_handles: dict[str, psutil.Process] = {}
        self.heartbeat_paths: dict[str, Path] = {}
        self.heartbeat_cpu_samples: dict[str, tuple[float, float]] = {}
        self.control_paths: dict[str, Path] = {}
        self._lock = asyncio.Lock()

    def start(self, request: WorkloadRequest) -> dict[str, Any]:
        current_settings = settings_store.get()
        process_id = str(uuid.uuid4())
        policy = request.policy or current_settings.active_policy
        working_directory = Path(__file__).resolve().parents[2]
        heartbeat_dir = DATA_DIR / "workloads" / process_id
        heartbeat_dir.mkdir(parents=True, exist_ok=True)
        heartbeat_path = heartbeat_dir / "heartbeat.json"
        control_path = heartbeat_dir / "control.json"
        control_path.write_text("{}", encoding="utf-8")
        config = request.model_dump()
        config["policy"] = policy
        command = [
            sys.executable, "-m", "app.workloads.worker",
            "--workload-type", request.workload_type,
            "--heartbeat-path", str(heartbeat_path),
            "--duration-s", str(request.duration_s),
            "--fault-after-s", str(request.fault_after_s),
            "--memory-rate-mb-s", str(request.memory_rate_mb_s),
            "--memory-limit-mb", str(current_settings.max_workload_memory_mb),
            "--control-path", str(control_path),
        ]
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        child = subprocess.Popen(
            command,
            cwd=working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creation_flags,
        )
        now = utc_now()
        name = request.name or f"{request.workload_type.replace('_', ' ').title()}-{process_id[:6]}"
        self.db.execute(
            """INSERT INTO managed_processes
            (id,name,workload_type,pid,status,policy,config_json,started_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                process_id, name, request.workload_type, child.pid, "running",
                policy, json.dumps(config), now,
            ),
        )
        self.children[process_id] = child
        self.process_handles[process_id] = psutil.Process(child.pid)
        self.heartbeat_paths[process_id] = heartbeat_path
        self.heartbeat_cpu_samples.pop(process_id, None)
        self.control_paths[process_id] = control_path
        return self.get_process(process_id)

    def get_process(self, process_id: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM managed_processes WHERE id=?", (process_id,))
        if not rows:
            raise KeyError(process_id)
        row = rows[0]
        row["config"] = json.loads(row.pop("config_json"))
        row["managed"] = process_id in self.children
        return row

    def list_processes(self) -> list[dict[str, Any]]:
        ids = self.db.query("SELECT id FROM managed_processes ORDER BY started_at DESC")
        processes = []
        for row in ids:
            process = self.get_process(row["id"])
            latest = self.db.query(
                "SELECT * FROM measurements WHERE process_id=? ORDER BY timestamp DESC LIMIT 1",
                (row["id"],),
            )
            prediction = self.db.query(
                "SELECT * FROM predictions WHERE process_id=? ORDER BY timestamp DESC LIMIT 1",
                (row["id"],),
            )
            process["metrics"] = latest[0] if latest else None
            process["prediction"] = self._decode_prediction(prediction[0]) if prediction else None
            processes.append(process)
        return processes

    @staticmethod
    def _decode_prediction(prediction: dict[str, Any]) -> dict[str, Any]:
        prediction["features"] = json.loads(prediction.pop("features_json"))
        prediction["contributors"] = json.loads(prediction.pop("contributors_json"))
        return prediction

    def stop(self, process_id: str) -> dict[str, Any]:
        child = self.children.get(process_id)
        if child is None:
            raise KeyError(process_id)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        self._read_child_stderr(child)
        self.children.pop(process_id, None)
        self.process_handles.pop(process_id, None)
        self.heartbeat_cpu_samples.pop(process_id, None)
        self._mark_stopped(process_id, "stopped")
        return self.get_process(process_id)

    def stop_all(self) -> None:
        for process_id in list(self.children):
            try:
                self.stop(process_id)
            except (KeyError, OSError):
                continue

    def _mark_stopped(self, process_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE managed_processes SET status=?,ended_at=? WHERE id=?",
            (status, utc_now(), process_id),
        )

    def _record_monitor_overhead(self) -> None:
        monitor_process = psutil.Process()
        self.db.execute(
            "INSERT INTO monitor_overhead(timestamp,cpu_percent,memory_mb) VALUES(?,?,?)",
            (
                time.time(),
                monitor_process.cpu_percent(interval=None),
                monitor_process.memory_info().rss / (1024 * 1024),
            ),
        )

    def _launch_saved(self, process_id: str) -> subprocess.Popen[bytes]:
        row = self.db.query("SELECT * FROM managed_processes WHERE id=?", (process_id,))
        if not row:
            raise KeyError(process_id)
        config = json.loads(row[0]["config_json"])
        self.children.pop(process_id, None)
        return self._start_saved(process_id, row[0], config)

    def _start_saved(
        self, process_id: str, row: dict[str, Any], config: dict[str, Any]
    ) -> subprocess.Popen[bytes]:
        current_settings = settings_store.get()
        heartbeat_path = DATA_DIR / "workloads" / process_id / f"{uuid.uuid4()}.json"
        control_path = heartbeat_path.with_name(f"{heartbeat_path.stem}-control.json")
        control_path.write_text("{}", encoding="utf-8")
        command = [
            sys.executable, "-m", "app.workloads.worker",
            "--workload-type", config["workload_type"],
            "--heartbeat-path", str(heartbeat_path),
            "--duration-s", str(config["duration_s"]),
            "--fault-after-s", str(config["fault_after_s"]),
            "--memory-rate-mb-s", str(config["memory_rate_mb_s"]),
            "--memory-limit-mb", str(current_settings.max_workload_memory_mb),
            "--control-path", str(control_path),
        ]
        child = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parents[2],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        self.children[process_id] = child
        self.process_handles[process_id] = psutil.Process(child.pid)
        self.heartbeat_paths[process_id] = heartbeat_path
        self.heartbeat_cpu_samples.pop(process_id, None)
        self.control_paths[process_id] = control_path
        self.db.execute(
            """UPDATE managed_processes
            SET pid=?,status='running',started_at=?,ended_at=NULL WHERE id=?""",
            (child.pid, utc_now(), process_id),
        )
        return child

    async def sample_all(self) -> None:
        async with self._lock:
            self._record_monitor_overhead()
            for process_id, child in list(self.children.items()):
                try:
                    await self._sample_process(process_id, child)
                except (psutil.Error, OSError, ValueError, KeyError, RuntimeError) as exc:
                    self.db.execute(
                        "INSERT INTO decisions(process_id,timestamp,risk_score,confidence,action,reason,outcome) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (process_id, time.time(), 0.0, 0.0, "monitoring_error", str(exc), "logged"),
                    )

    async def _sample_process(
        self, process_id: str, child: subprocess.Popen[bytes]
    ) -> None:
        now = time.time()
        row = self.db.query(
            "SELECT * FROM managed_processes WHERE id=?", (process_id,)
        )[0]
        proc = self.process_handles.get(process_id) if child.poll() is None else None
        if proc is None and child.poll() is None:
            proc = psutil.Process(child.pid)
            self.process_handles[process_id] = proc
        worker_error = self._read_child_stderr(child) if child.poll() is not None else None
        if proc is None or not proc.is_running():
            self.process_handles.pop(process_id, None)
            self.db.execute(
                "UPDATE managed_processes SET status='failed',ended_at=? WHERE id=?",
                (utc_now(), process_id),
            )
            status = "failed"
            cpu = memory = memory_percent = None
            uptime = max(0.0, now - datetime.fromisoformat(row["started_at"]).timestamp())
        else:
            with proc.oneshot():
                cpu = proc.cpu_percent(interval=None)
                memory = proc.memory_info().rss / (1024 * 1024)
                memory_percent = proc.memory_percent()
                uptime = max(0.0, now - datetime.fromisoformat(row["started_at"]).timestamp())
            status = "running"
        heartbeat_path = self.heartbeat_paths.get(process_id)
        heartbeat_age = None
        operation_status = None
        heartbeat_cpu_time = None
        if heartbeat_path and heartbeat_path.exists():
            try:
                heartbeat_data = json.loads(heartbeat_path.read_text(encoding="utf-8"))
                heartbeat_time = float(heartbeat_data["timestamp"])
                heartbeat_age = max(0.0, now - heartbeat_time)
                operation_status = heartbeat_data.get("operation_status")
                worker_rss = heartbeat_data.get("rss_mb")
                worker_memory_percent = heartbeat_data.get("memory_percent")
                if (
                    isinstance(worker_rss, (int, float))
                    and not isinstance(worker_rss, bool)
                ):
                    memory = float(worker_rss)
                if (
                    isinstance(worker_memory_percent, (int, float))
                    and not isinstance(worker_memory_percent, bool)
                ):
                    memory_percent = float(worker_memory_percent)
                cpu_time = heartbeat_data.get("cpu_time_s")
                if isinstance(cpu_time, (int, float)) and not isinstance(cpu_time, bool):
                    heartbeat_cpu_time = float(cpu_time)
                    previous_cpu = self.heartbeat_cpu_samples.get(process_id)
                    if (
                        previous_cpu is not None
                        and heartbeat_time > previous_cpu[0]
                        and heartbeat_cpu_time >= previous_cpu[1]
                    ):
                        cpu = (
                            (heartbeat_cpu_time - previous_cpu[1])
                            / (heartbeat_time - previous_cpu[0])
                            * 100.0
                        )
                    if (
                        previous_cpu is None
                        or heartbeat_time > previous_cpu[0]
                    ):
                        self.heartbeat_cpu_samples[process_id] = (
                            heartbeat_time, heartbeat_cpu_time
                        )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                heartbeat_age = None
        responsive = bool(
            status == "running"
            and (
                heartbeat_age is not None and heartbeat_age <= 2.5
                or uptime <= 4.0
            )
        )
        previous = self.db.query(
            "SELECT timestamp,memory_mb FROM measurements WHERE process_id=? ORDER BY timestamp DESC LIMIT 1",
            (process_id,),
        )
        growth = 0.0
        if previous and memory is not None:
            duration = now - previous[0]["timestamp"]
            if duration > 0 and previous[0]["memory_mb"] is not None:
                growth = (memory - previous[0]["memory_mb"]) / duration
        recent_failures = self.db.query(
            """SELECT COUNT(*) AS count FROM measurements
            WHERE process_id=? AND process_state='failed' AND timestamp>=?""",
            (process_id, now - 30.0),
        )[0]["count"]
        recent_failures += int(status == "failed")
        self.db.execute(
            """INSERT INTO measurements
            (process_id,timestamp,cpu_percent,memory_mb,memory_percent,memory_growth_mb_s,
            process_state,responsive,heartbeat_age_s,uptime_s,recent_failure_count,recent_restart_count,
            operation_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                process_id, now, cpu, memory, memory_percent, growth,
                status, int(responsive), heartbeat_age, uptime,
                recent_failures, row["restart_count"], operation_status,
            ),
        )
        samples = self.db.query(
            "SELECT * FROM measurements WHERE process_id=? ORDER BY timestamp DESC LIMIT 10",
            (process_id,),
        )
        samples.reverse()
        features = extract_features(samples)
        cfg = settings_store.get()
        trained_model = None
        if cfg.prediction_model == "logistic_regression":
            trained_model = load_trained_model()
            if trained_model is None:
                raise RuntimeError(
                    "Logistic regression is selected but no trained model is available."
                )
            if int(trained_model["horizon_s"]) != cfg.prediction_horizon:
                raise RuntimeError(
                    "The selected model was trained for a different prediction horizon."
                )
        prediction = estimate_risk(
            process_id, features, cfg.prediction_horizon,
            cfg.low_risk_threshold, cfg.high_risk_threshold,
            failed=status == "failed",
            minimum_confidence=cfg.minimum_prediction_confidence,
            trained_model=trained_model,
        )
        self.db.execute(
            """INSERT INTO predictions
            (process_id,timestamp,risk_score,category,confidence,horizon_s,model_name,
            features_json,contributors_json,recommended_action,confidence_kind)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                process_id, now, prediction.risk_score, prediction.category,
                prediction.confidence, prediction.horizon_s, prediction.model_name,
                json.dumps(prediction.features), json.dumps(prediction.contributors),
                prediction.recommended_action, prediction.confidence_kind,
            ),
        )
        prior_high = int(row["consecutive_high_cpu"])
        recent_resource_samples = self.db.query(
            """SELECT cpu_percent,memory_growth_mb_s FROM measurements
            WHERE process_id=? ORDER BY timestamp DESC LIMIT 3""",
            (process_id,),
        )
        cpu_window = [
            float(item["cpu_percent"])
            for item in recent_resource_samples
            if item["cpu_percent"] is not None
        ]
        smoothed_cpu = (
            sum(cpu_window) / len(cpu_window) if cpu_window else 0.0
        )
        cpu_high_now = (
            len(cpu_window) == 3
            and smoothed_cpu >= CPU_OVERLOAD_THRESHOLD_PERCENT
        )
        consecutive = prior_high + 1 if cpu_high_now else 0
        self.db.execute(
            "UPDATE managed_processes SET consecutive_high_cpu=? WHERE id=?",
            (consecutive, process_id),
        )
        recheck_due = row["next_recheck_at"] is not None and now >= row["next_recheck_at"]
        missed = uptime > 4.0 and (heartbeat_age is None or heartbeat_age > 4.0)
        sustained_memory_growth = sum(
            float(item["memory_growth_mb_s"] or 0.0) >= 2.0
            for item in recent_resource_samples
        ) == 3
        threshold_detected = (
            consecutive >= 3 or sustained_memory_growth or missed or status == "failed"
        )
        decision = choose_action(
            policy=row["policy"],
            risk_score=prediction.risk_score,
            confidence=prediction.confidence,
            low_threshold=cfg.low_risk_threshold,
            high_threshold=cfg.high_risk_threshold,
            responsive=responsive,
            failed=status == "failed",
            restart_count=row["restart_count"],
            restart_limit=cfg.max_restart_attempts,
            recheck_due=recheck_due,
            threshold_detected=threshold_detected,
            confirmed_unresponsive=missed and status == "running",
            minimum_confidence=cfg.minimum_prediction_confidence,
            workload_type=row["workload_type"],
            operation_status=operation_status,
            operation_retry_count=row["retry_count"],
            max_operation_retries=cfg.max_operation_retries,
            cpu_overloaded=consecutive >= 3,
            throttled=bool(self._read_control(process_id).get("throttle", False)),
        )
        decision_reason = decision.reason
        if worker_error:
            decision_reason += f" Worker error: {worker_error[:2000]}"
        if decision.action == "recheck":
            if row["next_recheck_at"] is None or recheck_due:
                self.db.execute(
                    "UPDATE managed_processes SET next_recheck_at=? WHERE id=?",
                    (now + cfg.recheck_interval, process_id),
                )
        elif decision.action != "continue monitoring":
            self.db.execute(
                "UPDATE managed_processes SET next_recheck_at=NULL WHERE id=?",
                (process_id,),
            )
        self.db.execute(
            """INSERT INTO decisions
            (process_id,timestamp,risk_score,confidence,action,reason,estimated_cost,evidence_json,outcome)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                process_id, now, prediction.risk_score, prediction.confidence,
                decision.action, decision_reason, decision.estimated_cost,
                json.dumps(prediction.features), None,
            ),
        )
        if decision.action == "restart":
            try:
                await self.restart(process_id, decision_reason)
            except RuntimeError as exc:
                self.db.execute(
                    """UPDATE decisions SET outcome=? WHERE id=(
                    SELECT id FROM decisions WHERE process_id=? ORDER BY id DESC LIMIT 1)""",
                    (f"recovery deferred: {exc}", process_id),
                )
                self.db.execute(
                    "UPDATE managed_processes SET next_recheck_at=? WHERE id=?",
                    (now + cfg.recheck_interval, process_id),
                )
        elif decision.action == "investigate":
            child = self.children.get(process_id)
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            if child is not None:
                worker_error = self._read_child_stderr(child)
                if worker_error:
                    self._set_latest_outcome(
                        process_id, f"worker error: {worker_error[:2000]}"
                    )
            self.db.execute(
                "UPDATE managed_processes SET status='investigate',ended_at=? WHERE id=?",
                (utc_now(), process_id),
            )
            self.children.pop(process_id, None)
            self.process_handles.pop(process_id, None)
            self.heartbeat_cpu_samples.pop(process_id, None)
        elif decision.action == "retry":
            self._write_control(
                process_id,
                {"retry_count": int(row["retry_count"]) + 1},
            )
            self.db.execute(
                "UPDATE managed_processes SET retry_count=retry_count+1 WHERE id=?",
                (process_id,),
            )
            self._set_latest_outcome(process_id, "retry_requested")
        elif decision.action == "throttle":
            self._write_control(process_id, {"throttle": True})
            self._set_latest_outcome(process_id, "throttle_applied")
        elif recheck_due and decision.action == "continue monitoring":
            self.db.execute(
                "UPDATE managed_processes SET next_recheck_at=NULL WHERE id=?",
                (process_id,),
            )

    def _read_control(self, process_id: str) -> dict[str, Any]:
        path = self.control_paths.get(process_id)
        if path is None or not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_control(self, process_id: str, changes: dict[str, Any]) -> None:
        path = self.control_paths.get(process_id)
        if path is None:
            raise RuntimeError("Managed worker control channel is unavailable.")
        controls = self._read_control(process_id)
        controls.update(changes)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(controls), encoding="utf-8")
        atomic_replace_with_retry(temporary, path)

    @staticmethod
    def _read_child_stderr(child: subprocess.Popen[bytes]) -> str | None:
        if (
            child.stderr is None
            or child.stderr.closed
            or child.poll() is None
        ):
            return None
        try:
            detail = child.stderr.read().decode("utf-8", errors="replace").strip()
        finally:
            child.stderr.close()
        return detail or None

    def _set_latest_outcome(self, process_id: str, outcome: str) -> None:
        self.db.execute(
            """UPDATE decisions SET outcome=? WHERE id=(
            SELECT id FROM decisions WHERE process_id=? ORDER BY id DESC LIMIT 1)""",
            (outcome, process_id),
        )

    async def restart(self, process_id: str, reason: str = "Manual restart requested.") -> dict[str, Any]:
        row = self.db.query("SELECT * FROM managed_processes WHERE id=?", (process_id,))
        if not row:
            raise KeyError(process_id)
        process = row[0]
        cfg = settings_store.get()
        now = time.time()
        last_restart = process["last_restart_at"]
        if process["restart_count"] >= cfg.max_restart_attempts:
            raise RuntimeError("Maximum restart attempts reached; automatic recovery is disabled.")
        if last_restart is not None and now - last_restart < cfg.restart_cooldown:
            raise RuntimeError("Restart cooldown is active for this managed process.")
        child = self.children.get(process_id)
        old_pid = child.pid if child else process["pid"]
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
        if child is not None:
            old_worker_error = self._read_child_stderr(child)
            if old_worker_error:
                reason += f" Previous worker error: {old_worker_error[:2000]}"
        self.children.pop(process_id, None)
        self.process_handles.pop(process_id, None)
        self.db.execute(
            """UPDATE managed_processes SET restart_count=restart_count+1,
            last_restart_at=?,status='recovering',retry_count=0,next_recheck_at=NULL
            WHERE id=?""",
            (now, process_id),
        )
        start = time.monotonic()
        new_child = self._launch_saved(process_id)
        deadline = time.monotonic() + cfg.verification_period
        heartbeat_samples = 0
        last_heartbeat: float | None = None
        ever_responsive = False
        recurring_failure = False
        while time.monotonic() < deadline:
            self._record_monitor_overhead()
            path = self.heartbeat_paths[process_id]
            if new_child.poll() is not None:
                recurring_failure = True
            replacement_error = (
                self._read_child_stderr(new_child)
                if new_child.poll() is not None else None
            )
            if replacement_error:
                reason += f" Replacement worker error: {replacement_error[:2000]}"
                break
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    heartbeat_time = float(data["timestamp"])
                    fresh = time.time() - heartbeat_time <= 1.5
                    if fresh:
                        ever_responsive = True
                        if heartbeat_time != last_heartbeat:
                            heartbeat_samples += 1
                            last_heartbeat = heartbeat_time
                    elif ever_responsive:
                        recurring_failure = True
                        break
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    pass
            await asyncio.sleep(0.2)
        if new_child.poll() is not None:
            recurring_failure = True
        if path.exists() and not recurring_failure:
            try:
                final_data = json.loads(path.read_text(encoding="utf-8"))
                final_age = time.time() - float(final_data["timestamp"])
                if final_age > 1.5 and ever_responsive:
                    recurring_failure = True
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                pass
        verified = (
            not recurring_failure
            and new_child.poll() is None
            and ever_responsive
            and heartbeat_samples >= 2
        )
        status = (
            "healthy" if verified else
            "failed" if recurring_failure else
            "inconclusive"
        )
        if not verified:
            if new_child.poll() is None:
                new_child.terminate()
                try:
                    new_child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    new_child.kill()
                    new_child.wait(timeout=3)
            replacement_error = self._read_child_stderr(new_child)
            if replacement_error:
                reason += f" Replacement worker error: {replacement_error[:2000]}"
            self.children.pop(process_id, None)
            self.db.execute(
                "UPDATE managed_processes SET status=?,ended_at=? WHERE id=?",
                ("recovery_failed" if status == "failed" else "recovery_inconclusive",
                 utc_now(), process_id),
            )
        else:
            self.db.execute(
                "UPDATE managed_processes SET status='running' WHERE id=?",
                (process_id,),
            )
        duration = time.monotonic() - start
        self.db.execute(
            """INSERT INTO recovery_attempts
            (process_id,timestamp,attempt_number,old_pid,new_pid,status,duration_s,
            reason,verification_period_s)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                process_id, time.time(), process["restart_count"] + 1,
                old_pid, new_child.pid, status, duration, reason,
                cfg.verification_period,
            ),
        )
        self.db.execute(
            """UPDATE decisions SET outcome=? WHERE id=(
            SELECT id FROM decisions WHERE process_id=? AND outcome IS NULL
            ORDER BY id DESC LIMIT 1)""",
            (status, process_id),
        )
        return {
            "process_id": process_id,
            "old_pid": old_pid,
            "new_pid": new_child.pid,
            "status": status,
            "duration_s": round(duration, 3),
            "reason": reason,
        }

    async def monitor_loop(self) -> None:
        while True:
            await self.sample_all()
            await asyncio.sleep(settings_store.get().monitoring_interval)


process_manager = ProcessManager()
