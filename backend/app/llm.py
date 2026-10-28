import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.core.database import Database, database

OLLAMA_URL = os.environ.get(
    "PREEMPTGUARD_OLLAMA_URL", "http://127.0.0.1:11434"
).rstrip("/")
OLLAMA_MODEL = os.environ.get("PREEMPTGUARD_OLLAMA_MODEL", "llama3.2")


class OllamaUnavailable(RuntimeError):
    pass


def _request_json(
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{OLLAMA_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise OllamaUnavailable(
            f"Ollama returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise OllamaUnavailable(
            f"Cannot reach Ollama at {OLLAMA_URL}. Start Ollama and verify the configured URL."
        ) from exc
    except json.JSONDecodeError as exc:
        raise OllamaUnavailable("Ollama returned invalid JSON.") from exc
    if not isinstance(result, dict):
        raise OllamaUnavailable("Ollama returned an unexpected response.")
    return result


def ollama_status() -> dict[str, Any]:
    result = _request_json("/api/tags", timeout_s=2.0)
    models = result.get("models", [])
    if not isinstance(models, list):
        raise OllamaUnavailable("Ollama model-list response has an invalid shape.")
    model_names = [
        item["name"] for item in models
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    return {
        "available": True,
        "configured_model": OLLAMA_MODEL,
        "model_installed": any(
            name == OLLAMA_MODEL or name.startswith(f"{OLLAMA_MODEL}:")
            for name in model_names
        ),
        "installed_models": model_names,
        "url": OLLAMA_URL,
    }


def _latest_rows(db: Database, table: str, process_id: str, limit: int) -> list[dict[str, Any]]:
    allowed_tables = {"measurements", "predictions", "decisions", "recovery_attempts"}
    if table not in allowed_tables:
        raise ValueError(f"Unsupported history table: {table}")
    rows = db.query(
        f"SELECT * FROM {table} WHERE process_id=? ORDER BY timestamp DESC LIMIT ?",
        (process_id, limit),
    )
    rows.reverse()
    return rows


def _simulation_only_context(process_id: str, db: Database) -> dict[str, Any]:
    rows = db.query(
        "SELECT id,workload_type,policy,status,config_json FROM managed_processes WHERE id=?",
        (process_id,),
    )
    if not rows:
        raise KeyError(process_id)
    process = rows[0]
    config = json.loads(process["config_json"])
    if config.get("simulation") is not True:
        raise PermissionError("LLM analysis is only enabled for isolated simulation workers.")

    similar_cases: list[dict[str, Any]] = []
    candidates = db.query(
        """SELECT id,workload_type,status,config_json FROM managed_processes
        WHERE workload_type=? AND id<>? ORDER BY started_at DESC LIMIT 20""",
        (process["workload_type"], process_id),
    )
    for candidate in candidates:
        candidate_config = json.loads(candidate["config_json"])
        if candidate_config.get("simulation") is not True:
            continue
        candidate_measurements = _latest_rows(db, "measurements", candidate["id"], 1)
        candidate_recoveries = _latest_rows(db, "recovery_attempts", candidate["id"], 1)
        similar_cases.append({
            "scenario": candidate["workload_type"],
            "worker_status": candidate["status"],
            "last_measurement": candidate_measurements[-1] if candidate_measurements else None,
            "last_recovery": candidate_recoveries[-1] if candidate_recoveries else None,
        })
        if len(similar_cases) == 5:
            break

    return {
        "scenario": process["workload_type"],
        "worker_status": process["status"],
        "measurements": _latest_rows(db, "measurements", process_id, 12),
        "risk_predictions": _latest_rows(db, "predictions", process_id, 12),
        "policy_decisions": _latest_rows(db, "decisions", process_id, 12),
        "recovery_attempts": _latest_rows(db, "recovery_attempts", process_id, 5),
        "similar_local_simulation_cases": similar_cases,
    }


def analyze_simulation(
    process_id: str,
    db: Database = database,
) -> dict[str, Any]:
    context = _simulation_only_context(process_id, db)
    result = _request_json(
        "/api/chat",
        timeout_s=60.0,
        payload={
            "model": OLLAMA_MODEL,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 350},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a local assistant analyzing an isolated synthetic "
                        "PreemptGuard worker, never a real computer process. Use only the "
                        "provided worker samples and similar synthetic simulation cases. "
                        "Explain observed health and risk evidence, what PreemptGuard has "
                        "already done, and a cautious next step. Do not claim that a "
                        "recovery succeeded unless a recovery record says so. Do not "
                        "instruct the application to execute commands. Risk scores are "
                        "heuristics, not failure probabilities. Be concise and state when "
                        "there is insufficient data."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(context, separators=(",", ":")),
                },
            ],
        },
    )
    message = result.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise OllamaUnavailable("Ollama response did not contain a message.")
    return {
        "process_id": process_id,
        "model": OLLAMA_MODEL,
        "analysis": message["content"],
        "similar_case_count": len(context["similar_local_simulation_cases"]),
        "context": context,
        "source": "local_simulation_history_only",
    }
