"""Check browser-server isolation and probe wiring without launching a browser."""

import json
import os
from pathlib import Path
import subprocess
import sys


PROGRAM = r'''
import json
import httpx
from fastapi.testclient import TestClient
from tests.web.web_browser_server import app

with TestClient(app) as client:
    original = client.get("/api/system/model-settings").json()
    assert original["generation"]["api_key_set"] is True
    assert client.post("/api/qa/probe-records").json()["items"] == []
    draft = dict(role="generation", provider="deepseek", base_url="https://api.deepseek.com",
                 use_saved_key=True, confirm_connection=True)
    response = client.post("/api/system/model-probe", json=draft)
    assert response.status_code == 200 and response.json()["status"] == "success", response.text
    assert response.json()["models"][0]["id"] == "deepseek-v4-pro"
    records = client.post("/api/qa/probe-records").json()["items"]
    assert len(records) == 1 and records[0]["method"] == "GET" and records[0]["auth_present"]
    assert "test-key-never-sent" not in json.dumps(records)

    assert client.post("/api/qa/probe-scenario",json=dict(mode="401")).status_code == 200
    response = client.post("/api/system/model-probe", json=draft)
    assert response.json()["status"] == "authentication_failed"
    assert "UNTRUSTED_REMOTE_FIXTURE" not in response.text and "test-key-never-sent" not in response.text

    update = {
        "generation":dict(provider="deepseek",model="fixture-saved-chat",base_url="https://api.deepseek.com"),
        "embedding":dict(provider="siliconflow",model="Qwen/Qwen3-Embedding-4B",base_url="https://api.siliconflow.cn/v1",dimensions=1024)
    }
    assert client.put("/api/system/model-settings",json=update).status_code == 200
    assert client.get("/api/system/model-settings").json()["generation"]["model"] == "fixture-saved-chat"
    reset = client.delete("/api/system/model-settings")
    assert reset.status_code == 200 and reset.json()["generation"]["configured"] is False
    assert client.get("/api/system/model-settings").json()["generation"]["api_key_set"] is False

    try:
        with httpx.Client(trust_env=False) as real_transport:
            real_transport.get("https://must-not-resolve.invalid/models")
    except AssertionError as error:
        assert "real HTTP transport" in str(error)
    else:
        raise AssertionError("A real HTTP transport was not blocked")
print(json.dumps({"safe_probe_wiring":True,"live_save_restore_observed":True,"real_transport_blocked":True}))
'''


def test_browser_probe_is_fake_and_settings_changes_are_not_masked(tmp_path):
    """The child prevents test-only transport guards from leaking into other tests."""
    directory = tmp_path / "novelmind-web-probe-fixture"
    directory.mkdir()
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith(("DEEPSEEK_", "EMBEDDING_", "GENERATION_", "SUMMARY_")):
            env.pop(key)
    env.update(PYTHON_DOTENV_DISABLED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8",
               NOVELMIND_QA="isolated", DATABASE_URL="sqlite:///"+(directory/"test.db").as_posix(),
               MODEL_SETTINGS_PATH=str(directory/"models.json"), VECTOR_STORE_PATH=str(directory/"vectors"),
               DEEPSEEK_API_KEY="", EMBEDDING_API_KEY="")
    result = subprocess.run([sys.executable, "-c", PROGRAM], env=env,
                            cwd=Path(__file__).resolve().parents[2],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout.strip())
    assert all(data.values())
