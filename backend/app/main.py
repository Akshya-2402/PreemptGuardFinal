import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import psutil
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.experiments import router as experiments_router
from app.api.llm import router as llm_router
from app.api.learning import router as learning_router
from app.api.monitoring import router as monitoring_router
from app.api.processes import router as processes_router
from app.api.settings import router as settings_router
from app.core.config import settings_store
from app.core.database import database
from app.monitoring.system_processes import system_process_monitor
from app.workloads.manager import process_manager

logger = logging.getLogger(__name__)


async def system_process_monitor_loop() -> None:
    while True:
        owned_pids = {
            child.pid
            for child in process_manager.children.values()
            if child.poll() is None
        }
        try:
            await asyncio.to_thread(system_process_monitor.refresh, owned_pids)
        except (psutil.Error, OSError, sqlite3.Error):
            logger.exception("Could not refresh the local system-process snapshot.")
        await asyncio.sleep(5.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    database.execute(
        """UPDATE managed_processes SET status='orphaned',ended_at=?
        WHERE status IN ('running','recovering')""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    stored = database.query("SELECT key,value FROM app_settings")
    if stored:
        updates = {}
        for row in stored:
            value: object = row["value"]
            if row["key"] in {
                "prediction_horizon", "max_restart_attempts", "max_operation_retries",
                "max_workload_memory_mb",
            }:
                value = int(value)
            elif row["key"] in {
                "monitoring_interval", "low_risk_threshold", "high_risk_threshold",
                "recheck_interval", "restart_cooldown", "verification_period",
                "minimum_prediction_confidence",
            }:
                value = float(value)
            updates[row["key"]] = value
        settings_store.update(updates)
    loop = asyncio.create_task(process_manager.monitor_loop())
    system_loop = asyncio.create_task(system_process_monitor_loop())
    try:
        yield
    finally:
        loop.cancel()
        system_loop.cancel()
        try:
            await loop
        except asyncio.CancelledError:
            pass
        try:
            await system_loop
        except asyncio.CancelledError:
            pass
        process_manager.stop_all()


app = FastAPI(
    title="PreemptGuard",
    description="Local managed-process monitoring and risk-aware recovery research lab.",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(processes_router)
app.include_router(monitoring_router)
app.include_router(settings_router)
app.include_router(experiments_router)
app.include_router(llm_router)
app.include_router(learning_router)
app.mount(
    "/assets",
    StaticFiles(
        directory=Path(__file__).resolve().parents[2] / "frontend" / "dist" / "assets",
        check_dir=False,
    ),
    name="dashboard-assets",
)


@app.get("/api/health")
def health():
    return {"status": "ok", "application": "PreemptGuard"}


@app.post("/api/history/reset")
def reset_history(confirm: bool = False):
    if not confirm:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="Set confirm=true to delete history.")
    if process_manager.children:
        from fastapi import HTTPException

        raise HTTPException(status_code=409, detail="Stop all managed workloads before clearing history.")
    with database.connect() as connection:
        for table in (
            "measurements", "predictions", "decisions", "recovery_attempts",
            "monitor_overhead", "experiments", "managed_processes",
            "host_process_samples", "host_recovery_feedback",
            "host_action_consents", "host_action_configs", "host_action_runs",
        ):
            connection.execute(f"DELETE FROM {table}")
    return {"status": "history cleared"}


@app.get("/")
def dashboard_page():
    index = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
    if not index.exists():
        return {
            "application": "PreemptGuard API",
            "dashboard": "Start Vite with `npm run dev` in frontend, or build it with `npm run build`.",
            "docs": "/docs",
        }
    return FileResponse(index)
