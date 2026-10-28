from typing import Literal

from pydantic import BaseModel, Field, model_validator

from app.core.config import Settings


class SimulationBaseline(BaseModel):
    pid: int = Field(ge=0)
    name: str = Field(max_length=256)
    process_started_at: float | None = Field(default=None, ge=0.0)
    cpu_percent: float | None = Field(default=None, ge=0.0)
    memory_mb: float | None = Field(default=None, ge=0.0)
    memory_percent: float | None = Field(default=None, ge=0.0)
    growth_mb_s: float | None = None


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
    simulation: bool = False
    simulation_baseline: SimulationBaseline | None = None

    @model_validator(mode="after")
    def validate_simulation_baseline(self):
        if self.simulation and self.simulation_baseline is None:
            raise ValueError("simulation_baseline is required for a simulation worker.")
        if not self.simulation and self.simulation_baseline is not None:
            raise ValueError("simulation_baseline is only valid for a simulation worker.")
        return self


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
    minimum_prediction_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class HostRecoveryRequest(BaseModel):
    action: Literal[
        "resume", "lower_priority", "restore_priority", "terminate", "restart_configured", "retry",
    ]
    process_started_at: float = Field(ge=0.0, allow_inf_nan=False)
    allow_action: bool = False
    remember_action: bool = True


class HostRestartConfigRequest(BaseModel):
    process_started_at: float = Field(ge=0.0, allow_inf_nan=False)
    executable: str = Field(min_length=1, max_length=4096)
    arguments: list[str] = Field(default_factory=list, max_length=64)
    working_directory: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_restart_arguments(self):
        if any(len(argument) > 4096 for argument in self.arguments):
            raise ValueError("Restart arguments must be 4096 characters or fewer.")
        return self


class HostRecoveryFeedback(BaseModel):
    action_run_id: int = Field(gt=0)
    helped: bool | None


def validate_settings_update(changes: SettingsUpdate) -> Settings:
    updates = changes.model_dump(exclude_none=True)
    from app.core.config import settings_store

    candidate = Settings.model_validate({**settings_store.get().model_dump(), **updates})
    if candidate.low_risk_threshold >= candidate.high_risk_threshold:
        raise ValueError("low_risk_threshold must be less than high_risk_threshold")
    return candidate
