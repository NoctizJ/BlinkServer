"""Generic webhook server.

This file is job-agnostic: it reads config.json, and for every entry
registers a Flask route that dynamically imports the given module and
calls its run(payload) function. To add a new automation, write a new
module under jobs/ with a run(payload) function and add an entry to
config.json — no changes needed here.
"""

import importlib
import json
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"

app = Flask(__name__)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def make_handler(hook):
    module_name = hook["module"]
    function_name = hook.get("function", "run")
    secret = hook.get("secret")

    def handler():
        if secret:
            provided = request.headers.get("X-Webhook-Secret")
            if provided != secret:
                return jsonify({"error": "unauthorized"}), 401

        payload = request.get_json(silent=True) or {}

        try:
            module = importlib.import_module(module_name)
            func = getattr(module, function_name)
        except (ImportError, AttributeError) as exc:
            logger.exception("failed to load %s.%s", module_name, function_name)
            return jsonify({"error": f"job not available: {exc}"}), 500

        try:
            result = func(payload)
        except Exception as exc:
            logger.exception("job %s.%s raised an error", module_name, function_name)
            return jsonify({"error": str(exc)}), 500

        return jsonify({"status": "ok", "result": result})

    handler.__name__ = f"handler_{module_name}_{function_name}".replace(".", "_")
    return handler


def register_webhooks(app, config):
    for hook in config.get("webhooks", []):
        path = hook["path"]
        app.add_url_rule(path, view_func=make_handler(hook), methods=["POST"])
        logger.info("registered webhook: POST %s -> %s.%s", path, hook["module"], hook.get("function", "run"))


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


config = load_config()
register_webhooks(app, config)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
