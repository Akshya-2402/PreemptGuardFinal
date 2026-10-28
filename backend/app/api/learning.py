import json
from statistics import mean, median

from fastapi import APIRouter, HTTPException

from app.core.database import database
from app.experiments.metrics import summarize_run
from app.host_recovery import (
    HostRecoveryError,
    configure_restart,
    execute_recovery,
)
from app.online_learning import (
    RECOVERY_ACTIONS,
    load_training_examples,
    process_features,
    recommend_action,
    record_feedback,
)
from app.schemas import (
    HostRecoveryFeedback,
    HostRecoveryRequest,
    HostRestartConfigRequest,
)

router = APIRouter(prefix="/api", tags=["local-online-learning"])


@router.get("/learning/status")
def get_learning_status():
    examples = load_training_examples(database)
    feedback_rows = database.query(
        """SELECT helped,COUNT(*) AS count FROM host_recovery_feedback
        WHERE helped IS NOT NULL GROUP BY helped"""
    )
    helpful_count = sum(row["count"] for row in feedback_rows if row["helped"] == 1)
    labeled_feedback_count = sum(row["count"] for row in feedback_rows)
    feedback_by_action = database.query(
        """SELECT a.action,COUNT(*) AS labeled_outcomes,
        SUM(CASE WHEN f.helped=1 THEN 1 ELSE 0 END) AS helpful,
        SUM(CASE WHEN f.helped=0 THEN 1 ELSE 0 END) AS not_helpful
        FROM host_recovery_feedback f
        JOIN host_action_runs a ON a.id=f.action_run_id
        WHERE f.helped IS NOT NULL GROUP BY a.action ORDER BY a.action"""
    )

    simulation_ids = []
    for row in database.query(
        "SELECT id,config_json FROM managed_processes ORDER BY started_at"
    ):
        try:
            if json.loads(row["config_json"]).get("simulation") is True:
                simulation_ids.append(row["id"])
        except (json.JSONDecodeError, TypeError):
            continue

    prediction_metrics = {
        "true_positives": 0,
        "false_positives": 0,
        "false_negatives": 0,
        "true_negatives": 0,
        "labeled_prediction_samples": 0,
        "evaluated_runs": 0,
        "prediction_horizon_s": None,
    }
    for process_id in simulation_ids:
        measurements = database.query(
            """SELECT timestamp,process_state,responsive FROM measurements
            WHERE process_id=? ORDER BY timestamp""",
            (process_id,),
        )
        predictions = database.query(
            """SELECT timestamp,risk_score,model_name,horizon_s FROM predictions
            WHERE process_id=? ORDER BY timestamp""",
            (process_id,),
        )
        if not measurements or not predictions:
            continue
        recoveries = database.query(
            """SELECT timestamp,status,duration_s FROM recovery_attempts
            WHERE process_id=? ORDER BY timestamp""",
            (process_id,),
        )
        horizon_s = int(predictions[-1]["horizon_s"])
        metrics = summarize_run(
            measurements,
            predictions,
            recoveries,
            horizon_s=horizon_s,
        )
        for key in (
            "true_positives", "false_positives",
            "false_negatives", "true_negatives",
            "labeled_prediction_samples",
        ):
            prediction_metrics[key] += metrics[key]
        prediction_metrics["evaluated_runs"] += 1
        prediction_metrics["prediction_horizon_s"] = horizon_s

    predicted_positives = (
        prediction_metrics["true_positives"] + prediction_metrics["false_positives"]
    )
    actual_positives = (
        prediction_metrics["true_positives"] + prediction_metrics["false_negatives"]
    )
    prediction_metrics["precision"] = (
        prediction_metrics["true_positives"] / predicted_positives
        if predicted_positives else None
    )
    prediction_metrics["recall"] = (
        prediction_metrics["true_positives"] / actual_positives
        if actual_positives else None
    )
    precision = prediction_metrics["precision"]
    recall = prediction_metrics["recall"]
    prediction_metrics["accuracy"] = (
        (
            prediction_metrics["true_positives"]
            + prediction_metrics["true_negatives"]
        )
        / prediction_metrics["labeled_prediction_samples"]
        if prediction_metrics["labeled_prediction_samples"] else None
    )
    prediction_metrics["f1"] = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )

    simulation_recoveries = []
    for row in database.query(
        """SELECT r.status,r.duration_s,p.config_json,
        COALESCE((
            SELECT d.action FROM decisions d
            WHERE d.process_id=r.process_id AND d.action='restart'
            AND d.timestamp<=r.timestamp
            ORDER BY d.timestamp DESC LIMIT 1
        ),'simulation_recovery') AS action
        FROM recovery_attempts r
        JOIN managed_processes p ON p.id=r.process_id
        WHERE r.status IN ('healthy','failed')"""
    ):
        try:
            is_simulation = json.loads(row["config_json"]).get("simulation") is True
        except (json.JSONDecodeError, TypeError):
            continue
        if is_simulation:
            simulation_recoveries.append({
                "action": row["action"],
                "status": row["status"],
                "duration_s": row["duration_s"],
            })
    actual_recoveries = database.query(
        """SELECT a.action,a.status,a.duration_s,f.helped FROM host_action_runs a
        LEFT JOIN host_recovery_feedback f ON f.action_run_id=a.id
        WHERE a.status IN ('requested','failed')"""
    )
    recovery_duration_rows = database.query(
        """SELECT action,status,duration_s FROM host_action_runs
        WHERE status IN ('requested','failed')"""
    )
    recovery_duration_rows.extend(simulation_recoveries)

    def recovery_summary(rows):
        durations = [
            float(row["duration_s"]) for row in rows
            if row["duration_s"] is not None
        ]
        measured_outcomes = [
            row for row in rows
            if (
                row.get("helped") is not None
                if "helped" in row
                else row["status"] in {"healthy", "failed"}
            )
        ]
        successful = sum(
            bool(row["helped"]) if "helped" in row else row["status"] == "healthy"
            for row in measured_outcomes
        )
        return {
            "attempts": len(rows),
            "successful": successful,
            "labeled_outcomes": len(measured_outcomes),
            "success_rate": successful / len(measured_outcomes) if measured_outcomes else None,
            "mean_duration_s": mean(durations) if durations else None,
            "median_duration_s": median(durations) if durations else None,
            "duration_sample_count": len(durations),
        }

    recovery_by_action = []
    for action in sorted({row["action"] for row in recovery_duration_rows}):
        rows = [row for row in recovery_duration_rows if row["action"] == action]
        recovery_by_action.append({"action": action, **recovery_summary(rows)})

    combined_recoveries = simulation_recoveries + actual_recoveries
    return {
        "enabled": True,
        "storage": "local_sqlite",
        "model": "local_online_case_based_v1",
        "model_type": "Local case-based recovery selector with a rule-based risk predictor",
        "training_examples": len(examples),
        "actual_feedback_examples": sum(item["source"] == "actual" for item in examples),
        "simulation_recovery_examples": sum(item["source"] == "simulation" for item in examples),
        "action_counts": {
            action: sum(item["action"] == action for item in examples)
            for action in sorted(RECOVERY_ACTIONS)
        },
        "description": (
            "The local case-based model updates from simulation recovery results and "
            "user-labelled real-process outcomes. It recommends only with at least five "
            "similar observed outcomes and an estimated success rate of at least 70%."
        ),
        "training_sources": {
            "actual_processes": sum(item["source"] == "actual" for item in examples),
            "simulated_recoveries": sum(item["source"] == "simulation" for item in examples),
        },
        "actual_feedback": {
            "labeled_outcomes": labeled_feedback_count,
            "helpful": helpful_count,
            "not_helpful": labeled_feedback_count - helpful_count,
            "helpfulness_rate": (
                helpful_count / labeled_feedback_count if labeled_feedback_count else None
            ),
            "by_action": feedback_by_action,
        },
        "prediction_evaluation": {
            **prediction_metrics,
            "scope": "simulated process runs with fully observed labels",
            "positive_threshold": 0.65,
        },
        "recovery_performance": {
            **recovery_summary(combined_recoveries),
            "simulations": recovery_summary(simulation_recoveries),
            "actual_processes": recovery_summary(actual_recoveries),
            "by_action": recovery_by_action,
        },
    }


@router.get("/learning/process-history")
def get_learning_process_history():
    history = []
    simulation_rows = database.query(
        """SELECT p.id,p.name,p.workload_type,p.status,p.started_at,p.ended_at,
        p.config_json,COUNT(DISTINCT m.id) AS sample_count,
        COUNT(DISTINCT d.id) AS decision_count,
        COUNT(DISTINCT r.id) AS recovery_count,
        MAX(r.timestamp) AS last_recovery_at
        FROM managed_processes p
        LEFT JOIN measurements m ON m.process_id=p.id
        LEFT JOIN decisions d ON d.process_id=p.id
        LEFT JOIN recovery_attempts r ON r.process_id=p.id
        GROUP BY p.id ORDER BY p.started_at DESC"""
    )
    for row in simulation_rows:
        try:
            config = json.loads(row["config_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        if config.get("simulation") is not True:
            continue
        history.append({
            "kind": "simulation",
            "process_id": row["id"],
            "name": row["name"],
            "workload_type": row["workload_type"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "sample_count": row["sample_count"],
            "decision_count": row["decision_count"],
            "recovery_count": row["recovery_count"],
            "last_recovery_at": row["last_recovery_at"],
        })

    actual_rows = database.query(
        """SELECT a.pid,a.process_started_at,a.process_name,
        COUNT(DISTINCT a.id) AS action_count,
        COUNT(DISTINCT f.id) AS labeled_outcomes,
        SUM(CASE WHEN f.helped=1 THEN 1 ELSE 0 END) AS helpful_outcomes,
        MIN(a.created_at) AS first_action_at,MAX(a.created_at) AS last_action_at,
        MAX(s.sampled_at) AS last_sampled_at
        FROM host_action_runs a
        LEFT JOIN host_recovery_feedback f ON f.action_run_id=a.id
        LEFT JOIN host_process_samples s ON s.pid=a.pid
            AND s.process_started_at=a.process_started_at
        GROUP BY a.pid,a.process_started_at,a.process_name
        ORDER BY last_action_at DESC"""
    )
    for row in actual_rows:
        history.append({
            "kind": "actual_process",
            "process_id": None,
            "pid": row["pid"],
            "process_started_at": row["process_started_at"],
            "name": row["process_name"],
            "workload_type": None,
            "status": "monitored" if row["last_sampled_at"] is not None else "not currently sampled",
            "started_at": row["first_action_at"],
            "ended_at": row["last_action_at"],
            "sample_count": 0,
            "decision_count": row["action_count"],
            "recovery_count": row["action_count"],
            "labeled_outcomes": row["labeled_outcomes"],
            "helpful_outcomes": row["helpful_outcomes"] or 0,
            "last_recovery_at": row["last_action_at"],
        })
    return history


@router.get("/learning/process-statistics")
def get_learning_process_statistics(
    kind: str,
    process_id: str | None = None,
    pid: int | None = None,
    process_started_at: float | None = None,
):
    if kind == "simulation":
        if not process_id:
            raise HTTPException(status_code=422, detail="process_id is required for a simulation.")
        rows = database.query(
            """SELECT id,name,workload_type,status,started_at,config_json
            FROM managed_processes WHERE id=?""",
            (process_id,),
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Simulation process was not found.")
        process = rows[0]
        try:
            config = json.loads(process["config_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise HTTPException(status_code=500, detail="Saved simulation configuration is invalid.") from exc
        if config.get("simulation") is not True:
            raise HTTPException(status_code=404, detail="This managed process is not a simulation.")
        measurements = database.query(
            """SELECT timestamp,process_state,responsive FROM measurements
            WHERE process_id=? ORDER BY timestamp""",
            (process_id,),
        )
        predictions = database.query(
            """SELECT timestamp,risk_score,model_name,horizon_s FROM predictions
            WHERE process_id=? ORDER BY timestamp""",
            (process_id,),
        )
        recoveries = database.query(
            """SELECT timestamp,status,duration_s,reason FROM recovery_attempts
            WHERE process_id=? ORDER BY timestamp""",
            (process_id,),
        )
        horizon = max((int(row["horizon_s"]) for row in predictions), default=None)
        evaluation = summarize_run(
            measurements,
            predictions,
            recoveries,
            horizon_s=horizon or 30,
        ) if measurements and predictions else None
        cohort = []
        peer_rows = database.query(
            """SELECT id FROM managed_processes
            WHERE workload_type=? AND id!=? ORDER BY started_at""",
            (process["workload_type"], process_id),
        )
        for peer in peer_rows:
            peer_config_rows = database.query(
                "SELECT config_json FROM managed_processes WHERE id=?",
                (peer["id"],),
            )
            try:
                if json.loads(peer_config_rows[0]["config_json"]).get("simulation") is not True:
                    continue
            except (json.JSONDecodeError, TypeError, IndexError):
                continue
            peer_measurements = database.query(
                """SELECT timestamp,process_state,responsive FROM measurements
                WHERE process_id=? ORDER BY timestamp""",
                (peer["id"],),
            )
            peer_predictions = database.query(
                """SELECT timestamp,risk_score,model_name,horizon_s FROM predictions
                WHERE process_id=? ORDER BY timestamp""",
                (peer["id"],),
            )
            if not peer_measurements or not peer_predictions:
                continue
            peer_horizon = max(int(item["horizon_s"]) for item in peer_predictions)
            peer_recoveries = database.query(
                """SELECT timestamp,status,duration_s FROM recovery_attempts
                WHERE process_id=? ORDER BY timestamp""",
                (peer["id"],),
            )
            cohort.append(summarize_run(
                peer_measurements,
                peer_predictions,
                peer_recoveries,
                horizon_s=peer_horizon,
            ))
        cohort_prediction_count = sum(
            item["labeled_prediction_samples"] for item in cohort
        )
        cohort_tp = sum(item["true_positives"] for item in cohort)
        cohort_fp = sum(item["false_positives"] for item in cohort)
        cohort_fn = sum(item["false_negatives"] for item in cohort)
        cohort_tn = sum(item["true_negatives"] for item in cohort)
        cohort_precision = cohort_tp / (cohort_tp + cohort_fp) if cohort_tp + cohort_fp else None
        cohort_recall = cohort_tp / (cohort_tp + cohort_fn) if cohort_tp + cohort_fn else None
        return {
            "kind": "simulation",
            "process_id": process_id,
            "name": process["name"],
            "workload_type": process["workload_type"],
            "started_at": process["started_at"],
            "status": process["status"],
            "prediction_evaluation": evaluation,
            "similar_processes": {
                "definition": f"Other saved simulations with the same {process['workload_type']} workload type.",
                "process_count": len(cohort),
                "labeled_prediction_samples": cohort_prediction_count,
                "precision": cohort_precision,
                "recall": cohort_recall,
                "accuracy": (
                    (cohort_tp + cohort_tn) / cohort_prediction_count
                    if cohort_prediction_count else None
                ),
                "true_positives": cohort_tp,
                "false_positives": cohort_fp,
                "false_negatives": cohort_fn,
                "true_negatives": cohort_tn,
            },
            "recovery_performance": {
                "attempts": len(recoveries),
                "successful": sum(row["status"] == "healthy" for row in recoveries),
                "mean_duration_s": mean(
                    [float(row["duration_s"]) for row in recoveries if row["duration_s"] is not None]
                ) if any(row["duration_s"] is not None for row in recoveries) else None,
                "median_duration_s": median(
                    [float(row["duration_s"]) for row in recoveries if row["duration_s"] is not None]
                ) if any(row["duration_s"] is not None for row in recoveries) else None,
                "duration_sample_count": sum(row["duration_s"] is not None for row in recoveries),
            },
        }

    if kind != "actual_process":
        raise HTTPException(status_code=422, detail="kind must be simulation or actual_process.")
    if pid is None or process_started_at is None:
        raise HTTPException(
            status_code=422,
            detail="pid and process_started_at are required for an actual process.",
        )
    action_rows = database.query(
        """SELECT a.id,a.action,a.status,a.features_json,a.duration_s,a.created_at,
        f.helped FROM host_action_runs a
        LEFT JOIN host_recovery_feedback f ON f.action_run_id=a.id
        WHERE a.pid=? AND a.process_started_at=? ORDER BY a.created_at""",
        (pid, process_started_at),
    )
    if not action_rows:
        raise HTTPException(status_code=404, detail="No recovery history exists for this process instance.")
    sample_rows = database.query(
        """SELECT * FROM host_process_samples WHERE pid=? AND process_started_at=?
        ORDER BY sampled_at DESC LIMIT 1""",
        (pid, process_started_at),
    )
    labeled = [row for row in action_rows if row["helped"] is not None]
    duration_values = [
        float(row["duration_s"]) for row in action_rows if row["duration_s"] is not None
    ]
    actual_examples = [
        example for example in load_training_examples(database)
        if example["source"] == "actual"
    ]
    current_features = process_features(sample_rows[0]) if sample_rows else None
    similar_by_action = []
    for action in sorted({row["action"] for row in action_rows}):
        current_runs = [row for row in action_rows if row["action"] == action]
        peer_examples = []
        for example in actual_examples:
            if example["action"] != action or current_features is None:
                continue
            distance = sum(
                (current_features.get(key, 0.0) - example["features"].get(key, 0.0)) ** 2
                for key in current_features
            ) ** 0.5
            if distance <= 0.5:
                peer_examples.append((distance, example))
        peer_examples.sort(key=lambda item: item[0])
        peers = [item[1] for item in peer_examples[:20]]
        similar_by_action.append({
            "action": action,
            "this_process_labeled_outcomes": sum(
                row["helped"] is not None for row in current_runs
            ),
            "this_process_helpful_rate": (
                sum(bool(row["helped"]) for row in current_runs if row["helped"] is not None)
                / sum(row["helped"] is not None for row in current_runs)
                if any(row["helped"] is not None for row in current_runs) else None
            ),
            "similar_labeled_outcomes": len(peers),
            "similar_helpful_rate": (
                sum(bool(item["helped"]) for item in peers) / len(peers)
                if peers else None
            ),
        })
    return {
        "kind": "actual_process",
        "pid": pid,
        "process_started_at": process_started_at,
        "name": action_rows[0]["action"] and (
            database.query(
                """SELECT process_name FROM host_action_runs
                WHERE pid=? AND process_started_at=? LIMIT 1""",
                (pid, process_started_at),
            )[0]["process_name"]
        ),
        "status": sample_rows[0]["state"] if sample_rows else "not currently sampled",
        "prediction_evaluation": None,
        "prediction_evaluation_note": (
            "Per-process risk-prediction precision cannot be measured from host telemetry alone; "
            "the user would need to label actual failure outcomes independently."
        ),
        "recovery_performance": {
            "attempts": len(action_rows),
            "labeled_outcomes": len(labeled),
            "helpful": sum(bool(row["helped"]) for row in labeled),
            "helpfulness_rate": (
                sum(bool(row["helped"]) for row in labeled) / len(labeled) if labeled else None
            ),
            "mean_duration_s": mean(duration_values) if duration_values else None,
            "median_duration_s": median(duration_values) if duration_values else None,
            "duration_sample_count": len(duration_values),
        },
        "similar_process_comparison": {
            "definition": "Nearest labeled actual-process outcomes for the same recovery action within the feature-distance limit.",
            "by_action": similar_by_action,
        },
        "actions": [
            {
                "action": row["action"],
                "status": row["status"],
                "helped": row["helped"],
                "duration_s": row["duration_s"],
                "created_at": row["created_at"],
            }
            for row in action_rows
        ],
    }


@router.get("/learning/pending-feedback")
def get_pending_learning_feedback():
    rows = database.query(
        """SELECT a.id AS action_run_id,a.pid,a.process_started_at,
        a.process_name,a.action,a.details,a.created_at
        FROM host_action_runs a
        WHERE a.status='requested' AND NOT EXISTS(
            SELECT 1 FROM host_recovery_feedback f WHERE f.action_run_id=a.id
        )
        ORDER BY a.created_at DESC LIMIT 30"""
    )
    for row in rows:
        row["details"] = json.loads(row["details"])
    return rows


@router.get("/system/processes/{pid}/recommendation")
def get_process_recommendation(
    pid: int,
    process_started_at: float,
    sudden_change: bool = False,
):
    rows = database.query(
        """SELECT * FROM host_process_samples
        WHERE pid=? AND process_started_at=? ORDER BY sampled_at DESC LIMIT 1""",
        (pid, process_started_at),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="No sample found for this process instance.")
    sample = rows[0]
    if sample["state"] == "stopped":
        return {
            "recommendation": "resume",
            "reason": "The selected process is stopped; resuming it is the least disruptive recovery to try first.",
            "estimates": [],
            "training_example_count": 0,
            "training_counts_by_action": {},
            "model": "stopped_process_resume_v1",
            "confidence_kind": "direct_stopped_process_state",
            "setup_required_action": None,
        }
    result = recommend_action(
        sample,
        database,
        require_high_risk=not sudden_change,
    )
    configs = database.query(
        """SELECT config_json FROM host_action_configs
        WHERE pid=? AND process_started_at=?""",
        (pid, process_started_at),
    )
    config = json.loads(configs[0]["config_json"]) if configs else {}

    def is_available(action: str) -> bool:
        if action == "restart_configured":
            return isinstance(config.get("restart"), dict)
        if action == "restore_priority":
            return config.get("original_priority") is not None
        if action == "retry":
            failed = database.query(
                """SELECT id,action FROM host_action_runs
                WHERE pid=? AND process_started_at=? AND status='failed'
                ORDER BY created_at DESC LIMIT 1""",
                (pid, process_started_at),
            )
            return bool(
                failed
                and failed[0]["action"] != "retry"
                and not database.query(
                    "SELECT 1 FROM host_action_runs WHERE retry_of=? LIMIT 1",
                    (failed[0]["id"],),
                )
            )
        return True

    estimates = result["estimates"]
    available_estimates = [
        item for item in estimates if is_available(item["action"])
    ]
    result["estimates"] = available_estimates
    result["recommendation"] = next(
        (
            item["action"]
            for item in available_estimates
            if item["similar_outcomes"] >= 5
            and item["estimated_success_rate"] >= 0.7
        ),
        None,
    )
    result["setup_required_action"] = None
    if result["recommendation"] is None:
        setup_action = next(
            (
                item["action"]
                for item in estimates
                if item["similar_outcomes"] >= 5
                and item["estimated_success_rate"] >= 0.7
                and item["action"] == "restart_configured"
                and not is_available(item["action"])
            ),
            None,
        )
        if setup_action:
            result["setup_required_action"] = setup_action
            result["reason"] = "Configure a restart command before this method can be applied."
        elif estimates and not available_estimates:
            result["reason"] = "No learned recovery method is currently available for this process."
    return result


@router.post("/system/processes/{pid}/recovery")
def recover_host_process(pid: int, request: HostRecoveryRequest):
    try:
        return execute_recovery(
            pid=pid,
            process_started_at=request.process_started_at,
            action=request.action,
            allow_action=request.allow_action,
            remember_action=request.remember_action,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except HostRecoveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/system/processes/{pid}/restart-config")
def save_restart_config(pid: int, request: HostRestartConfigRequest):
    try:
        configure_restart(
            pid=pid,
            process_started_at=request.process_started_at,
            executable=request.executable,
            arguments=request.arguments,
            working_directory=request.working_directory,
        )
    except HostRecoveryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"Restart path is unavailable: {exc}") from exc
    return {"status": "configured"}


@router.get("/system/processes/{pid}/actions")
def get_host_process_actions(pid: int, process_started_at: float):
    consent = database.query(
        """SELECT action,created_at FROM host_action_consents
        WHERE pid=? AND process_started_at=? ORDER BY action""",
        (pid, process_started_at),
    )
    runs = database.query(
        """SELECT a.id,a.action,a.status,a.details,a.duration_s,a.created_at,
        EXISTS(SELECT 1 FROM host_recovery_feedback f WHERE f.action_run_id=a.id)
            AS feedback_recorded
        FROM host_action_runs a
        WHERE pid=? AND process_started_at=? ORDER BY created_at DESC LIMIT 20""",
        (pid, process_started_at),
    )
    for row in runs:
        row["details"] = json.loads(row["details"])
    configs = database.query(
        """SELECT config_json FROM host_action_configs
        WHERE pid=? AND process_started_at=?""",
        (pid, process_started_at),
    )
    config = json.loads(configs[0]["config_json"]) if configs else {}
    return {
        "approved_actions": consent,
        "restart_configured": bool(config.get("restart")),
        "restore_priority_available": config.get("original_priority") is not None,
        "runs": runs,
    }


@router.delete("/system/processes/{pid}/consents/{action}")
def revoke_host_action_consent(pid: int, action: str, process_started_at: float):
    if action not in RECOVERY_ACTIONS:
        raise HTTPException(status_code=422, detail="Unsupported recovery action.")
    database.execute(
        """DELETE FROM host_action_consents
        WHERE pid=? AND process_started_at=? AND action=?""",
        (pid, process_started_at, action),
    )
    return {"status": "approval revoked"}


@router.post("/system/processes/{pid}/feedback")
def submit_recovery_feedback(pid: int, request: HostRecoveryFeedback):
    rows = database.query(
        """SELECT * FROM host_action_runs WHERE id=? AND pid=?""",
        (request.action_run_id, pid),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Recovery action record not found.")
    action_run = rows[0]
    if action_run["status"] != "requested":
        raise HTTPException(
            status_code=409,
            detail="Feedback is accepted only for a recovery action that was requested.",
        )
    if database.query(
        "SELECT 1 FROM host_recovery_feedback WHERE action_run_id=?",
        (request.action_run_id,),
    ):
        raise HTTPException(status_code=409, detail="Feedback was already recorded.")
    try:
        record_feedback(
            pid=pid,
            process_started_at=float(action_run["process_started_at"]),
            process_name=action_run["process_name"],
            action=action_run["action"],
            action_run_id=request.action_run_id,
            helped=request.helped,
            features=json.loads(action_run["features_json"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "feedback recorded"}
