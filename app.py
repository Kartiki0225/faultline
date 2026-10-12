"""FaultLine: a live J1939 engine diagnostics dashboard.

Run:  python app.py      then open http://127.0.0.1:5050

Three parts run together:
  ECU      simulates an engine and broadcasts J1939 frames on a CAN bus
  Gateway  listens to the bus and decodes the frames
  Flask    serves the dashboard and streams live data over a WebSocket
"""

import json

import can
from flask import Flask, Response, jsonify, render_template, request
from flask_sock import Sock

import gateway as tool
import j1939
from ecu import Ecu
from engine import FAULTS

# A virtual CAN bus lives inside this program. To read a real engine, change
# these two lines to a hardware interface that python-can supports,
# for example: can.Bus(interface="pcan", channel="PCAN_USBBUS1", bitrate=250000)
ecu = Ecu(can.Bus(interface="virtual", channel="faultline"))
gateway = tool.Gateway(can.Bus(interface="virtual", channel="faultline"))

app = Flask(__name__)
sock = Sock(app)


@app.get("/")
def dashboard():
    return render_template("index.html", faults=FAULTS)


@sock.route("/ws")
def stream(ws):
    """Send a snapshot ten times a second, and act on commands from the browser."""
    last_frame = 0
    while True:
        command = ws.receive(timeout=0.1)  # waits up to 100 ms, which also sets the pace
        if command is not None:
            run_command(json.loads(command))

        snapshot = gateway.snapshot(after_frame=last_frame)
        last_frame = snapshot["frame_number"]
        snapshot["simulator"] = ecu.status()
        ws.send(json.dumps(snapshot))


def run_command(command: dict) -> None:
    print("command:", command, flush=True)
    name = command.get("cmd")
    if name == "start":
        ecu.start_engine()
    elif name == "stop":
        ecu.stop_engine()
    elif name == "throttle":
        ecu.set_throttle(float(command.get("value", 0)))
    elif name == "fault":
        ecu.set_fault(command.get("name", ""), bool(command.get("on")))
    elif name == "repair":
        ecu.repair()


# ---------------------------------------------------------------- REST API

@app.get("/api/snapshot")
def api_snapshot():
    return jsonify({**gateway.snapshot(), "simulator": ecu.status()})


@app.get("/api/explain")
def api_explain():
    """Explain one frame, for example /api/explain?id=0CF00400&data=F1A5A0E02EFFFFFF"""
    try:
        can_id = int(request.args["id"], 16)
        data = bytes.fromhex(request.args["data"])
    except (KeyError, ValueError):
        return jsonify({"error": "Give id and data as hex."}), 400
    if can_id >= 1 << 29 or len(data) != 8:
        return jsonify({"error": "A J1939 frame has a 29-bit id and 8 data bytes."}), 400
    return jsonify(tool.explain(can_id, data))


@app.get("/api/messages")
def api_messages():
    """The signal table: which values live in which message, and how they are scaled."""
    return jsonify([
        {
            "pgn": message.pgn,
            "name": message.name,
            "title": message.title,
            "period_ms": round(message.period * 1000),
            "signals": [
                {"spn": s.spn, "name": s.name, "start_byte": s.start_byte, "bit_length": s.bit_length,
                 "scale": s.scale, "offset": s.offset, "unit": s.unit}
                for s in message.signals
            ],
        }
        for message in j1939.MESSAGES.values()
    ])


@app.get("/api/log")
def api_log():
    """Download the recorded frames as a candump log."""
    return Response(
        gateway.log_text(),
        mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=faultline.log"},
    )


if __name__ == "__main__":
    ecu.start()
    gateway.start()
    # No reloader: it would start a second copy of the engine.
    app.run(port=5050, threaded=True, use_reloader=False)
