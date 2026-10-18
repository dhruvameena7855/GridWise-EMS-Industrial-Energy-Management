from __future__ import annotations

import csv
import io
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import SimulationConfig, OutageRequest, ScenarioCompareRequest
from .repository import initialize, save_run, list_runs, get_run
from .simulator import run_simulation, run_outage_test, run_sizing_sweep, TARIFF_SOURCES

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    yield


app = FastAPI(
    title="GridWise EMS API",
    description=(
        "Simulation-only industrial energy management system with solar PV, BESS, "
        "kVA/PF-aware billing, DG commitment, rolling-horizon optimization and validation metrics."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def homepage() -> FileResponse:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=503, detail="Frontend asset is not installed")
    return FileResponse(index)


@app.get("/styles.css", include_in_schema=False)
def static_styles() -> FileResponse:
    return FileResponse(STATIC_DIR / "styles.css")


@app.get("/app.js", include_in_schema=False)
def static_app_js() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.js")


@app.get("/demo_data.json", include_in_schema=False)
def static_demo_data() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo_data.json")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "gridwise-ems-api", "mode": "simulation-only", "version": "1.0.0"}


@app.get("/api/defaults")
def defaults() -> dict[str, Any]:
    return SimulationConfig().model_dump()


@app.get("/api/presets")
def presets() -> dict[str, Any]:
    return {
        "industry": [
            {"id": "factory_shift", "label": "Shift-based manufacturing plant", "description": "Day/evening production shifts with overnight base load."},
            {"id": "jio_datacenter", "label": "24×7 data-centre archetype", "description": "Flat critical load with cooling-related variation; illustrative only, not Jio site data."},
            {"id": "refinery_utilities", "label": "Refinery utilities archetype", "description": "High base load with pumps/compressors; illustrative only, not Reliance refinery data."},
        ],
        "tariff": [
            {"id": "illustrative_tou", "label": "Illustrative time-of-day tariff", "description": "Custom demonstration rates: off-peak, normal and peak prices are assumptions."},
            {"id": "rajasthan_jvvnl_ht5_fy2024_25", "label": "Rajasthan JVVNL HT-5 historical reference (FY 2024–25)", "description": "Historical RERC order reference: ₹7.30/kWh and ₹300/kVA/month for the specified HT-5 large-industry category/rate schedule; verify later orders before real use.", "source_url": TARIFF_SOURCES["rajasthan_jvvnl_ht5_fy2024_25"]["source_url"]},
        ],
    }


@app.post("/api/simulate")
def simulate(cfg: SimulationConfig) -> dict[str, Any]:
    result = run_simulation(cfg)
    result["run_id"] = save_run(result)
    return result


@app.get("/api/runs")
def runs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"items": list_runs(limit)}


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    result = get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Simulation run not found")
    result["run_id"] = run_id
    return result


@app.get("/api/runs/{run_id}/export.csv")
def export_run_csv(run_id: int, strategy: str = Query(default="forecast_mpc")) -> StreamingResponse:
    result = get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Simulation run not found")
    if strategy not in result.get("strategies", {}):
        raise HTTPException(status_code=422, detail=f"Unknown strategy: {strategy}")
    profiles = result.get("series", [])
    selected = result["strategies"][strategy]["series"]
    buffer = io.StringIO()
    fieldnames = list(dict.fromkeys([key for item in profiles for key in item.keys()] + [key for item in selected for key in item.keys()]))
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    if fieldnames:
        writer.writeheader()
        for profile, row in zip(profiles, selected):
            merged = {**profile, **row}
            writer.writerow({key: merged.get(key, "") for key in fieldnames})
    response = StreamingResponse(iter([buffer.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="gridwise_run_{run_id}_{strategy}.csv"'
    return response


@app.post("/api/outage-test")
def outage_test(req: OutageRequest, run_id: int | None = Query(default=None, ge=1)) -> dict[str, Any]:
    if run_id is not None:
        result = get_run(run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Simulation run not found")
    else:
        result = run_simulation(SimulationConfig())
    return run_outage_test(result, req)


@app.post("/api/scenarios/compare")
def compare_scenarios(req: ScenarioCompareRequest) -> dict[str, Any]:
    configs = [req.base, *req.alternatives]
    output = []
    for index, cfg in enumerate(configs):
        result = run_simulation(cfg)
        output.append({
            "scenario_index": index,
            "scenario_name": cfg.site_name,
            "config": cfg.model_dump(),
            "strategies": {key: value["metrics"] for key, value in result["strategies"].items()},
            "forecasting_benchmark": result["forecasting_benchmark"],
            "optimizer": result["optimizer"],
        })
    return {"items": output, "note": "Scenario comparison uses synthetic data and the selected tariff assumptions."}


@app.post("/api/sizing-sweep")
def sizing_sweep(cfg: SimulationConfig) -> dict[str, Any]:
    return run_sizing_sweep(cfg)
