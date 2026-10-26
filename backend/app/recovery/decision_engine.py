from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    estimated_cost: float


def _costs(risk_score: float, confidence: float) -> dict[str, float]:
    return {
        "continue monitoring": risk_score * 5.0 + (1.0 - confidence) * 3.0,
        "recheck": 1.2 + risk_score * 1.2,
        "restart": 2.0 + (1.0 - confidence) * 1.5,
    }


def choose_action(
    *,
    policy: str,
    risk_score: float,
    confidence: float,
    low_threshold: float,
    high_threshold: float,
    responsive: bool,
    failed: bool,
    restart_count: int,
    restart_limit: int,
    recheck_due: bool = False,
    threshold_detected: bool = False,
    confirmed_unresponsive: bool = False,
    minimum_confidence: float = 0.55,
    workload_type: str = "",
    operation_status: str | None = None,
    operation_retry_count: int = 0,
    max_operation_retries: int = 2,
    cpu_overloaded: bool = False,
    throttled: bool = False,
) -> Decision:
    if failed:
        if restart_count >= restart_limit:
            return Decision("investigate", "Restart limit reached after confirmed failure.", 5.0)
        return Decision("restart", "Managed process exited; confirmed failure requires recovery.", 2.0)
    if policy == "fixed":
        if confirmed_unresponsive:
            if restart_count >= restart_limit:
                return Decision("investigate", "Fixed-baseline timeout reached the restart limit.", 5.0)
            return Decision("restart", "Fixed baseline recovery timeout confirmed a stale heartbeat.", 2.0)
        return Decision("continue monitoring", "Fixed baseline waits for confirmed process failure.", 0.0)
    if policy == "threshold":
        if threshold_detected:
            if restart_count >= restart_limit:
                return Decision("investigate", "Threshold was exceeded but the restart limit has been reached.", 5.0)
            return Decision("restart", "Threshold baseline detected a persistent resource or heartbeat threshold.", 3.0)
        return Decision("continue monitoring", "No configured threshold has been exceeded for enough samples.", 0.0)
    if not responsive and confirmed_unresponsive:
        if restart_count >= restart_limit:
            return Decision(
                "investigate",
                "Confirmed heartbeat failure reached the restart limit.",
                5.0,
            )
        return Decision(
            "restart",
            "The managed process remained unresponsive through the recheck interval.",
            2.0,
        )

    if policy in ("preemptguard", "predictive_fixed", "confidence_recheck"):
        if operation_status == "failed":
            if operation_retry_count < max_operation_retries:
                return Decision(
                    "retry",
                    "Temporary managed operation failed; retry it without restarting the process.",
                    0.7,
                )
            if restart_count >= restart_limit:
                return Decision(
                    "investigate",
                    "Operation retry limit and process restart limit were reached.",
                    5.0,
                )
            return Decision(
                "restart",
                "Temporary operation retries were exhausted; restart the managed workload.",
                2.0,
            )
        if workload_type in ("cpu", "cpu_memory") and cpu_overloaded and not throttled:
            return Decision(
                "throttle",
                "Sustained CPU load detected; reduce the managed worker's CPU duty cycle before restarting.",
                0.9,
            )

    if policy == "predictive_fixed":
        if risk_score >= high_threshold:
            if restart_count >= restart_limit:
                return Decision("investigate", "Restart limit reached at the predictive threshold.", 5.0)
            return Decision("restart", "Predictive risk crossed the fixed recovery threshold.", 2.0)
        return Decision("continue monitoring", "Risk is below the fixed predictive recovery threshold.", 0.0)

    costs = _costs(risk_score, confidence)
    if policy == "confidence_recheck" and confidence < minimum_confidence:
        return Decision(
            "recheck",
            "Evidence confidence is below the configured minimum; collect another observation.",
            costs["recheck"],
        )
    if not responsive and not confirmed_unresponsive:
        return Decision(
            "recheck",
            f"Heartbeat is temporarily stale; recheck cost ({costs['recheck']:.2f}) "
            f"is below restart cost ({costs['restart']:.2f}).",
            costs["recheck"],
        )
    if not responsive:
        if restart_count >= restart_limit:
            return Decision("investigate", "Process is unresponsive and the restart limit has been reached.", 5.0)
        return Decision(
            "restart",
            f"Heartbeat failure is confirmed; restart cost ({costs['restart']:.2f}) "
            f"is below delay cost ({costs['continue monitoring']:.2f}).",
            costs["restart"],
        )
    if recheck_due and risk_score < high_threshold:
        return Decision(
            "continue monitoring",
            "Recheck lowered risk below the restart threshold; avoided a restart.",
            costs["continue monitoring"],
        )
    if risk_score >= high_threshold and confidence >= minimum_confidence:
        if restart_count >= restart_limit:
            return Decision("investigate", "High risk observed but the restart limit has been reached.", 5.0)
        if costs["restart"] <= costs["continue monitoring"]:
            return Decision(
                "restart",
                f"High risk and sufficient history; restart cost ({costs['restart']:.2f}) "
                f"is below delay cost ({costs['continue monitoring']:.2f}).",
                costs["restart"],
            )
    if risk_score >= low_threshold or confidence < 0.5:
        if costs["recheck"] < costs["continue monitoring"]:
            return Decision(
                "recheck",
                f"Risk is moderate or evidence is uncertain; recheck cost "
                f"({costs['recheck']:.2f}) is below continued-delay cost "
                f"({costs['continue monitoring']:.2f}).",
                costs["recheck"],
            )
    return Decision(
        "continue monitoring",
        f"Low expected risk; continued-monitoring cost "
        f"({costs['continue monitoring']:.2f}) is the lowest action cost.",
        costs["continue monitoring"],
    )
