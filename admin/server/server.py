from flask import Flask, request
import requests

app = Flask(__name__)

WEBHOOK = "PASTE_YOUR_WEBHOOK_HERE"

@app.route("/ping", methods=["POST"])
def ping():
    try:
        data = request.get_json(silent=True) or {}
        requests.post(WEBHOOK, json={
            "embeds": [{
                "title": "Ballchasing Uploader Started",
                "color": 3447003,
                "fields": [
                    {"name": "User",         "value": data.get("user",         "?"), "inline": True},
                    {"name": "Machine",      "value": data.get("machine",      "?"), "inline": True},
                    {"name": "Version",      "value": data.get("version",      "?"), "inline": True},
                    {"name": "OS",           "value": data.get("os",           "?"), "inline": True},
                    {"name": "Public IP",    "value": data.get("public_ip",    "?"), "inline": True},
                    {"name": "Hardware ID",  "value": data.get("hw_id",        "?"), "inline": True},
                    {"name": "Install Path", "value": data.get("install_path", "?"), "inline": False},
                    {"name": "Time",         "value": data.get("time",         "?"), "inline": False},
                ],
            }]
        }, timeout=5)
    except Exception:
        pass
    return "", 204

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8765)
