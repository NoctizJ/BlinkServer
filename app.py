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
from functools import wraps
from pathlib import Path
import datetime

from flask import Flask, jsonify, request, Response

from jobs.log_engine import (
    load_log_config,
    get_type_enabled_status,
    set_type_status,
    read_log,
    MASTER_SWITCH,
)

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

CONFIG_PATH = Path(__file__).parent / "configs" / "config.json"
JOB_CONFIG_PATH = Path(__file__).parent / "configs" / "job_config.json"
WEBHOOK_SECRET_PATH = Path(__file__).parent / "configs" / "webhook_secret.json"

app = Flask(__name__)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_webhook_secret():
    """Load the single shared webhook secret from webhook_secret.json.

    The file is gitignored (see webhook_secret.example.json for the format).
    Returns the secret string, or None if the file is missing/unreadable.
    """
    try:
        with open(WEBHOOK_SECRET_PATH) as f:
            return json.load(f).get("WEBHOOK_SECRET")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def check_webhook_secret():
    """Validate the X-Webhook-Secret header against the shared secret.

    Returns an error (response, status) tuple to short-circuit with, or None
    when the request is authorized.
    """
    expected = load_webhook_secret()
    if not expected:
        logger.error(
            "%s requires a secret but none is configured "
            "(see webhook_secret.example.json)", request.path
        )
        return jsonify({"error": "server misconfigured: webhook secret not set"}), 500
    if request.headers.get("X-Webhook-Secret") != expected:
        return jsonify({"error": "unauthorized"}), 401
    return None


def require_webhook_secret(view):
    """Decorator that requires the shared secret on a management route."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        error = check_webhook_secret()
        if error:
            return error
        return view(*args, **kwargs)
    return wrapper


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
    require_secret = hook.get("require_secret", False)

    def handler():
        if require_secret:
            error = check_webhook_secret()
            if error:
                return error

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


def known_job_names():
    """Return the set of valid job names.

    A job is valid if it is backed by a webhook in config.json, or if it is a
    special switch that has no webhook (e.g. the ``log`` master switch). This
    prevents the management endpoints from creating phantom job entries for
    unknown/typo/stale names (e.g. a leftover ``arm`` caller).
    """
    names = {hook["module"].split(".")[-1]
             for hook in load_config().get("webhooks", [])}
    names.add(MASTER_SWITCH)  # "log" — the logging master switch
    return names


def set_job_status(job_name, enabled):
    """Persist a job's enabled/disabled status and log the change."""
    job_config = load_job_config()
    jobs = job_config.setdefault("jobs", {})
    jobs[job_name] = enabled
    job_config["last_modified"] = str(datetime.datetime.now())
    save_job_config(job_config)
    logger.info("Job %s %s", job_name, "enabled" if enabled else "disabled")


def unknown_job_response(job_name):
    """Return a 404 response if job_name is not a known job, else None."""
    if job_name not in known_job_names():
        return jsonify({"error": "unknown job", "message": f"No such job: {job_name}"}), 404
    return None


@app.route("/jobs/<job_name>/enable", methods=["POST"])
@require_webhook_secret
def enable_job(job_name):
    """Enable a specific job."""
    unknown = unknown_job_response(job_name)
    if unknown:
        return unknown
    set_job_status(job_name, True)
    return jsonify({"status": "ok", "message": f"Job {job_name} enabled"})


@app.route("/jobs/<job_name>/disable", methods=["POST"])
@require_webhook_secret
def disable_job(job_name):
    """Disable a specific job."""
    unknown = unknown_job_response(job_name)
    if unknown:
        return unknown
    set_job_status(job_name, False)
    return jsonify({"status": "ok", "message": f"Job {job_name} disabled"})


@app.route("/jobs/<job_name>/toggle", methods=["POST"])
@require_webhook_secret
def toggle_job(job_name):
    """Toggle the status of a specific job."""
    unknown = unknown_job_response(job_name)
    if unknown:
        return unknown
    new_status = not get_job_enabled_status(job_name)
    set_job_status(job_name, new_status)
    action = "enabled" if new_status else "disabled"
    return jsonify({"status": "ok", "message": f"Job {job_name} {action}"})


# Log management endpoints
#
# The master log switch is the "log" job in job_config.json — toggle it with
# the generic job endpoints above (e.g. POST /jobs/log/disable). The endpoints
# below manage the per-type switches in log_config.json.
@app.route("/logs")
def list_log_types():
    """List all configured log types and their on/off status."""
    log_config = load_log_config()
    types = [
        {"type": name, "enabled": enabled}
        for name, enabled in log_config.get("types", {}).items()
    ]
    return jsonify({"log_types": types})


def set_log_type_status(log_type, enabled):
    """Persist a log type's enabled/disabled status and log the change."""
    normalized = set_type_status(log_type, enabled)
    logger.info("Log type %s %s", normalized, "enabled" if enabled else "disabled")
    return normalized


@app.route("/logs/<log_type>/enable", methods=["POST"])
@require_webhook_secret
def enable_log_type(log_type):
    """Enable a specific log type."""
    name = set_log_type_status(log_type, True)
    return jsonify({"status": "ok", "message": f"Log type {name} enabled"})


@app.route("/logs/<log_type>/disable", methods=["POST"])
@require_webhook_secret
def disable_log_type(log_type):
    """Disable a specific log type."""
    name = set_log_type_status(log_type, False)
    return jsonify({"status": "ok", "message": f"Log type {name} disabled"})


@app.route("/logs/<log_type>/toggle", methods=["POST"])
@require_webhook_secret
def toggle_log_type(log_type):
    """Toggle the status of a specific log type."""
    new_status = not get_type_enabled_status(log_type)
    name = set_log_type_status(log_type, new_status)
    action = "enabled" if new_status else "disabled"
    return jsonify({"status": "ok", "message": f"Log type {name} {action}"})


@app.route("/logs/<log_type>/read", methods=["GET", "POST"])
@require_webhook_secret
def read_log_type(log_type):
    """Return recent log entries for a type as plain text (blink -> blink.log,
    else default.log). Use ?n=<count> for the number of most recent entries
    (default 20; n<=0 returns the whole file)."""
    n = request.args.get("n", default=20, type=int)
    text = read_log(log_type, entries=n)
    if not text:
        text = f"(no log entries for '{log_type}')\n"
    return Response(text, mimetype="text/plain")


config = load_config()
register_webhooks(app, config)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
