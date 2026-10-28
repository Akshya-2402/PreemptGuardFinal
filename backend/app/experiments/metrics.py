from collections.abc import Sequence
from statistics import mean, median
from typing import Any


def _failure_times(measurements: Sequence[dict[str, Any]]) -> list[float]:
    events: list[float] = []
    stale_start: float | None = None
    stale_count = 0
    for item in measurements:
        timestamp = float(item["timestamp"])
        if item["process_state"] == "failed":
            events.append(timestamp)
        if not bool(item.get("responsive", True)):
            if stale_count == 0:
                stale_start = timestamp
            stale_count += 1
            if stale_count == 3 and stale_start is not None:
                events.append(stale_start)
        else:
            stale_count = 0
            stale_start = None
    return sorted(set(events))


def summarize_run(
    measurements: Sequence[dict[str, Any]],
    predictions: Sequence[dict[str, Any]],
    recoveries: Sequence[dict[str, Any]],
    *,
    horizon_s: int,
    risk_threshold: float = 0.65,
    monitoring_samples: Sequence[dict[str, Any]] = (),
    post_recovery_observation_s: float = 3.0,
) -> dict[str, Any]:
    """Evaluate recorded points using eventual observed failure as the label."""
    failures = _failure_times(measurements)
    labeled = 0
    true_positive = false_positive = false_negative = true_negative = 0
    lead_times: list[float] = []
    last_measurement = (
        float(measurements[-1]["timestamp"]) if measurements else 0.0
    )
    measurement_by_time = {
        float(item["timestamp"]): item for item in measurements
    }
    for prediction in sorted(predictions, key=lambda item: float(item["timestamp"])):
        timestamp = float(prediction["timestamp"])
        matching_measurement = measurement_by_time.get(timestamp)
        if matching_measurement is not None and (
            matching_measurement["process_state"] == "failed"
            or not bool(matching_measurement.get("responsive", True))
        ):
            continue
        next_failure = next((event for event in failures if event > timestamp), None)
        positive_label = (
            next_failure is not None and next_failure <= timestamp + horizon_s
        )
        fully_observed_negative = last_measurement >= timestamp + horizon_s
        if not positive_label and not fully_observed_negative:
            continue
        labeled += 1
        positive_prediction = float(prediction["risk_score"]) >= risk_threshold
        if positive_label and positive_prediction:
            true_positive += 1
            lead_times.append(float(next_failure) - timestamp)
        elif positive_label:
            false_negative += 1
        elif positive_prediction:
            false_positive += 1
        else:
            true_negative += 1
    predicted_positives = true_positive + false_positive
    actual_positives = true_positive + false_negative
    precision = (
        true_positive / predicted_positives
        if predicted_positives else (0.0 if labeled else None)
    )
    recall = (
        true_positive / actual_positives
        if actual_positives else (0.0 if labeled else None)
    )
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else (0.0 if labeled else None)
    )
    durations = [
        float(item["duration_s"]) for item in recoveries
        if item["duration_s"] is not None
    ]
    unnecessary_restart_count = 0
    unassessed_restart_count = 0
    for recovery in recoveries:
        if recovery["status"] != "healthy":
            continue
        recovery_time = float(recovery["timestamp"])
        observed_after = [
            item for item in measurements
            if recovery_time < float(item["timestamp"])
            <= recovery_time + post_recovery_observation_s
        ]
        enough_observation = bool(
            observed_after
            and float(observed_after[-1]["timestamp"])
            >= recovery_time + post_recovery_observation_s
        )
        if not enough_observation:
            unassessed_restart_count += 1
        elif all(
            item["process_state"] == "running" and bool(item.get("responsive", True))
            for item in observed_after
        ):
            unnecessary_restart_count += 1
    overhead_cpu = [
        float(item["cpu_percent"]) for item in monitoring_samples
        if item["cpu_percent"] is not None
    ]
    overhead_memory = [
        float(item["memory_mb"]) for item in monitoring_samples
        if item["memory_mb"] is not None
    ]
    prediction_model_counts: dict[str, int] = {}
    for item in predictions:
        model_name = str(item.get("model_name", "unknown"))
        prediction_model_counts[model_name] = prediction_model_counts.get(model_name, 0) + 1
    successful_recoveries = sum(item["status"] == "healthy" for item in recoveries)
    return {
        "sample_count": len(measurements),
        "prediction_sample_count": len(predictions),
        "prediction_model_counts": prediction_model_counts,
        "labeled_prediction_samples": labeled,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "true_positives": true_positive,
        "false_positives": false_positive,
        "false_negatives": false_negative,
        "true_negatives": true_negative,
        "false_positive_rate": (
            false_positive / (false_positive + true_negative)
            if false_positive + true_negative else None
        ),
        "confusion_matrix": {
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        "first_early_warning_lead_time_s": lead_times[0] if lead_times else None,
        "recovery_attempts": len(recoveries),
        "successful_recoveries": successful_recoveries,
        "failed_recoveries": sum(item["status"] != "healthy" for item in recoveries),
        "recovery_success_rate": (
            successful_recoveries / len(recoveries) if recoveries else None
        ),
        "mean_recovery_time_s": sum(durations) / len(durations) if durations else None,
        "median_recovery_time_s": median(durations) if durations else None,
        "total_downtime_s": sum(durations),
        "unnecessary_restarts": (
            None if unassessed_restart_count else unnecessary_restart_count
        ),
        "unassessed_unnecessary_restart_count": unassessed_restart_count,
        "unnecessary_restart_definition": (
            f"A restart is counted as operationally unnecessary if the replacement "
            f"remains responsive with no recorded failure for the next "
            f"{post_recovery_observation_s:g} seconds. This is an observed proxy, "
            "not the unobservable counterfactual of what the stopped process would do."
        ),
        "cpu_overhead_percent": mean(overhead_cpu) if overhead_cpu else None,
        "memory_overhead_mb": mean(overhead_memory) if overhead_memory else None,
        "monitoring_overhead_sample_count": len(monitoring_samples),
        "overhead_measurement_method": (
            "Backend process psutil CPU percent (per-core process utilization) and RSS "
            "sampled by the monitor loop during this run; includes API and other backend work."
        ),
        "prediction_positive_threshold": risk_threshold,
        "prediction_positive_definition": f"risk score >= {risk_threshold:.2f}",
        "prediction_horizon_s": horizon_s,
        "label_definition": (
            "A prediction timestamp is positive when a recorded process exit or the "
            "start of three consecutive unresponsive samples falls within the future "
            "horizon; negatives require complete observation through the horizon. "
            "Features use measurements available at the prediction timestamp only."
        ),
    }
