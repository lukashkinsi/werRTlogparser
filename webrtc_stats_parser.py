#!/usr/bin/env python3
"""Normalize WebRTC getStats() logs for Grafana ingestion.

Accepted input:
  1. JSON array of snapshots: [{"timestamp": ..., "stats": [...]}, ...]
  2. JSONL with one snapshot per line.
  3. A single RTCStatsReport represented as a list or id->stat object.

The output is either a JSON array or CSV with one row per RTP stream and
snapshot. Times in WebRTC metrics (jitter and RTT) are converted to ms.
Bitrate is calculated from byte-counter deltas between adjacent snapshots.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


OUTPUT_FIELDS = [
    "timestamp",
    "call_id",
    "direction",
    "media_kind",
    "ssrc",
    "packets",
    "packets_lost",
    "packet_loss_pct",
    "jitter_ms",
    "rtt_ms",
    "bitrate_kbps",
    "codec",
    "ice_candidate_type",
]


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: Any) -> tuple[str, float | None]:
    """Return ISO-8601 UTC and epoch milliseconds used for delta calculations."""
    numeric = _number(value)
    if numeric is not None:
        # RTCStats timestamps are normally epoch milliseconds. Small values may
        # be monotonic milliseconds; keep them usable for delta calculations.
        if numeric > 10_000_000_000:
            epoch_ms = numeric
            dt = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z"), epoch_ms
        if numeric > 1_000_000_000:
            epoch_ms = numeric * 1000
            dt = datetime.fromtimestamp(numeric, tz=timezone.utc)
            return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z"), epoch_ms
        return str(value), numeric

    if isinstance(value, str):
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ), dt.timestamp() * 1000
        except ValueError:
            return text, None

    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z"), now.timestamp() * 1000


def _looks_like_stat(value: Any) -> bool:
    return isinstance(value, dict) and isinstance(value.get("type"), str)


def _stats_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if _looks_like_stat(item)]
    if isinstance(value, dict):
        if _looks_like_stat(value):
            return [value]
        return [item for item in value.values() if _looks_like_stat(item)]
    return []


def _normalize_snapshots(payload: Any) -> list[dict[str, Any]]:
    """Convert common JSON shapes to {timestamp, call_id, stats} snapshots."""
    candidates: list[Any]
    if isinstance(payload, list):
        # A bare list of RTCStats objects is one snapshot; a list of wrappers is many.
        candidates = [{"stats": payload}] if payload and all(_looks_like_stat(x) for x in payload) else payload
    else:
        candidates = [payload]

    snapshots: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        wrapped = candidate.get("stats", candidate.get("report", candidate.get("rtcStats")))
        stats = _stats_list(wrapped if wrapped is not None else candidate)
        if not stats:
            continue
        timestamp = candidate.get("timestamp") or candidate.get("ts")
        if timestamp is None:
            timestamp = next((s.get("timestamp") for s in stats if s.get("timestamp") is not None), None)
        snapshots.append(
            {
                "timestamp": timestamp,
                "call_id": candidate.get("call_id") or candidate.get("callId"),
                "stats": stats,
            }
        )
    return snapshots


def load_snapshots(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        return []
    try:
        return _normalize_snapshots(json.loads(text))
    except json.JSONDecodeError:
        snapshots: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                snapshots.extend(_normalize_snapshots(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc
        return snapshots


def _codec_name(stat: dict[str, Any] | None) -> str | None:
    if not stat:
        return None
    mime = stat.get("mimeType") or stat.get("name")
    if not mime:
        return None
    clock = stat.get("clockRate")
    return f"{mime}/{clock}" if clock else str(mime)


def _selected_candidate_type(stats: list[dict[str, Any]]) -> str | None:
    by_id = {str(s.get("id")): s for s in stats if s.get("id") is not None}
    transports = [s for s in stats if s.get("type") == "transport"]
    selected_pair_ids = {str(s.get("selectedCandidatePairId")) for s in transports if s.get("selectedCandidatePairId")}
    pairs = [s for s in stats if s.get("type") == "candidate-pair"]
    selected = next(
        (
            p
            for p in pairs
            if str(p.get("id")) in selected_pair_ids
            or p.get("selected") is True
            or (p.get("nominated") is True and p.get("state") == "succeeded")
        ),
        None,
    )
    if not selected:
        return None
    local = by_id.get(str(selected.get("localCandidateId")))
    remote = by_id.get(str(selected.get("remoteCandidateId")))
    local_type = (local or {}).get("candidateType")
    remote_type = (remote or {}).get("candidateType")
    if local_type and remote_type:
        return f"{local_type}->{remote_type}"
    return local_type or remote_type


def _candidate_rtt_ms(stats: list[dict[str, Any]]) -> float | None:
    pairs = [s for s in stats if s.get("type") == "candidate-pair"]
    selected = next(
        (
            p
            for p in pairs
            if p.get("selected") is True
            or (p.get("nominated") is True and p.get("state") == "succeeded")
        ),
        None,
    )
    seconds = _number((selected or {}).get("currentRoundTripTime"))
    return seconds * 1000 if seconds is not None else None


def parse_snapshots(snapshots: Iterable[dict[str, Any]], default_call_id: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous: dict[tuple[str, str, str], tuple[float, float]] = {}

    for snapshot in snapshots:
        stats = snapshot["stats"]
        timestamp, epoch_ms = _iso_timestamp(snapshot.get("timestamp"))
        call_id = str(snapshot.get("call_id") or default_call_id)
        by_id = {str(s.get("id")): s for s in stats if s.get("id") is not None}
        ice_type = _selected_candidate_type(stats)
        fallback_rtt = _candidate_rtt_ms(stats)

        remote_inbound_by_local = {
            str(s.get("localId")): s
            for s in stats
            if s.get("type") == "remote-inbound-rtp" and s.get("localId")
        }

        for stat in stats:
            stat_type = stat.get("type")
            if stat_type not in {"inbound-rtp", "outbound-rtp"}:
                continue
            if stat.get("isRemote") is True:
                continue

            direction = "inbound" if stat_type == "inbound-rtp" else "outbound"
            kind = stat.get("kind") or stat.get("mediaType") or "unknown"
            stream_id = str(stat.get("id") or stat.get("ssrc") or "unknown")
            remote = remote_inbound_by_local.get(stream_id) if direction == "outbound" else None

            packets = _number(stat.get("packetsReceived" if direction == "inbound" else "packetsSent"))
            lost = _number(stat.get("packetsLost"))
            if lost is None and remote:
                lost = _number(remote.get("packetsLost"))
            denominator = (packets or 0) + max(lost or 0, 0)
            loss_pct = 100 * max(lost or 0, 0) / denominator if denominator > 0 else None

            jitter = _number(stat.get("jitter"))
            if jitter is None and remote:
                jitter = _number(remote.get("jitter"))
            rtt = _number((remote or {}).get("roundTripTime"))
            rtt_ms = rtt * 1000 if rtt is not None else fallback_rtt

            byte_count = _number(stat.get("bytesReceived" if direction == "inbound" else "bytesSent"))
            bitrate = None
            key = (call_id, direction, stream_id)
            if byte_count is not None and epoch_ms is not None and key in previous:
                old_time, old_bytes = previous[key]
                elapsed_ms = epoch_ms - old_time
                delta_bytes = byte_count - old_bytes
                if elapsed_ms > 0 and delta_bytes >= 0:
                    bitrate = delta_bytes * 8 / elapsed_ms  # bits/ms == kbit/s
            if byte_count is not None and epoch_ms is not None:
                previous[key] = (epoch_ms, byte_count)

            codec = by_id.get(str(stat.get("codecId")))
            row = {
                "timestamp": timestamp,
                "call_id": call_id,
                "direction": direction,
                "media_kind": kind,
                "ssrc": stat.get("ssrc"),
                "packets": packets,
                "packets_lost": lost,
                "packet_loss_pct": loss_pct,
                "jitter_ms": jitter * 1000 if jitter is not None else None,
                "rtt_ms": rtt_ms,
                "bitrate_kbps": bitrate,
                "codec": _codec_name(codec),
                "ice_candidate_type": ice_type,
            }
            rows.append({k: round(v, 3) if isinstance(v, float) else v for k, v in row.items()})
    return rows


def write_output(rows: list[dict[str, Any]], output: Path | None, output_format: str) -> None:
    stream = output.open("w", encoding="utf-8", newline="") if output else sys.stdout
    try:
        if output_format == "json":
            json.dump(rows, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        else:
            writer = csv.DictWriter(stream, fieldnames=OUTPUT_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    finally:
        if output:
            stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse WebRTC getStats logs for Grafana")
    parser.add_argument("input", type=Path, help="Input .json or .jsonl file")
    parser.add_argument("-o", "--output", type=Path, help="Output file; stdout by default")
    parser.add_argument("-f", "--format", choices=("json", "csv"), default="json")
    parser.add_argument("--call-id", default="", help="Fallback call identifier")
    args = parser.parse_args()

    try:
        snapshots = load_snapshots(args.input)
        if not snapshots:
            raise ValueError("No RTCStats objects found in the input")
        rows = parse_snapshots(snapshots, args.call_id)
        if not rows:
            raise ValueError("No inbound-rtp or outbound-rtp records found")
        write_output(rows, args.output, args.format)
        return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
