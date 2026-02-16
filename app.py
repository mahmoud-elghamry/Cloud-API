"""
Smart Water Station - Cloud Backend API V2.0
Deploy to Railway or Render for remote access.

Architecture:
  ESP32 (WiFi HTTP POST) ──┐
                           ├──→ This Cloud API ←── Frontend reads
  AI Module (Serial→POST) ─┘

Includes built-in analytics engine (merged from analytics.py).
"""

import os
import threading
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

# ============================================================
# App Setup
# ============================================================

app = Flask(__name__)
CORS(app)

MAX_HISTORY = int(os.environ.get("MAX_HISTORY", 500))
VERSION = "2.0.0"
START_TIME = datetime.utcnow().isoformat()


# ============================================================
# Built-in Analytics Engine (from analytics.py)
# ============================================================

# Water quality thresholds (TDS in ppm)
WQ_THRESHOLDS = {
    "excellent": 150,
    "good": 300,
    "fair": 450,
    "poor": 500,
}

# Safety limits (matching firmware Config.h)
MAX_PRESSURE = 100.0   # PSI
MIN_PRESSURE = 5.0
MAX_TDS = 500.0
MIN_WATER_LEVEL = 10.0
MAX_FLOW_RATE = 50.0


def _calc_water_quality(tds: float) -> dict:
    """Calculate water quality score (0-100) and grade from TDS value."""
    if tds <= WQ_THRESHOLDS["excellent"]:
        score = 100 - int((tds / WQ_THRESHOLDS["excellent"]) * 10)
        grade = "Excellent"
    elif tds <= WQ_THRESHOLDS["good"]:
        score = 80 - int(((tds - WQ_THRESHOLDS["excellent"]) /
                         (WQ_THRESHOLDS["good"] - WQ_THRESHOLDS["excellent"])) * 20)
        grade = "Good"
    elif tds <= WQ_THRESHOLDS["fair"]:
        score = 60 - int(((tds - WQ_THRESHOLDS["good"]) /
                         (WQ_THRESHOLDS["fair"] - WQ_THRESHOLDS["good"])) * 20)
        grade = "Fair"
    else:
        score = max(0, 40 - int(((tds - WQ_THRESHOLDS["fair"]) / 100) * 20))
        grade = "Poor"

    return {"score": max(0, min(100, score)), "grade": grade}


def _analyze_telemetry(telemetry: dict) -> dict:
    """
    Run full analytics on raw telemetry data.
    Accepts either flat format or nested sensor format.

    Flat:    {"tds": 250, "pressure": 45, "flow": 20, "level": 75}
    Nested:  {"tds": {"value": 250, "status": "OK"}, ...}
    """
    anomalies = []
    recommendations = []

    # Extract values (support both flat and nested formats)
    def _val(key, default=0):
        v = telemetry.get(key, default)
        if isinstance(v, dict):
            return v.get("value", default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    tds = _val("tds", 0)
    pressure = _val("pressure", 0)
    flow = _val("flow", 0)
    level = _val("level", 100)

    # Water Quality
    wq = _calc_water_quality(tds)

    # Pressure
    if pressure >= MAX_PRESSURE * 0.9:
        pressure_status = "critical"
        anomalies.append("PRESSURE_CRITICAL")
        recommendations.append("Reduce pump speed or check for blockage")
    elif pressure >= MAX_PRESSURE * 0.75:
        pressure_status = "warning"
        anomalies.append("PRESSURE_HIGH")
        recommendations.append("Monitor pressure - approaching limit")
    elif pressure <= MIN_PRESSURE and pressure > 0:
        pressure_status = "warning"
        anomalies.append("PRESSURE_LOW")
        recommendations.append("Check water supply or pump")
    else:
        pressure_status = "normal"

    # Flow
    if flow >= MAX_FLOW_RATE * 0.9:
        flow_status = "high"
        anomalies.append("FLOW_HIGH")
        recommendations.append("Check for leaks or reduce pump speed")
    elif 0 < flow < 5:
        flow_status = "low"
        anomalies.append("FLOW_LOW")
        recommendations.append("Check for clogged filters")
    else:
        flow_status = "normal"

    # Water Level
    if level <= MIN_WATER_LEVEL:
        anomalies.append("WATER_LEVEL_CRITICAL")
        recommendations.append("Refill water tank immediately")
    elif level <= MIN_WATER_LEVEL * 2:
        anomalies.append("WATER_LEVEL_LOW")
        recommendations.append("Water level low - consider refilling soon")

    # TDS
    if tds >= MAX_TDS:
        anomalies.append("TDS_HIGH")
        recommendations.append("Water quality poor - check filtration system")
    elif tds >= MAX_TDS * 0.8:
        recommendations.append("TDS approaching limit - monitor filtration")

    return {
        "water_quality": wq,
        "pressure_status": pressure_status,
        "flow_status": flow_status,
        "anomalies": anomalies,
        "recommendations": recommendations,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============================================================
# In-Memory Data Store (thread-safe)
# ============================================================

_lock = threading.Lock()

store = {
    "telemetry": {},
    "state": {"state": "UNKNOWN", "error": "NONE"},
    "analytics": {},
    "history": [],
    "commands_pending": [],
    "security": {},
    "connected": False,
}


def _add_history(telemetry: dict, analytics: dict):
    """Append telemetry + analytics snapshot to history."""
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "data": telemetry,
        "analytics": analytics,
    }
    store["history"].append(entry)
    if len(store["history"]) > MAX_HISTORY:
        store["history"] = store["history"][-MAX_HISTORY:]


# ============================================================
# Health & Info
# ============================================================

@app.route("/")
def index():
    return jsonify({
        "name": "Smart Water Station Cloud API",
        "version": VERSION,
        "docs": {
            "GET  /api/health": "Health check",
            "GET  /api/status": "Full system status",
            "GET  /api/telemetry": "Latest sensor readings + analytics",
            "POST /api/telemetry": "Push raw sensor data (auto-analyzed)",
            "GET  /api/analytics": "Latest AI analytics",
            "POST /api/analytics": "Push analytics (override)",
            "GET  /api/history": "Telemetry history (?limit=N)",
            "POST /api/command": "Send command to ESP32 (from frontend)",
            "GET  /api/commands/pending": "Poll pending commands (from AI module)",
            "POST /api/state": "Push system state",
            "GET  /api/security": "Get security status",
            "POST /api/security": "Push security status",
        }
    })


@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "version": VERSION,
        "started_at": START_TIME,
    })


# ============================================================
# Telemetry — GET / POST  (POST auto-runs analytics!)
# ============================================================

@app.route("/api/telemetry", methods=["GET"])
def get_telemetry():
    with _lock:
        return jsonify(store["telemetry"])


@app.route("/api/telemetry", methods=["POST"])
def post_telemetry():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400

    # Run analytics on incoming data
    analytics = _analyze_telemetry(data)

    with _lock:
        store["telemetry"] = data
        store["analytics"] = analytics
        store["connected"] = True
        _add_history(data, analytics)

    return jsonify({
        "success": True,
        "analytics": analytics,
    })


# ============================================================
# System State
# ============================================================

@app.route("/api/state", methods=["POST"])
def post_state():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    with _lock:
        store["state"] = data
    return jsonify({"success": True})


# ============================================================
# Analytics — GET / POST (POST is manual override)
# ============================================================

@app.route("/api/analytics", methods=["GET"])
def get_analytics():
    with _lock:
        return jsonify(store["analytics"])


@app.route("/api/analytics", methods=["POST"])
def post_analytics():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    with _lock:
        store["analytics"] = data
    return jsonify({"success": True})


# ============================================================
# History
# ============================================================

@app.route("/api/history")
def get_history():
    limit = request.args.get("limit", type=int)
    with _lock:
        if limit:
            return jsonify(store["history"][-limit:])
        return jsonify(store["history"])


# ============================================================
# Full Status
# ============================================================

@app.route("/api/status")
def get_status():
    with _lock:
        return jsonify({
            "connected": store["connected"],
            "state": store["state"],
            "telemetry": store["telemetry"],
            "security": store["security"],
            "analytics": store["analytics"],
        })


# ============================================================
# Commands
# ============================================================

@app.route("/api/command", methods=["POST"])
def post_command():
    data = request.get_json(silent=True)
    if not data or "cmd" not in data:
        return jsonify({"error": "Missing 'cmd' field"}), 400
    data["timestamp"] = datetime.utcnow().isoformat()
    with _lock:
        store["commands_pending"].append(data)
    return jsonify({"success": True, "queued": True})


@app.route("/api/commands/pending", methods=["GET"])
def get_pending_commands():
    with _lock:
        commands = store["commands_pending"].copy()
        store["commands_pending"].clear()
    return jsonify(commands)


# ============================================================
# Security
# ============================================================

@app.route("/api/security", methods=["GET"])
def get_security():
    with _lock:
        return jsonify(store["security"])


@app.route("/api/security", methods=["POST"])
def post_security():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Invalid JSON"}), 400
    with _lock:
        store["security"] = data
    return jsonify({"success": True})


# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    print(f"🚀 Smart Water Station Cloud API v{VERSION}")
    print(f"   Running on http://0.0.0.0:{port}")
    print(f"   Built-in analytics: ENABLED")
    print(f"   Max history: {MAX_HISTORY}")

    app.run(host="0.0.0.0", port=port, debug=debug)
