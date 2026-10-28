import asyncio
import csv
import io
import json
import random
import time
import uuid
from datetime import datetime, timezone
from statistics import mean, pstdev

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.core.config import settings_store
from app.core.database import database
from app.experiments.metrics import summarize_run
from app.schemas import WorkloadRequest
from app.workloads.manager import process_manager

router = APIRouter(prefix="/api/experiments", tags=["experiments"])
FAILURE_WORKLOAD_TYPES = {"crash", "repeated_failure", "unresponsive", "intermittent"}
SUMMARY_METRICS = (
    "precision",
    "recall",
    "f1",
    "false_positive_rate",
    "first_early_warning_lead_time_s",
    "recovery_success_rate",
    "mean_recovery_time_s",
    "median_recovery_time_s",
    "total_downtime_s",
    "unnecessary_restarts",
    "cpu_overhead_percent",
    "memory_overhead_mb",
)


def summarize_policies(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    groups = list(dict.fromkeys(
        (str(row["policy"]), str(row.get("scenario", "measured")))
        for row in rows
    ))
    for policy, scenario in groups:
        policy_runs = [
            row for row in rows
            if row["policy"] == policy
            and str(row.get("scenario", "measured")) == scenario
        ]
        metrics: dict[str, object] = {}
        for name in SUMMARY_METRICS:
            values = [
                float(run["metrics"][name])
                for run in policy_runs
                if run["metrics"].get(name) is not None
            ]
            metrics[name] = {
                "mean": mean(values) if values else None,
                "std": pstdev(values) if values else None,
                "n": len(values),
            }
        summaries.append({
            "policy": policy,
            "scenario": scenario,
            "run_count": len(policy_runs),
            "metrics": metrics,
        })
    return summaries


def build_experiment_scenarios(workload: WorkloadRequest) -> list[tuple[str, WorkloadRequest]]:
    failure_workload = workload
    if workload.workload_type not in FAILURE_WORKLOAD_TYPES:
        failure_workload = workload.model_copy(update={"workload_type": "crash"})
    healthy_workload = workload.model_copy(update={"workload_type": "healthy"})
    return [
        ("healthy_control", healthy_workload),
        ("failure_scenario", failure_workload),
    ]


class ExperimentRequest(BaseModel):
    workload_type: WorkloadRequest
    duration_s: int = Field(default=40, ge=5, le=3600)
    repetitions: int = Field(default=1, ge=1, le=10)
    randomization_seed: int | None = Field(default=None, ge=0, le=4_294_967_295)
    sampling_interval: float | None = Field(default=None, ge=0.25, le=30.0)
    prediction_horizon: int | None = Field(default=None, ge=1, le=600)
    low_risk_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    high_risk_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    restart_cooldown: float | None = Field(default=None, ge=0.0, le=3600.0)
    max_restart_attempts: int | None = Field(default=None, ge=0, le=20)
    max_operation_retries: int | None = Field(default=None, ge=0, le=10)

    @model_validator(mode="after")
    def validate_thresholds(self):
        low = self.low_risk_threshold
        high = self.high_risk_threshold
        if low is not None and high is not None and low >= high:
            raise ValueError("low_risk_threshold must be less than high_risk_threshold")
        current_settings = settings_store.get()
        horizon = self.prediction_horizon or current_settings.prediction_horizon
        interval = self.sampling_interval or current_settings.monitoring_interval
        if self.duration_s < horizon + 3 * interval:
            raise ValueError(
                "Observation duration must be at least the prediction horizon plus "
                "three sampling intervals to include fully observed negative windows."
            )
        if self.workload_type.fault_after_s >= horizon:
            raise ValueError(
                "The failure must occur before the prediction horizon to measure "
                "positive prediction windows."
            )
        return self


async def execute_experiment(experiment_id: str, request: ExperimentRequest) -> None:
    config = request.model_dump()
    policies = (
        "fixed",
        "threshold",
        "predictive_fixed",
        "confidence_recheck",
        "preemptguard",
    )
    order_seed = (
        request.randomization_seed
        if request.randomization_seed is not None
        else uuid.UUID(experiment_id).int & 0xFFFFFFFF
    )
    order_rng = random.Random(order_seed)
    rows: list[dict[str, object]] = []
    trial_orders: list[list[str]] = []
    scenarios = build_experiment_scenarios(request.workload_type)
    original_settings = settings_store.get()
    changes = {
        key: value for key, value in {
            "monitoring_interval": request.sampling_interval,
            "prediction_horizon": request.prediction_horizon,
            "low_risk_threshold": request.low_risk_threshold,
            "high_risk_threshold": request.high_risk_threshold,
            "restart_cooldown": request.restart_cooldown,
            "max_restart_attempts": request.max_restart_attempts,
            "max_operation_retries": request.max_operation_retries,
        }.items() if value is not None
    }
    cfg = settings_store.update(changes) if changes else original_settings
    if cfg.low_risk_threshold >= cfg.high_risk_threshold:
        raise ValueError("low_risk_threshold must be less than high_risk_threshold")
    duration = request.duration_s
    workload = request.workload_type
    try:
        database.execute("UPDATE experiments SET status='running' WHERE id=?", (experiment_id,))
        for repetition in range(request.repetitions):
            trials = [
                (policy, scenario_name, scenario_workload)
                for policy in policies
                for scenario_name, scenario_workload in scenarios
            ]
            order_rng.shuffle(trials)
            trial_order = [
                f"{policy}:{scenario_name}"
                for policy, scenario_name, _ in trials
            ]
            trial_orders.append(trial_order)
            for trial_position, (policy, scenario_name, scenario_workload) in enumerate(
                trials, start=1
            ):
                worker_duration = min(3600, duration + 60)
                child = process_manager.start(
                    scenario_workload.model_copy(update={
                        "policy": policy,
                        "duration_s": worker_duration,
                    })
                )
                process_id = child["id"]
                await asyncio.sleep(duration)
                try:
                    process_manager.stop(process_id)
                except KeyError:
                    pass
                measurements = database.query(
                    "SELECT * FROM measurements WHERE process_id=? ORDER BY timestamp",
                    (process_id,),
                )
                predictions = database.query(
                    "SELECT * FROM predictions WHERE process_id=? ORDER BY timestamp",
                    (process_id,),
                )
                recoveries = database.query(
                    "SELECT * FROM recovery_attempts WHERE process_id=? ORDER BY timestamp",
                    (process_id,),
                )
                metrics = summarize_run(
                    measurements, predictions, recoveries,
                    horizon_s=request.prediction_horizon or cfg.prediction_horizon,
                    risk_threshold=cfg.high_risk_threshold,
                    monitoring_samples=database.query(
                        """SELECT timestamp,cpu_percent,memory_mb FROM monitor_overhead
                        WHERE timestamp BETWEEN ? AND ? ORDER BY timestamp""",
                        (float(measurements[0]["timestamp"]), float(measurements[-1]["timestamp"]))
                        if measurements else (0.0, 0.0),
                    ),
                    post_recovery_observation_s=cfg.verification_period,
                )
                rows.append({
                    "repetition": repetition + 1,
                    "trial_position": trial_position,
                    "policy_order_seed": order_seed,
                    "policy": policy,
                    "scenario": scenario_name,
                    "workload_type": scenario_workload.workload_type,
                    "process_id": process_id,
                    "metrics": metrics,
                    "configuration": {
                        "duration_s": duration,
                        "sampling_interval": request.sampling_interval or cfg.monitoring_interval,
                        "prediction_horizon": request.prediction_horizon or cfg.prediction_horizon,
                        "low_risk_threshold": cfg.low_risk_threshold,
                        "high_risk_threshold": cfg.high_risk_threshold,
                        "restart_cooldown": cfg.restart_cooldown,
                        "max_restart_attempts": cfg.max_restart_attempts,
                        "max_operation_retries": cfg.max_operation_retries,
                    },
                })
        result = {
            "experiment_id": experiment_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "software_version": "0.1.0",
            "prediction_model": "rule_based",
            "scenario_design": {
                "method": "paired healthy control and failure scenario for every policy and repetition",
                "scenarios": [
                    {"scenario": name, "workload_type": scenario.workload_type}
                    for name, scenario in scenarios
                ],
            },
            "randomization": {
                "method": "independently shuffled policy and scenario order within each repetition",
                "seed": order_seed,
                "trial_orders": trial_orders,
            },
            "comparison": rows,
            "policy_summary": summarize_policies(rows),
            "ablation_study": {
                "status": "completed",
                "comparisons": [
                    {
                        "component": name,
                        "policy": policy,
                        "runs": [
                            run for run in rows if run["policy"] == policy
                        ],
                    }
                    for name, policy in (
                        ("Threshold-based detection without prediction", "threshold"),
                        ("Predictive risk with a fixed recovery threshold", "predictive_fixed"),
                        ("Predictive risk with confidence-aware rechecking", "confidence_recheck"),
                        ("Full risk-aware policy", "preemptguard"),
                    )
                ],
            },
        }
        database.execute(
            "UPDATE experiments SET status='completed',results_json=?,completed_at=? WHERE id=?",
            (json.dumps(result), datetime.now(timezone.utc).isoformat(), experiment_id),
        )
    except Exception as exc:
        database.execute(
            "UPDATE experiments SET status='failed',results_json=?,completed_at=? WHERE id=?",
            (json.dumps({"error": str(exc), "partial_results": rows}),
             datetime.now(timezone.utc).isoformat(), experiment_id),
        )
        raise
    finally:
        settings_store.update(original_settings.model_dump())


@router.post("")
def create_experiment(request: ExperimentRequest, background_tasks: BackgroundTasks):
    current_settings = settings_store.get()
    low_threshold = (
        request.low_risk_threshold
        if request.low_risk_threshold is not None else current_settings.low_risk_threshold
    )
    high_threshold = (
        request.high_risk_threshold
        if request.high_risk_threshold is not None else current_settings.high_risk_threshold
    )
    if low_threshold >= high_threshold:
        raise HTTPException(
            status_code=422,
            detail="low_risk_threshold must be less than high_risk_threshold",
        )
    horizon = request.prediction_horizon or current_settings.prediction_horizon
    active = database.query(
        "SELECT id FROM experiments WHERE status IN ('queued','running') LIMIT 1"
    )
    if active:
        raise HTTPException(status_code=409, detail="Another experiment is already running.")
    if any(child.poll() is None for child in process_manager.children.values()):
        raise HTTPException(
            status_code=409,
            detail="Stop active managed workloads before starting a paired experiment.",
        )
    experiment_id = str(uuid.uuid4())
    database.execute(
        "INSERT INTO experiments(id,configuration_json,status,created_at) VALUES(?,?,?,?)",
        (
            experiment_id,
            json.dumps(request.model_dump()),
            "queued",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    background_tasks.add_task(execute_experiment, experiment_id, request)
    return {"experiment_id": experiment_id, "status": "queued"}


@router.get("")
def list_experiments():
    rows = database.query(
        "SELECT id,configuration_json,status,created_at,completed_at FROM experiments ORDER BY created_at DESC"
    )
    for row in rows:
        row["configuration"] = json.loads(row.pop("configuration_json"))
    return rows


@router.get("/{experiment_id}")
def get_experiment(experiment_id: str):
    rows = database.query("SELECT * FROM experiments WHERE id=?", (experiment_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    row = rows[0]
    return {
        "experiment_id": row["id"],
        "status": row["status"],
        "configuration": json.loads(row["configuration_json"]),
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "results": json.loads(row["results_json"]) if row["results_json"] else None,
    }


@router.get("/{experiment_id}/export.csv")
def export_experiment(experiment_id: str):
    rows = database.query("SELECT results_json,status FROM experiments WHERE id=?", (experiment_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Experiment not found.")
    if rows[0]["status"] != "completed" or not rows[0]["results_json"]:
        raise HTTPException(status_code=409, detail="Experiment has not completed.")
    result = json.loads(rows[0]["results_json"])
    stream = io.StringIO()
    writer = csv.DictWriter(
        stream,
        fieldnames=[
            "experiment_id", "policy", "scenario", "repetition", "trial_position",
            "policy_order_seed", "prediction_model", "workload_type", "process_id",
            "sample_count", "prediction_sample_count", "labeled_prediction_samples",
            "prediction_model_counts", "true_positives", "false_positives",
            "false_negatives", "true_negatives",
            "prediction_horizon_s", "precision", "recall", "f1",
            "false_positive_rate", "first_early_warning_lead_time_s",
            "recovery_attempts", "successful_recoveries", "failed_recoveries",
            "recovery_success_rate", "mean_recovery_time_s", "median_recovery_time_s",
            "total_downtime_s", "unnecessary_restarts",
            "unassessed_unnecessary_restart_count", "cpu_overhead_percent",
            "memory_overhead_mb",
        ],
    )
    writer.writeheader()
    for run in result["comparison"]:
        metrics = run["metrics"]
        writer.writerow({
            "experiment_id": experiment_id,
            "policy": run["policy"],
            "scenario": run.get("scenario"),
            "repetition": run["repetition"],
            "trial_position": run["trial_position"],
            "policy_order_seed": run["policy_order_seed"],
            "prediction_model": result.get("prediction_model"),
            "workload_type": run["workload_type"],
            "process_id": run["process_id"],
            **{key: metrics.get(key) for key in writer.fieldnames if key in metrics},
        })
    stream.seek(0)
    return StreamingResponse(
        iter([stream.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="preemptguard-{experiment_id}.csv"'},
    )
