import json

from fastapi import APIRouter

from app.core.database import database
from app.monitoring.system_processes import system_process_monitor
from app.workloads.manager import process_manager

router = APIRouter(prefix="/api", tags=["monitoring"])


@router.get("/system/processes")
def get_system_processes():
    return system_process_monitor.snapshot()


@router.get("/system/processes/{pid}/history")
def get_system_process_history(
    pid: int,
    process_started_at: float | None = None,
    limit: int = 60,
):
    if pid < 0:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="pid must be non-negative.")
    if not 1 <= limit <= 2000:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="limit must be between 1 and 2000.")
    if process_started_at is None:
        rows = database.query(
            """SELECT * FROM host_process_samples WHERE pid=?
            ORDER BY sampled_at DESC LIMIT ?""",
            (pid, limit),
        )
    else:
        rows = database.query(
            """SELECT * FROM host_process_samples
            WHERE pid=? AND process_started_at=?
            ORDER BY sampled_at DESC LIMIT ?""",
            (pid, process_started_at, limit),
        )
    rows.reverse()
    for row in rows:
        row["risk_evidence"] = json.loads(row.pop("risk_evidence_json"))
    return rows


@router.get("/overview")
def get_overview():
    processes = process_manager.list_processes()
    latest_predictions = [
        item["prediction"] for item in processes if item.get("prediction")
    ]
    recoveries = database.query("SELECT status,duration_s FROM recovery_attempts")
    failures = database.query(
        "SELECT COUNT(*) AS count FROM measurements WHERE process_state='failed'"
    )[0]["count"]
    risks = [item["risk_score"] for item in latest_predictions]
    return {
        "total_processes": len(processes),
        "healthy_processes": sum(
            bool(
                item.get("metrics")
                and item["metrics"].get("responsive")
                and item["status"] == "running"
            )
            for item in processes
        ),
        "moderate_or_high_risk": sum(score >= 0.35 for score in risks),
        "confirmed_failures": failures,
        "successful_recoveries": sum(item["status"] == "healthy" for item in recoveries),
        "unnecessary_restarts": None,
        "average_recovery_time_s": (
            sum(item["duration_s"] for item in recoveries if item["duration_s"] is not None)
            / max(1, sum(item["duration_s"] is not None for item in recoveries))
            if recoveries else None
        ),
        "measured_downtime_s": sum(
            item["duration_s"] or 0 for item in recoveries
        ),
        "recent_measurements": database.query(
            """SELECT m.* FROM measurements m JOIN
            (SELECT process_id,MAX(timestamp) AS max_time FROM measurements GROUP BY process_id) latest
            ON m.process_id=latest.process_id AND m.timestamp=latest.max_time
            ORDER BY m.timestamp DESC LIMIT 50"""
        ),
    }


@router.get("/decisions")
def get_decisions(limit: int = 200, process_id: str | None = None):
    if not 1 <= limit <= 1000:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000.")
    if process_id:
        return database.query(
            """SELECT d.*,p.name AS process_name FROM decisions d JOIN managed_processes p
            ON p.id=d.process_id WHERE d.process_id=? ORDER BY d.timestamp DESC LIMIT ?""",
            (process_id, limit),
        )
    return database.query(
        """SELECT d.*,p.name AS process_name FROM decisions d JOIN managed_processes p
        ON p.id=d.process_id ORDER BY d.timestamp DESC LIMIT ?""",
        (limit,),
    )


@router.get("/recovery")
def get_recoveries(limit: int = 200):
    return database.query(
        """SELECT r.*,p.name AS process_name FROM recovery_attempts r JOIN managed_processes p
        ON p.id=r.process_id ORDER BY r.timestamp DESC LIMIT ?""",
        (limit,),
    )


@router.get("/predictions")
def get_predictions(limit: int = 200, process_id: str | None = None):
    if not 1 <= limit <= 1000:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="limit must be between 1 and 1000.")
    if process_id:
        rows = database.query(
            "SELECT * FROM predictions WHERE process_id=? ORDER BY timestamp DESC LIMIT ?",
            (process_id, limit),
        )
    else:
        rows = database.query(
            """SELECT * FROM predictions ORDER BY timestamp DESC LIMIT ?""",
            (limit,),
        )
    for row in rows:
        row["features"] = __import__("json").loads(row.pop("features_json"))
        row["contributors"] = __import__("json").loads(row.pop("contributors_json"))
    return rows


@router.get("/monitoring/overhead")
def get_monitoring_overhead(limit: int = 300):
    if not 1 <= limit <= 2000:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail="limit must be between 1 and 2000.")
    return database.query(
        "SELECT * FROM monitor_overhead ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    )
