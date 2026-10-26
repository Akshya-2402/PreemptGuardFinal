import argparse
import json
import math
import os
import time
from pathlib import Path

from app.workloads.files import atomic_replace_with_retry


def read_control(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_heartbeat(path: Path, operation_status: str = "succeeded") -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({
            "timestamp": time.time(),
            "operation_status": operation_status,
            "cpu_time_s": time.process_time(),
        }),
        encoding="utf-8",
    )
    atomic_replace_with_retry(temporary, path)


def run(
    workload_type: str,
    heartbeat_path: Path,
    duration_s: int,
    fault_after_s: int,
    memory_rate_mb_s: float,
    memory_limit_mb: int,
    control_path: Path,
) -> None:
    started = time.monotonic()
    allocations: list[bytearray] = []
    allocated_mb = 0
    operation_status = "succeeded"
    completed_retries = 0
    while time.monotonic() - started < duration_s:
        elapsed = time.monotonic() - started
        controls = read_control(control_path)
        if workload_type in ("crash", "repeated_failure") and elapsed >= fault_after_s:
            raise SystemExit(23)
        if workload_type == "temporary_io" and elapsed >= fault_after_s:
            requested_retries = int(controls.get("retry_count", 0))
            if requested_retries > completed_retries:
                completed_retries = requested_retries
                operation_status = "succeeded"
            elif completed_retries == 0:
                operation_status = "failed"
        sleep_duration = 0.5
        if workload_type in ("cpu", "cpu_memory"):
            throttled = bool(controls.get("throttle"))
            busy_duration = 0.05 if throttled else 0.45
            sleep_duration = 0.45 if throttled else 0.05
            busy_until = time.perf_counter() + busy_duration
            value = 0.0
            while time.perf_counter() < busy_until:
                value = math.sin(value + 1.0)
        if workload_type in ("memory", "cpu_memory") and allocated_mb < memory_limit_mb:
            amount = min(memory_rate_mb_s, memory_limit_mb - allocated_mb, 2.0)
            allocations.append(bytearray(int(amount * 1024 * 1024)))
            allocated_mb += amount
        is_unresponsive = (
            workload_type == "unresponsive"
            and elapsed >= fault_after_s
        ) or (
            workload_type == "intermittent"
            and fault_after_s <= elapsed < fault_after_s + 3
        )
        if not is_unresponsive:
            write_heartbeat(heartbeat_path, operation_status)
        time.sleep(sleep_duration)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workload-type", required=True)
    parser.add_argument("--heartbeat-path", required=True)
    parser.add_argument("--duration-s", required=True, type=int)
    parser.add_argument("--fault-after-s", required=True, type=int)
    parser.add_argument("--memory-rate-mb-s", required=True, type=float)
    parser.add_argument("--memory-limit-mb", required=True, type=int)
    parser.add_argument("--control-path", required=True)
    args = parser.parse_args()
    run(
        args.workload_type,
        Path(args.heartbeat_path),
        args.duration_s,
        args.fault_after_s,
        args.memory_rate_mb_s,
        args.memory_limit_mb,
        Path(args.control_path),
    )


if __name__ == "__main__":
    main()
