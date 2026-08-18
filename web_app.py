#!/usr/bin/env python3
"""Web interface for uploading and analysing WebRTC getStats logs."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from webrtc_stats_parser import load_snapshots, parse_snapshots

ALLOWED_EXTENSIONS = {".json", ".jsonl", ".log", ".txt"}


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024,
        SECRET_KEY=os.getenv("SECRET_KEY", "change-me-in-production"),
    )
    if test_config:
        app.config.update(test_config)

    @app.get("/")
    def index():
        return render_template("index.html", max_upload_mb=app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024)

    @app.post("/api/analyze")
    def analyze():
        upload = request.files.get("log")
        if upload is None or not upload.filename:
            return jsonify(error="Выберите файл с логом."), 400
        filename = secure_filename(upload.filename)
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            return jsonify(error="Поддерживаются файлы JSON, JSONL, LOG и TXT."), 400

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                upload.save(temporary)
                temporary_path = Path(temporary.name)
            snapshots = load_snapshots(temporary_path)
            if not snapshots:
                raise ValueError("В файле не найдены записи RTCStats.")
            rows = parse_snapshots(snapshots, request.form.get("call_id", "").strip())
            if not rows:
                raise ValueError("В файле не найдены inbound-rtp или outbound-rtp записи.")
            return jsonify(rows=rows, summary=_build_summary(rows), filename=filename)
        except (OSError, UnicodeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error: RequestEntityTooLarge):
        return jsonify(error="Файл превышает допустимый размер."), 413

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    return app


def _values(rows: list[dict[str, Any]], name: str) -> list[float]:
    return [float(row[name]) for row in rows if row.get(name) is not None]


def _build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    loss = _values(rows, "packet_loss_pct")
    jitter = _values(rows, "jitter_ms")
    rtt = _values(rows, "rtt_ms")
    bitrate = _values(rows, "bitrate_kbps")
    avg_loss = mean(loss) if loss else None
    avg_jitter = mean(jitter) if jitter else None
    avg_rtt = mean(rtt) if rtt else None

    # Practical, transparent thresholds for a quick operational assessment.
    if ((avg_loss or 0) >= 5 or (avg_jitter or 0) >= 50 or (avg_rtt or 0) >= 400):
        quality, score = "Плохое", 1
    elif ((avg_loss or 0) >= 2 or (avg_jitter or 0) >= 30 or (avg_rtt or 0) >= 250):
        quality, score = "Нестабильное", 2
    elif ((avg_loss or 0) >= 1 or (avg_jitter or 0) >= 20 or (avg_rtt or 0) >= 150):
        quality, score = "Удовлетворительное", 3
    elif ((avg_loss or 0) >= 0.3 or (avg_jitter or 0) >= 10 or (avg_rtt or 0) >= 80):
        quality, score = "Хорошее", 4
    else:
        quality, score = "Отличное", 5

    calls = sorted({row["call_id"] for row in rows if row.get("call_id")})
    media = Counter(str(row.get("media_kind", "unknown")) for row in rows)
    return {
        "quality": quality,
        "score": score,
        "samples": len(rows),
        "calls": len(calls) or 1,
        "call_ids": calls,
        "media": media,
        "avg_loss_pct": _rounded_mean(loss),
        "avg_jitter_ms": _rounded_mean(jitter),
        "avg_rtt_ms": _rounded_mean(rtt),
        "avg_bitrate_kbps": _rounded_mean(bitrate),
    }


def _rounded_mean(values: list[float]) -> float | None:
    return round(mean(values), 2) if values else None


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
