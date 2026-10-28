from fastapi import APIRouter, HTTPException

from app.llm import OllamaUnavailable, analyze_simulation, ollama_status

router = APIRouter(prefix="/api/llm", tags=["local-llm"])


@router.get("/status")
def get_llm_status():
    try:
        return ollama_status()
    except OllamaUnavailable as exc:
        return {
            "available": False,
            "configured_model": None,
            "model_installed": False,
            "installed_models": [],
            "error": str(exc),
        }


@router.post("/simulations/{process_id}/analysis")
def get_simulation_analysis(process_id: str):
    try:
        return analyze_simulation(process_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Simulation worker not found.") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OllamaUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
