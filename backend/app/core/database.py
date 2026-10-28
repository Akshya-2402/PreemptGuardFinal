import json
import sqlite3
from contextlib import contextmanager
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Iterator

from app.core.config import DATABASE_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_processes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    workload_type TEXT NOT NULL,
    pid INTEGER,
    status TEXT NOT NULL,
    policy TEXT NOT NULL,
    config_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    restart_count INTEGER NOT NULL DEFAULT 0,
    last_restart_at REAL,
    consecutive_high_cpu INTEGER NOT NULL DEFAULT 0,
    next_recheck_at REAL,
    retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    cpu_percent REAL,
    memory_mb REAL,
    memory_percent REAL,
    memory_growth_mb_s REAL,
    process_state TEXT NOT NULL,
    responsive INTEGER NOT NULL,
    heartbeat_age_s REAL,
    uptime_s REAL,
    recent_failure_count INTEGER NOT NULL DEFAULT 0,
    recent_restart_count INTEGER NOT NULL DEFAULT 0,
    operation_status TEXT,
    FOREIGN KEY(process_id) REFERENCES managed_processes(id)
);
CREATE INDEX IF NOT EXISTS idx_measurements_process_time
    ON measurements(process_id, timestamp);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    risk_score REAL NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL,
    confidence_kind TEXT NOT NULL DEFAULT 'history_coverage_not_probability',
    horizon_s INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    features_json TEXT NOT NULL,
    contributors_json TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    FOREIGN KEY(process_id) REFERENCES managed_processes(id)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    risk_score REAL NOT NULL,
    confidence REAL NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    estimated_cost REAL NOT NULL DEFAULT 0.0,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT,
    FOREIGN KEY(process_id) REFERENCES managed_processes(id)
);
CREATE TABLE IF NOT EXISTS recovery_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    attempt_number INTEGER NOT NULL,
    old_pid INTEGER,
    new_pid INTEGER,
    status TEXT NOT NULL,
    duration_s REAL,
    reason TEXT NOT NULL,
    verification_period_s REAL,
    FOREIGN KEY(process_id) REFERENCES managed_processes(id)
);
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    configuration_json TEXT NOT NULL,
    status TEXT NOT NULL,
    results_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS monitor_overhead (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    cpu_percent REAL,
    memory_mb REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_monitor_overhead_time ON monitor_overhead(timestamp);
CREATE TABLE IF NOT EXISTS host_process_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid INTEGER NOT NULL,
    process_started_at REAL,
    sampled_at REAL NOT NULL,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    cpu_percent REAL,
    memory_mb REAL,
    memory_percent REAL,
    growth_mb_s REAL,
    risk_score REAL,
    risk_category TEXT NOT NULL,
    health TEXT NOT NULL,
    risk_evidence_json TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    recovery_method TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_process_samples_identity_time
    ON host_process_samples(pid, process_started_at, sampled_at);
CREATE TABLE IF NOT EXISTS host_recovery_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid INTEGER NOT NULL,
    process_started_at REAL NOT NULL,
    process_name TEXT NOT NULL,
    action TEXT NOT NULL,
    helped INTEGER CHECK(helped IS NULL OR helped IN (0,1)),
    action_run_id INTEGER NOT NULL UNIQUE,
    features_json TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_recovery_feedback_action_time
    ON host_recovery_feedback(action, created_at);
CREATE TABLE IF NOT EXISTS host_action_consents (
    pid INTEGER NOT NULL,
    process_started_at REAL NOT NULL,
    action TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(pid, process_started_at, action)
);
CREATE TABLE IF NOT EXISTS host_action_configs (
    pid INTEGER NOT NULL,
    process_started_at REAL NOT NULL,
    config_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(pid, process_started_at)
);
CREATE TABLE IF NOT EXISTS host_action_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pid INTEGER NOT NULL,
    process_started_at REAL NOT NULL,
    process_name TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT NOT NULL,
    features_json TEXT NOT NULL,
    retry_of INTEGER,
    duration_s REAL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_host_action_runs_identity_time
    ON host_action_runs(pid, process_started_at, created_at);
"""


class Database:
    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            feedback_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(host_recovery_feedback)")
            }
            if "action_run_id" not in feedback_columns:
                connection.execute(
                    "ALTER TABLE host_recovery_feedback ADD COLUMN action_run_id INTEGER"
                )
                connection.execute(
                    "CREATE UNIQUE INDEX idx_host_recovery_feedback_action_run "
                    "ON host_recovery_feedback(action_run_id)"
                )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(decisions)")
            }
            if "estimated_cost" not in columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN estimated_cost REAL NOT NULL DEFAULT 0.0"
                )
            measurement_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(measurements)")
            }
            if "recent_failure_count" not in measurement_columns:
                connection.execute(
                    "ALTER TABLE measurements ADD COLUMN recent_failure_count INTEGER NOT NULL DEFAULT 0"
                )
            if "recent_restart_count" not in measurement_columns:
                connection.execute(
                    "ALTER TABLE measurements ADD COLUMN recent_restart_count INTEGER NOT NULL DEFAULT 0"
                )
            if "operation_status" not in measurement_columns:
                connection.execute("ALTER TABLE measurements ADD COLUMN operation_status TEXT")
            host_action_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(host_action_runs)")
            }
            if "duration_s" not in host_action_columns:
                connection.execute(
                    "ALTER TABLE host_action_runs ADD COLUMN duration_s REAL"
                )
            prediction_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(predictions)")
            }
            if "confidence_kind" not in prediction_columns:
                connection.execute(
                    "ALTER TABLE predictions ADD COLUMN confidence_kind TEXT NOT NULL "
                    "DEFAULT 'history_coverage_not_probability'"
                )
            process_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(managed_processes)")
            }
            if "retry_count" not in process_columns:
                connection.execute(
                    "ALTER TABLE managed_processes ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )
            decision_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(decisions)")
            }
            if "evidence_json" not in decision_columns:
                connection.execute(
                    "ALTER TABLE decisions ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '{}'"
                )
            recovery_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(recovery_attempts)")
            }
            if "verification_period_s" not in recovery_columns:
                connection.execute(
                    "ALTER TABLE recovery_attempts ADD COLUMN verification_period_s REAL"
                )
            connection.execute(
                "DELETE FROM app_settings WHERE key='prediction_model'"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, tuple(parameters))
            return int(cursor.lastrowid or 0)

    def query(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(sql, tuple(parameters)).fetchall()
            return [dict(row) for row in rows]

    def execute_many(self, sql: str, parameters: Iterable[Iterable[Any]]) -> None:
        with self.connect() as connection:
            connection.executemany(sql, parameters)

    def store_json(self, value: Any) -> str:
        return json.dumps(value, separators=(",", ":"), allow_nan=False)


database = Database()
