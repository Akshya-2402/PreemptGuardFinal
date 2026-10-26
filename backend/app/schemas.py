from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import Settings


class WorkloadRequest(BaseModel):
    workload_type: Literal[
        "healthy", "cpu", "cpu_memory", "memory", "unresponsive",
        "crash", "intermittent", "repeated_failure", "temporary_io",
    ]
    name: str | None = Field(default=None, max_length=80)
    policy: Literal[
        "fixed", "threshold", "preemptguard",
        "predictive_fixed", "confidence_recheck",
    ] | None = None
    duration_s: int = Field(default=120, ge=5, le=3600)
    memory_rate_mb_s: float = Field(default=1.0, ge=0.1, le=16.0)
    fault_after_s: int = Field(default=15, ge=3, le=1800)


class SettingsUpdate(BaseModel):
    monitoring_interval: float | None = Field(default=None, ge=0.25, le=30.0)
    prediction_horizon: int | None = Field(default=None, ge=1, le=600)
    low_risk_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    high_risk_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    recheck_interval: float | None = Field(default=None, ge=0.5, le=60.0)
    restart_cooldown: float | None = Field(default=None, ge=0.0, le=3600.0)
    max_restart_attempts: int | None = Field(default=None, ge=0, le=20)
    max_operation_retries: int | None = Field(default=None, ge=0, le=10)
    verification_period: float | None = Field(default=None, ge=0.5, le=60.0)
    max_workload_memory_mb: int | None = Field(default=None, ge=1, le=256)
    active_policy: Literal["fixed", "threshold", "preemptguard"] | None = None
    prediction_model: Literal["rule_based", "logistic_regression"] | None = None
    minimum_prediction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


def validate_settings_update(changes: SettingsUpdate) -> Settings:
    updates = changes.model_dump(exclude_none=True)
    from app.core.config import settings_store

    candidate = Settings.model_validate({**settings_store.get().model_dump(), **updates})
    if candidate.low_risk_threshold >= candidate.high_risk_threshold:
        raise ValueError("low_risk_threshold must be less than high_risk_threshold")
    return candidate
