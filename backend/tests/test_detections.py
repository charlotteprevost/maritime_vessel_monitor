import os
import sys
import pytest

# Ensure the app module is importable from the parent directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app import app as flask_app
from utils.gfw_client import GFWApiClient


@pytest.fixture
def client():
    """
    Provide a test client configured for the Flask application.
    """
    flask_app.config["TESTING"] = True
    flask_app.config["GFW_CLIENT"] = GFWApiClient("test-token", enable_cache=False)
    return flask_app.test_client()


# --------------------------
# 🔍 Error Handling Tests
# --------------------------

def test_missing_eez_ids(client):
    """
    When no EEZ identifier is provided the API should return a 400 error.
    """
    res = client.get("/api/detections?start_date=2025-04-01&end_date=2025-04-05")
    assert res.status_code == 400


def test_missing_start_date(client):
    """
    The start date is required for detections. Missing it should result in a 400 status.
    """
    res = client.get("/api/detections?eez_ids=France&end_date=2025-04-10")
    assert res.status_code == 400


def test_missing_end_date(client):
    """
    The end date is required for detections. Missing it should result in a 400 status.
    """
    res = client.get("/api/detections?eez_ids=France&start_date=2025-04-01")
    assert res.status_code == 400


def test_start_date_after_end_date_is_rejected(client):
    res = client.get(
        "/api/detections?eez_ids=France&start_date=2025-04-05&end_date=2025-04-01"
    )
    assert res.status_code == 400
    assert "start_date must be <= end_date" in res.json.get("error", "")


def test_invalid_date_format_is_rejected(client):
    res = client.get(
        "/api/detections?eez_ids=France&start_date=2025/04/01&end_date=2025-04-05"
    )
    assert res.status_code == 400
    assert "Invalid date format" in res.json.get("error", "")


def test_invalid_eez_ids(client):
    """
    Supplying an unknown EEZ identifier should still return a response. The status code
    may be 200 if the underlying API handles unknown regions gracefully, or a 400 if
    validation fails.
    """
    res = client.get(
        "/api/detections?eez_ids=Atlantis&start_date=2024-01-01&end_date=2024-01-07"
    )
    assert res.status_code in (200, 400)


def test_multiple_eez_ids(client):
    """
    Multiple EEZ identifiers may be provided. The API should return a 200 status and
    aggregate results for all requested EEZs.
    """
    res = client.get(
        "/api/detections?eez_ids=France&eez_ids=Spain&start_date=2025-04-01&end_date=2025-04-05"
    )
    assert res.status_code == 200


# --------------------------
# ✅ Valid Scenarios
# --------------------------

def test_valid_no_data(client, requests_mock):
    """
    When the GFW report endpoint returns no entries, the detections API should still
    return a tile URL and an empty summaries list.
    """
    requests_mock.post(
        "https://gateway.api.globalfishingwatch.org/v3/4wings/report",
        json={"entries": []},
        status_code=200,
    )
    res = client.get(
        "/api/detections?eez_ids=France&start_date=2025-04-01&end_date=2025-04-05"
    )
    assert res.status_code == 200
    assert "tile_url" in res.json
    assert "summaries" in res.json
    assert isinstance(res.json["summaries"], list)


def test_valid_with_filters(client, requests_mock):
    """
    Filters such as gear type, neural vessel type, or ship type should be passed
    through to the GFW API and still result in a valid response.
    """
    requests_mock.post(
        "https://gateway.api.globalfishingwatch.org/v3/4wings/report",
        json={"entries": []},
        status_code=200,
    )
    res = client.get(
        "/api/detections?eez_ids=France&start_date=2025-04-01&end_date=2025-04-05&geartype=TRAWLERS&neural_vessel_type=Likely%20Fishing&shiptype=FISHING"
    )
    assert res.status_code == 200
    assert "tile_url" in res.json


def test_gfw_api_failure(client, requests_mock):
    """
    If the underlying GFW report endpoint returns an error, the detections API should
    still respond with a 200 status and include an error message within its summaries
    structure.
    """
    requests_mock.post(
        "https://gateway.api.globalfishingwatch.org/v3/4wings/report",
        status_code=500,
        json={"error": "Boom"}
    )
    res = client.get(
        "/api/detections?eez_ids=France&start_date=2025-04-01&end_date=2025-04-05"
    )
    assert res.status_code == 200
    assert "summaries" in res.json
    # At least one summary chunk should contain an error key when failures occur
    assert any(
        ("chunks" in s) and any("error" in c for c in s.get("chunks", []))
        for s in res.json["summaries"]
    )