import json
import time
from threading import Lock
from typing import Any

import psutil

from app.core.database import Database, database

class SystemProcessMonitor:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._previous: dict[tuple[int, float], tuple[float, float, int]] = {}
        self._logical_cpu_count = psutil.cpu_count(logical=True) or 1
        self._total_memory = psutil.virtual_memory().total
        self._refresh_lock = Lock()
        self._snapshot_lock = Lock()
        self._snapshot: dict[str, Any] = {
            "processes": [],
            "sampled_at": None,
            "refreshing": False,
        }

    def refresh(self, owned_pids: set[int] | None = None) -> None:
        with self._refresh_lock:
            with self._snapshot_lock:
                self._snapshot["refreshing"] = True
            now = time.time()
            owned = owned_pids or set()
            current: dict[tuple[int, float], tuple[float, float, int]] = {}
            processes: list[dict[str, Any]] = []
            persisted_samples: list[tuple[Any, ...]] = []
            try:
                for process in psutil.process_iter(
                    attrs=[
                        "pid", "name", "create_time", "status",
                        "cpu_percent", "memory_info",
                    ],
                    ad_value=None,
                ):
                    info = process.info
                    pid = int(info["pid"])
                    name = info.get("name") or f"Process {pid}"
                    create_time = info.get("create_time")
                    state = info.get("status")
                    cpu_percent = info.get("cpu_percent")
                    memory = info.get("memory_info")
                    if create_time is None or state is None:
                        limited = self._limited_process(pid, "access_denied", pid in owned)
                        processes.append(limited)
                        persisted_samples.append(self._sample_values(
                            limited, now, None
                        ))
                        continue

                    key = (pid, float(create_time))
                    memory_mb = memory.rss / (1024 * 1024) if memory else None
                    memory_percent = (
                        memory.rss / self._total_memory * 100.0 if memory else None
                    )
                    previous = self._previous.get(key)
                    growth_mb_s = None
                    high_cpu_samples = 0
                    normalized_cpu = (
                        float(cpu_percent) / self._logical_cpu_count
                        if cpu_percent is not None else None
                    )
                    if previous is not None:
                        elapsed, previous_memory_mb, high_cpu_samples = previous
                        sample_duration = now - elapsed
                        if sample_duration > 0 and memory_mb is not None:
                            growth_mb_s = (memory_mb - previous_memory_mb) / sample_duration
                        high_cpu_samples = (
                            high_cpu_samples + 1
                            if normalized_cpu is not None and normalized_cpu >= 50.0
                            else 0
                        )
                    if memory_mb is not None:
                        current[key] = (now, memory_mb, high_cpu_samples)

                    assessment = assess_process(
                        state=state,
                        cpu_percent=float(cpu_percent) if cpu_percent is not None else None,
                        normalized_cpu_percent=normalized_cpu,
                        memory_mb=memory_mb,
                        memory_percent=memory_percent,
                        growth_mb_s=growth_mb_s,
                        high_cpu_samples=high_cpu_samples,
                    )
                    if cpu_percent is None or memory is None:
                        assessment.update({
                            "health": "Limited access",
                            "risk_evidence": assessment["risk_evidence"] + [
                                "Some process statistics are not accessible"
                            ],
                            "recommended_action": (
                                "Some statistics are unavailable at current permissions; "
                                "no action was taken."
                            ),
                            "recovery_method": (
                                "Review the owning application; do not force-close "
                                "system processes."
                            ),
                        })
                    process_sample = {
                        "pid": pid,
                        "name": name,
                        "state": state,
                        "process_started_at": float(create_time),
                        "cpu_percent": cpu_percent,
                        "memory_mb": memory_mb,
                        "memory_percent": memory_percent,
                        "growth_mb_s": growth_mb_s,
                        "managed_by_preemptguard": pid in owned,
                        **assessment,
                    }
                    processes.append(process_sample)
                    persisted_samples.append(self._sample_values(
                        process_sample, now, float(create_time)
                    ))

                processes.sort(
                    key=lambda item: (
                        item["risk_score"] if item["risk_score"] is not None else -1.0,
                        item["cpu_percent"] or 0.0,
                        item["memory_mb"] or 0.0,
                    ),
                    reverse=True,
                )
                if self._db is not None and persisted_samples:
                    self._db.execute_many(
                        """INSERT INTO host_process_samples
                        (pid,process_started_at,sampled_at,name,state,cpu_percent,memory_mb,
                        memory_percent,growth_mb_s,risk_score,risk_category,health,
                        risk_evidence_json,recommended_action,recovery_method)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        persisted_samples,
                    )
                self._previous = current
                with self._snapshot_lock:
                    self._snapshot.update({
                        "processes": processes,
                        "sampled_at": now,
                    })
            finally:
                with self._snapshot_lock:
                    self._snapshot["refreshing"] = False

    def snapshot(self) -> dict[str, Any]:
        with self._snapshot_lock:
            return {
                "processes": list(self._snapshot["processes"]),
                "sampled_at": self._snapshot["sampled_at"],
                "refreshing": self._snapshot["refreshing"],
            }

    @staticmethod
    def _sample_values(
        process: dict[str, Any],
        sampled_at: float,
        process_started_at: float | None,
    ) -> tuple[Any, ...]:
        return (
            process["pid"],
            process_started_at,
            sampled_at,
            process["name"],
            process["state"],
            process["cpu_percent"],
            process["memory_mb"],
            process["memory_percent"],
            process["growth_mb_s"],
            process["risk_score"],
            process["risk_category"],
            process["health"],
            json.dumps(process["risk_evidence"]),
            process["recommended_action"],
            process["recovery_method"],
        )

    @staticmethod
    def _limited_process(pid: int, state: str, owned: bool) -> dict[str, Any]:
        if state == "zombie":
            health = "Zombie process"
            action = "Inspect the parent application; the parent must reap this process."
        else:
            health = "Limited access"
            action = "Some statistics are unavailable at current permissions; no action was taken."
        return {
            "pid": pid,
            "name": f"Process {pid}",
            "state": state,
            "cpu_percent": None,
            "memory_mb": None,
            "memory_percent": None,
            "growth_mb_s": None,
            "managed_by_preemptguard": owned,
            "health": health,
            "risk_score": None,
            "risk_category": "Unknown",
            "risk_evidence": [health],
            "recommended_action": action,
            "recovery_method": (
                "Review the owning application; do not force-close system processes."
            ),
        }


def assess_process(
    *,
    state: str,
    cpu_percent: float | None,
    normalized_cpu_percent: float | None,
    memory_mb: float | None,
    memory_percent: float | None,
    growth_mb_s: float | None,
    high_cpu_samples: int,
) -> dict[str, Any]:
    evidence: list[str] = []
    risk = 0.0
    normalized_cpu = normalized_cpu_percent or 0.0
    memory_use = memory_percent or 0.0
    growth = growth_mb_s or 0.0

    if normalized_cpu >= 50.0:
        risk += 0.4
        evidence.append("High CPU use relative to total machine capacity")
    elif normalized_cpu >= 25.0:
        risk += 0.2
        evidence.append("Elevated CPU use")

    if memory_use >= 10.0:
        risk += 0.4
        evidence.append("High share of system memory in use")
    elif memory_use >= 5.0:
        risk += 0.2
        evidence.append("Elevated memory use")

    if growth >= 5.0:
        risk += 0.35
        evidence.append("Rapidly growing resident memory")
    elif growth >= 1.0:
        risk += 0.2
        evidence.append("Resident memory is growing")

    if state == psutil.STATUS_ZOMBIE:
        risk += 0.5
        evidence.append("Process is in zombie state")
    elif state == psutil.STATUS_STOPPED:
        risk += 0.2
        evidence.append("Process is stopped or suspended")

    risk = min(1.0, risk)
    if state == psutil.STATUS_ZOMBIE:
        health = "Zombie process"
    elif state == psutil.STATUS_STOPPED:
        health = "Stopped or suspended"
    elif risk >= 0.7:
        health = "High resource pressure"
    elif risk >= 0.3:
        health = "Elevated resource use"
    else:
        health = "Normal"

    if state == psutil.STATUS_ZOMBIE:
        recommended_action = "Inspect the parent application; the parent must reap this process."
        recovery_method = (
            "Resolve or restart the parent application gracefully; terminating the zombie "
            "itself cannot release its process-table entry."
        )
    elif state == psutil.STATUS_STOPPED:
        recommended_action = "Check whether this process was intentionally paused."
        recovery_method = (
            "If it should be active, resume it from its owning application. If it is "
            "unresponsive, save work where possible and use the application's normal restart."
        )
    elif growth >= 1.0:
        recommended_action = (
            "Investigate the memory trend; save work before considering a graceful restart."
        )
        recovery_method = (
            "Check the application's own memory diagnostics and close unused work. "
            "If growth continues or the app becomes unresponsive, save data and restart "
            "the application gracefully."
        )
    elif memory_use >= 10.0:
        recommended_action = "Review memory use and save work before changing this process."
        recovery_method = (
            "Close unused documents, tabs, or tasks in the owning application; restart it "
            "gracefully only if memory pressure persists."
        )
    elif normalized_cpu >= 25.0:
        if high_cpu_samples >= 3:
            recommended_action = "Sustained CPU load detected; inspect the active workload."
            recovery_method = (
                "Reduce parallel work or pause the task in its application. If it is "
                "unresponsive, save what you can and use that application's normal restart."
            )
        else:
            recommended_action = "Observe the next samples; a short CPU burst may be normal."
            recovery_method = (
                "No immediate recovery is indicated. If high CPU persists, reduce or pause "
                "the workload using its application."
            )
    else:
        recommended_action = "No recovery needed; continue monitoring."
        recovery_method = (
            "No action indicated by these samples. For an application-specific failure, "
            "save work and use the application's normal close/reopen procedure."
        )

    return {
        "health": health,
        "risk_score": round(risk, 3),
        "risk_category": (
            "Critical" if risk >= 0.85 else
            "High" if risk >= 0.65 else
            "Moderate" if risk >= 0.3 else "Low"
        ),
        "risk_evidence": evidence or ["No elevated resource signal in this sample"],
        "recommended_action": recommended_action,
        "recovery_method": recovery_method,
        "normalized_cpu_percent": normalized_cpu_percent,
    }


system_process_monitor = SystemProcessMonitor(database)
