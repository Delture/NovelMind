"""Destination changes cannot silently forward a previous endpoint's credentials."""

from copy import deepcopy

import pytest


ROLES = ("generation", "embedding")
KEY_FIELDS = {"generation": "deepseek_api_key", "embedding": "embedding_api_key"}
URL_FIELDS = {"generation": "deepseek_base_url", "embedding": "embedding_base_url"}


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("key", [None, "", "  \t  "])
@pytest.mark.parametrize("destination", ["https://different.example/v1", "https://{role}-a.example/v2"])
def test_endpoint_change_without_new_key_is_rejected_atomically(isolated_config, role, key, destination):
    case = isolated_config
    before_bytes, before_settings = case.path.read_bytes(), case.config.get_settings()
    payload = deepcopy(case.payload)
    payload[role].update(base_url=destination.format(role=role), api_key=key)
    # The other role is validly changed too: a rejected request must commit neither role.
    other = next(item for item in ROLES if item != role)
    payload[other]["model"] = "must-not-be-saved"
    response = case.client.put("/api/system/model-settings", json=payload)
    assert response.status_code == 400, response.text
    assert case.path.read_bytes() == before_bytes
    assert case.config.get_settings() == before_settings
    assert "review-" not in response.text


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("action", ["replace", "clear"])
def test_endpoint_change_accepts_explicit_key_lifecycle(isolated_config, role, action):
    case = isolated_config
    old = case.config.get_settings()
    payload = deepcopy(case.payload)
    payload[role]["base_url"] = "https://different.example/v1"
    if action == "replace":
        payload[role]["api_key"] = "  review-replacement-not-real  "
    else:
        payload[role]["clear_api_key"] = True
    response = case.client.put("/api/system/model-settings", json=payload)
    assert response.status_code == 200, response.text
    saved = case.config.get_settings()
    expected = "review-replacement-not-real" if action == "replace" else None
    assert getattr(saved, KEY_FIELDS[role]) == expected
    other = next(item for item in ROLES if item != role)
    assert getattr(saved, KEY_FIELDS[other]) == getattr(old, KEY_FIELDS[other])
    assert response.json()[role]["api_key_set"] is (action == "replace")
    assert "not-real" not in response.text


@pytest.mark.parametrize("role", ROLES)
def test_disk_replace_failure_preserves_file_and_live_settings(isolated_config, monkeypatch, role):
    case = isolated_config
    original, old = case.path.read_bytes(), case.config.get_settings()
    payload = deepcopy(case.payload)
    payload[role].update(base_url="https://different.example/v1", api_key="review-new-not-real")

    def fail_replace(*args, **kwargs):
        raise OSError("simulated temporary-disk write failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(case.config.os, "replace", fail_replace)
        response = case.client.put("/api/system/model-settings", json=payload)
    assert response.status_code == 400, response.text
    assert case.path.read_bytes() == original
    assert case.config.get_settings() == old
    assert not list(case.root.glob(".*.tmp"))
