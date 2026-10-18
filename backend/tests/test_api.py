from __future__ import annotations

import csv
import io

from fastapi.testclient import TestClient

from app import repository
from app.main import app


def test_end_to_end_api_run_history_detail_and_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DEFAULT_DB", tmp_path / "gridwise-test.db")
    with TestClient(app) as client:
        homepage = client.get("/")
        assert homepage.status_code == 200
        assert "GridWise EMS" in homepage.text
        script = client.get("/static/app.js")
        stylesheet = client.get("/static/styles.css")
        assert script.status_code == 200 and "api('/api/simulate'" in script.text
        assert stylesheet.status_code == 200 and "--bg" in stylesheet.text

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "simulation-only"

        defaults = client.get("/api/defaults")
        assert defaults.status_code == 200
        config = defaults.json()
        config["days"] = 1
        config["site_name"] = "API Test Plant"

        created = client.post("/api/simulate", json=config)
        assert created.status_code == 200, created.text
        result = created.json()
        assert result["run_id"] > 0
        assert "forecast_mpc" in result["strategies"]

        runs = client.get("/api/runs")
        assert runs.status_code == 200
        assert any(row["id"] == result["run_id"] for row in runs.json()["items"])

        detail = client.get(f"/api/runs/{result['run_id']}")
        assert detail.status_code == 200
        assert detail.json()["config"]["site_name"] == "API Test Plant"

        exported = client.get(f"/api/runs/{result['run_id']}/export.csv?strategy=forecast_mpc")
        assert exported.status_code == 200
        parsed = list(csv.DictReader(io.StringIO(exported.text)))
        assert len(parsed) == 96
        assert "grid_import_kva" in parsed[0]
        assert "load_kw" in parsed[0]
        assert "battery_soc_pct" in parsed[0]


def test_bad_run_id_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr(repository, "DEFAULT_DB", tmp_path / "gridwise-test.db")
    with TestClient(app) as client:
        response = client.get("/api/runs/9999")
        assert response.status_code == 404
