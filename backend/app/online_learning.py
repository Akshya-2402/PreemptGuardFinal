import json
import math
import time
from collections import Counter
from typing import Any

from app.core.database import Database, database

RECOVERY_ACTIONS = {
    "resume",
    "lower_priority",
    "restore_priority",
    "terminate",
    "restart_configured",
    "retry",
}
FEATURE_SCALES = {
    "cpu_percent": 100.0,
    "memory_percent": 100.0,
    "growth_mb_s": 10.0,
    "risk_score": 1.0,
}
HIGH_RISK_THRESHOLD = 0.65


def process_features(sample: dict[str, Any]) -> dict[str, float]:
    return {
        name: min(1.0, max(0.0, float(sample.get(name) or 0.0) / scale))
        for name, scale in FEATURE_SCALES.items()
    }


def record_feedback(
    *,
    pid: int,
    process_started_at: float,
    process_name: str,
    action: str,
    action_run_id: int,
    helped: bool | None,
    features: dict[str, float],
    db: Database = database,
) -> None:
    if action not in RECOVERY_ACTIONS:
        raise ValueError(f"Unsupported recovery action: {action}")
    db.execute(
        """INSERT INTO host_recovery_feedback
        (pid,process_started_at,process_name,action,helped,action_run_id,
        features_json,source,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            pid,
            process_started_at,
            process_name,
            action,
            int(helped) if helped is not None else None,
            action_run_id,
            json.dumps(features, separators=(",", ":")),
            "actual_process_user_feedback",
            time.time(),
        ),
    )


def load_training_examples(db: Database = database) -> list[dict[str, Any]]:
    examples = [
        {
            "action": row["action"],
            "helped": bool(row["helped"]),
            "features": json.loads(row["features_json"]),
            "source": "actual",
        }
        for row in db.query(
            """SELECT action,helped,features_json FROM host_recovery_feedback
            WHERE helped IS NOT NULL
            ORDER BY created_at DESC LIMIT 1000"""
        )
    ]
    simulation_rows = db.query(
        """SELECT r.status,r.timestamp,p.config_json,
        m.cpu_percent,m.memory_percent,m.memory_growth_mb_s,m.process_state,
        m.responsive,(SELECT pr.risk_score FROM predictions pr
            WHERE pr.process_id=r.process_id AND pr.timestamp<=r.timestamp
            ORDER BY pr.timestamp DESC LIMIT 1) AS risk_score
        FROM recovery_attempts r
        JOIN managed_processes p ON p.id=r.process_id
        LEFT JOIN measurements m ON m.id=(
            SELECT m2.id FROM measurements m2 WHERE m2.process_id=r.process_id
            AND m2.timestamp<=r.timestamp ORDER BY m2.timestamp DESC LIMIT 1
        )
        ORDER BY r.timestamp DESC LIMIT 500"""
    )
    for row in simulation_rows:
        try:
            is_simulation = json.loads(row["config_json"]).get("simulation") is True
        except (json.JSONDecodeError, TypeError):
            continue
        if not is_simulation or row["status"] not in {"healthy", "failed"}:
            continue
        examples.append({
            "action": "restart_configured",
            "helped": row["status"] == "healthy",
            "features": process_features({
                "cpu_percent": row["cpu_percent"],
                "memory_percent": row["memory_percent"],
                "growth_mb_s": row["memory_growth_mb_s"],
                "risk_score": row["risk_score"],
            }),
            "source": "simulation",
        })
    return examples


def _distance(left: dict[str, float], right: dict[str, float]) -> float:
    return math.sqrt(sum(
        (float(left.get(name, 0.0)) - float(right.get(name, 0.0))) ** 2
        for name in FEATURE_SCALES
    ))


def recommend_action(
    sample: dict[str, Any],
    db: Database = database,
    examples: list[dict[str, Any]] | None = None,
    require_high_risk: bool = True,
) -> dict[str, Any]:
    query = process_features(sample)
    if examples is None:
        examples = load_training_examples(db)
    if require_high_risk and query["risk_score"] < HIGH_RISK_THRESHOLD:
        return {
            "recommendation": None,
            "reason": "Recovery recommendations are limited to high or critical risk.",
            "estimates": [],
            "training_example_count": len(examples),
            "training_counts_by_action": dict(Counter(example["action"] for example in examples)),
            "model": "local_online_case_based_v1",
            "confidence_kind": "similar_observed_outcomes_not_guaranteed",
        }
    by_action: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for example in examples:
        action = example["action"]
        if action in RECOVERY_ACTIONS and action != "resume":
            by_action.setdefault(action, []).append((
                _distance(query, example["features"]),
                example,
            ))

    estimates: list[dict[str, Any]] = []
    for action, cases in by_action.items():
        nearest = [
            case for case in sorted(cases, key=lambda case: case[0])
            if case[0] <= 0.5
        ][:20]
        if len(nearest) < 3:
            continue
        weights = [math.exp(-4.0 * distance) for distance, _ in nearest]
        weight_total = sum(weights)
        weighted_successes = sum(
            weight * int(example["helped"])
            for weight, (_, example) in zip(weights, nearest)
        )
        success_rate = (weighted_successes + 1.0) / (weight_total + 2.0)
        estimates.append({
            "action": action,
            "estimated_success_rate": round(success_rate, 3),
            "similar_outcomes": len(nearest),
            "sources": dict(Counter(example["source"] for _, example in nearest)),
        })

    estimates.sort(
        key=lambda item: (item["estimated_success_rate"], item["similar_outcomes"]),
        reverse=True,
    )
    recommendation = (
        estimates[0]["action"]
        if estimates
        and estimates[0]["similar_outcomes"] >= 5
        and estimates[0]["estimated_success_rate"] >= 0.7
        else None
    )
    counts = Counter(example["action"] for example in examples)
    return {
        "recommendation": recommendation,
        "reason": (
            "Selected from locally stored similar recovery outcomes."
            if recommendation else
            "Not enough similar successful outcomes to recommend an action."
        ),
        "estimates": estimates,
        "training_example_count": len(examples),
        "training_counts_by_action": dict(counts),
        "model": "local_online_case_based_v1",
        "confidence_kind": "similar_observed_outcomes_not_guaranteed",
    }
