from pathlib import Path
from threading import RLock

from pydantic import BaseModel, Field


class Settings(BaseModel):
    monitoring_interval: float = Field(default=1.0, ge=0.25, le=30.0)
    prediction_horizon: int = Field(default=30, ge=1, le=600)
    low_risk_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    high_risk_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    recheck_interval: float = Field(default=3.0, ge=0.5, le=60.0)
    restart_cooldown: float = Field(default=10.0, ge=0.0, le=3600.0)
    max_restart_attempts: int = Field(default=3, ge=0, le=20)
    max_operation_retries: int = Field(default=2, ge=0, le=10)
    verification_period: float = Field(default=3.0, ge=0.5, le=60.0)
    max_workload_memory_mb: int = Field(default=64, ge=1, le=256)
    active_policy: str = Field(
        default="preemptguard", pattern="^(fixed|threshold|preemptguard)$"
    )
    minimum_prediction_confidence: float = Field(default=0.55, ge=0.0, le=1.0)


class SettingsStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._settings = Settings()

    def get(self) -> Settings:
        with self._lock:
            return self._settings.model_copy()

    def update(self, changes: dict[str, object]) -> Settings:
        with self._lock:
            self._settings = Settings.model_validate(
                {**self._settings.model_dump(), **changes}
            )
            return self._settings.model_copy()


settings_store = SettingsStore()
DATA_DIR = Path(__file__).resolve().parents[3] / ".data"
DATABASE_PATH = Path(__import__("os").environ.get(
    "PREEMPTGUARD_DB", str(DATA_DIR / "preemptguard.sqlite3")
))
