import io
import json

from web_app import _build_summary, create_app


def _payload():
    return [{"timestamp": 1_700_000_000_000, "call_id": "demo", "stats": [
        {"id": "in-1", "type": "inbound-rtp", "kind": "audio", "packetsReceived": 98, "packetsLost": 2, "jitter": 0.025, "bytesReceived": 1000}
    ]}]


def test_index_and_health():
    client = create_app({"TESTING": True}).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/health").json == {"status": "ok"}


def test_upload_returns_rows_and_quality():
    client = create_app({"TESTING": True}).test_client()
    response = client.post("/api/analyze", data={"log": (io.BytesIO(json.dumps(_payload()).encode()), "call.json")})
    assert response.status_code == 200
    assert response.json["summary"]["quality"] == "Нестабильное"
    assert response.json["rows"][0]["jitter_ms"] == 25.0


def test_invalid_extension_is_rejected():
    client = create_app({"TESTING": True}).test_client()
    response = client.post("/api/analyze", data={"log": (io.BytesIO(b"x"), "call.exe")})
    assert response.status_code == 400


def test_bad_quality_threshold():
    summary = _build_summary([{"packet_loss_pct": 6, "jitter_ms": 10, "rtt_ms": 20, "media_kind": "audio"}])
    assert summary["score"] == 1
