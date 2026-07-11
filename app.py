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
import argparse
from pathlib import Path
import datetime

from flask import Flask, jsonify, request

# Set up argument parsing for debug mode
parser = argparse.ArgumentParser(description='Start Blink Server')
parser.add_argument('--debug', action='store_true', help='Enable debug mode with verbose logging')

args = parser.parse_args()

# Configure logging based on debug mode
if args.debug:
    logging.basicConfig(level=logging.DEBUG)
    print("Debug mode enabled")
else:
    # In production, we want minimal logging
    logging.basicConfig(level=logging.WARNING)

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.json"
JOB_CONFIG_PATH = Path(__file__).parent / "job_config.json"

app = Flask(__name__)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_job_config():
    """Load job configuration including enabled/disabled status."""
    try:
        with open(JOB_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        # Create default job config if it doesn't exist
        default_config = {
            "jobs": {},
            "last_modified": None
        }
        save_job_config(default_config)
        return default_config


def save_job_config(config):
    """Save job configuration to file."""
    with open(JOB_CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)


def get_job_enabled_status(job_name):
    """Check if a job is enabled."""
    job_config = load_job_config()
    return job_config.get("jobs", {}).get(job_name, True)  # Default to enabled


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

        # Check if job is enabled
        job_name = module_name.split('.')[-1]  # Get the job name from module path
        if not get_job_enabled_status(job_name):
            logger.info("Job %s is disabled, ignoring webhook request", job_name)
            return jsonify({
                "status": "disabled",
                "message": f"Job {job_name} is currently disabled"
            }), 403

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

    # Generate a unique handler name based on path, module and function to avoid Flask conflicts
    handler_name = f"handler_{module_name}_{function_name}".replace(".", "_")
    # If there are multiple hooks with same module/function but different paths, make names unique
    if 'path' in hook:
        path_hash = hash(hook['path']) % 10000  # Simple hash to create unique suffix
        handler_name = f"{handler_name}_{path_hash}"

    handler.__name__ = handler_name
    return handler


def register_webhooks(app, config):
    for hook in config.get("webhooks", []):
        path = hook["path"]
        app.add_url_rule(path, view_func=make_handler(hook), methods=["POST"])
        logger.info("registered webhook: POST %s -> %s.%s", path, hook["module"], hook.get("function", "run"))


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


# Job management endpoints
@app.route("/jobs")
def list_jobs():
    """List all available jobs and their status."""
    config = load_config()

    jobs = []
    for hook in config.get("webhooks", []):
        module_name = hook["module"]
        job_name = module_name.split('.')[-1]
        enabled = get_job_enabled_status(job_name)
        jobs.append({
            "name": job_name,
            "path": hook["path"],
            "enabled": enabled
        })

    return jsonify({"jobs": jobs})


def set_job_status(job_name, enabled):
    """Persist a job's enabled/disabled status and log the change."""
    job_config = load_job_config()
    jobs = job_config.setdefault("jobs", {})
    jobs[job_name] = enabled
    job_config["last_modified"] = str(datetime.datetime.now())
    save_job_config(job_config)
    logger.info("Job %s %s", job_name, "enabled" if enabled else "disabled")


@app.route("/jobs/<job_name>/enable", methods=["POST"])
def enable_job(job_name):
    """Enable a specific job."""
    set_job_status(job_name, True)
    return jsonify({"status": "ok", "message": f"Job {job_name} enabled"})


@app.route("/jobs/<job_name>/disable", methods=["POST"])
def disable_job(job_name):
    """Disable a specific job."""
    set_job_status(job_name, False)
    return jsonify({"status": "ok", "message": f"Job {job_name} disabled"})


@app.route("/jobs/<job_name>/toggle", methods=["POST"])
def toggle_job(job_name):
    """Toggle the status of a specific job."""
    new_status = not get_job_enabled_status(job_name)
    set_job_status(job_name, new_status)
    action = "enabled" if new_status else "disabled"
    return jsonify({"status": "ok", "message": f"Job {job_name} {action}"})


config = load_config()
register_webhooks(app, config)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
