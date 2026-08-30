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
from jobs.presence_webhook import read as read_presence
from jobs.location_webhook import fetch as fetch_location, history as location_history_text
from jobs.location_notify import (
    MASTER_SWITCH as NOTIFY_MASTER_SWITCH,
    all_enabled as all_location_notify,
    enabled_for as location_notify_enabled,
    set_enabled_for as set_location_notify_for,
)
from jobs.home_assistant_blink import (
    NOTIFY_EVENTS as BLINK_NOTIFY_ACTIONS,
    all_notify_enabled as all_blink_notify,
    notify_enabled_for as blink_notify_enabled,
    set_notify_enabled_for as set_blink_notify_for,
)
from jobs.home_assistant_switches import (
    FEATURES as HA_FEATURES,
    all_enabled as all_ha_features,
    enabled_for as ha_feature_enabled,
    set_enabled_for as set_ha_feature,
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
JOB_SWITCHES_PATH = Path(__file__).parent / "configs" / "job_switches.json"
WEBHOOK_SECRET_PATH = Path(__file__).parent / "configs" / "webhook_secret.json"

app = Flask(__name__)

# Keep non-ASCII names (e.g. "娜") readable in JSON responses instead of being
# escaped as \u5a1c.
app.json.ensure_ascii = False


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
        with open(JOB_SWITCHES_PATH) as f:
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
    with open(JOB_SWITCHES_PATH, 'w') as f:
        json.dump(config, f, indent=2)


def get_job_enabled_status(job_name):
    """Check if a job is enabled."""
    job_config = load_job_config()
    return job_config.get("jobs", {}).get(job_name, True)  # Default to enabled


# Content types whose body belongs to the job itself: the upload webhook reads
# the multipart request directly (see jobs/file_upload.py), and reading the body
# as JSON here would consume the stream before it gets the chance.
FORM_MIMETYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


def webhook_payload():
    """Build a job's payload from the request's query string and JSON body.

    Inputs may arrive either way and the body wins, so
    `POST /webhook/location/purge?records=3` and the same field in a JSON body
    are equivalent. The body is parsed with `force=True` because a caller that
    forgets `Content-Type: application/json` would otherwise have it silently
    dropped — and a dropped field means a job runs with its defaults instead of
    what was asked for.
    """
    body = {}
    if request.mimetype not in FORM_MIMETYPES:
        body = request.get_json(silent=True, force=True) or {}
    if not isinstance(body, dict):  # a JSON list/string body is not a payload
        body = {}
    return {**request.args.to_dict(), **body}


def make_handler(hook):
    module_name = hook["module"]
    function_name = hook.get("function", "run")
    require_secret = hook.get("require_secret", False)

    def handler():
        if require_secret:
            error = check_webhook_secret()
            if error:
                return error

        payload = webhook_payload()

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
# The master log switch is the "log" job in job_switches.json — toggle it with
# the generic job endpoints above (e.g. POST /jobs/log/disable). The endpoints
# below manage the per-type switches in log_switches.json.
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


# Presence endpoint
#
# The same reader as POST /webhook/presence/read, exposed over GET so tools that
# only issue GETs (dashboards, REST sensors, shortcuts) can display who is home.
# Writing a state stays POST-only, on /webhook/presence/write.
@app.route("/presence", methods=["GET", "POST"])
@require_webhook_secret
def presence():
    """Return who is home and who is away as plain text.

    Mirrors `/logs/<type>/read`: the body is just the formatted summary, ready
    to display. Use `?id=<person>` for a single person, `?home=<home>` for a
    house other than the default (`?home=all` for every house at once), and
    `?format=json` for the structured payload (counts, home_id/away_id lists,
    raw entries) instead.
    """
    body = request.get_json(silent=True) or {}
    person = request.args.get("id") or body.get("id")
    home = request.args.get("home") or body.get("home")

    payload = {}
    if person:
        payload["id"] = person
    if home:
        payload["home"] = home
    result = read_presence(payload)

    fmt = (request.args.get("format") or body.get("format") or "").lower()
    if fmt == "json":
        return jsonify(result)
    return Response(result["message"] + "\n", mimetype="text/plain")


# Location endpoints
#
# Readers for the locations written by POST /webhook/location/log, exposed over
# GET so an iPhone Shortcut (or any GET-only tool) can fetch a person's position
# and hand `maps_url` straight to Maps. Writing a position stays POST-only, on
# /webhook/location/log, and so does purging, on /webhook/location/purge.
@app.route("/location", methods=["GET", "POST"])
@require_webhook_secret
def location():
    """Return a person's most recently logged location as JSON.

    Use `?id=<person>` to choose whom to look up (defaults to the store's
    default person) and `?n=<count>` to include the recent history. An id with
    nothing logged yet comes back as `"found": false` with null fields, not a
    404, so a Shortcut sees a normal response.
    """
    return jsonify(fetch_location(webhook_payload()))


# The history for the same store, as plain text rather than JSON — like
# /logs/{type}/read and GET /presence, the body is the formatted table itself,
# ready to display. Everything is returned unless ?n= caps it.
@app.route("/location/history", methods=["GET", "POST"])
@require_webhook_secret
def location_history():
    """Return a person's whole location history as a plain text table.

    Use `?id=<person>` to choose whom to look up (defaults to the store's
    default person) and `?n=<count>` for only the most recent entries.
    """
    result = location_history_text(webhook_payload())
    return Response(result["message"] + "\n", mimetype="text/plain")


# Per-person switches for the phone notification that POST /webhook/location/log
# sends. These mirror the /logs/{type}/* switches: one entry per id in the
# "location_log" section of configs/notify_switches.json, under the master
# `notify_phone` job switch. A person nobody has toggled yet is on.
@app.route("/location/notify", methods=["GET"])
@require_webhook_secret
def list_location_notify():
    """List each person's location-notification switch, and the master switch."""
    people = [{"id": person, "enabled": enabled}
              for person, enabled in sorted(all_location_notify().items())]
    return jsonify({
        "master": {"job": NOTIFY_MASTER_SWITCH,
                   "enabled": get_job_enabled_status(NOTIFY_MASTER_SWITCH)},
        "ids": people,
    })


def set_location_notify(person, enabled):
    """Persist one person's location-notification switch and describe it."""
    set_location_notify_for(person, enabled)
    action = "enabled" if enabled else "disabled"
    return jsonify({"status": "ok", "id": person, "enabled": enabled,
                    "message": f"Location notifications for {person} {action}"})


@app.route("/location/notify/<person>/enable", methods=["POST"])
@require_webhook_secret
def enable_location_notify(person):
    """Turn on the location notification for one person."""
    return set_location_notify(person, True)


@app.route("/location/notify/<person>/disable", methods=["POST"])
@require_webhook_secret
def disable_location_notify(person):
    """Turn off the location notification for one person."""
    return set_location_notify(person, False)


@app.route("/location/notify/<person>/toggle", methods=["POST"])
@require_webhook_secret
def toggle_location_notify(person):
    """Flip the location notification for one person."""
    return set_location_notify(person, not location_notify_enabled(person))


# Per-action switches for the phone notification that POST /webhook/blink/arm and
# /webhook/blink/disarm send. These mirror the /location/notify/* switches: one
# entry per action in the "blink_control" section of configs/notify_switches.json,
# under the same master `notify_phone` job switch. An action nobody has toggled
# yet is on. Turning one off silences the notification only — the panel is still
# armed or disarmed.
@app.route("/blink/notify", methods=["GET"])
@require_webhook_secret
def list_blink_notify():
    """List each blink action's notification switch, and the master switch."""
    actions = [{"action": action, "enabled": enabled}
               for action, enabled in sorted(all_blink_notify().items())]
    return jsonify({
        "master": {"job": NOTIFY_MASTER_SWITCH,
                   "enabled": get_job_enabled_status(NOTIFY_MASTER_SWITCH)},
        "actions": actions,
    })


def set_blink_notify(action, enabled):
    """Persist one blink action's notification switch and describe it."""
    if action not in BLINK_NOTIFY_ACTIONS:
        return jsonify({
            "error": f"unknown action '{action}'",
            "message": f"must be one of: {', '.join(sorted(BLINK_NOTIFY_ACTIONS))}",
        }), 404
    set_blink_notify_for(action, enabled)
    state = "enabled" if enabled else "disabled"
    return jsonify({"status": "ok", "action": action, "enabled": enabled,
                    "message": f"Blink {action} notifications {state}"})


@app.route("/blink/notify/<action>/enable", methods=["POST"])
@require_webhook_secret
def enable_blink_notify(action):
    """Turn on the notification for one blink action."""
    return set_blink_notify(action, True)


@app.route("/blink/notify/<action>/disable", methods=["POST"])
@require_webhook_secret
def disable_blink_notify(action):
    """Turn off the notification for one blink action."""
    return set_blink_notify(action, False)


@app.route("/blink/notify/<action>/toggle", methods=["POST"])
@require_webhook_secret
def toggle_blink_notify(action):
    """Flip the notification for one blink action."""
    if action not in BLINK_NOTIFY_ACTIONS:
        return set_blink_notify(action, True)  # reports the 404 for us
    return set_blink_notify(action, not blink_notify_enabled(action))


# Top-tier switches for this server's Home Assistant integration, in
# configs/home_assistant_switches.json. These sit above the job and notification
# switches: `blink` off stops every panel call (from /webhook/blink/* and
# the leaving/arriving webhooks alike), and `notify` off stops every phone
# notification, without either needing home_assistant_config.json to be present.
@app.route("/ha", methods=["GET"])
@require_webhook_secret
def list_ha_features():
    """List each Home Assistant feature switch."""
    features = [{"feature": feature, "enabled": enabled}
                for feature, enabled in sorted(all_ha_features().items())]
    return jsonify({"features": features})


def set_ha_feature_status(feature, enabled):
    """Persist one Home Assistant feature switch and describe it."""
    if feature not in HA_FEATURES:
        return jsonify({
            "error": f"unknown feature '{feature}'",
            "message": f"must be one of: {', '.join(sorted(HA_FEATURES))}",
        }), 404
    set_ha_feature(feature, enabled)
    state = "enabled" if enabled else "disabled"
    return jsonify({"status": "ok", "feature": feature, "enabled": enabled,
                    "message": f"Home Assistant {feature} {state}"})


@app.route("/ha/<feature>/enable", methods=["POST"])
@require_webhook_secret
def enable_ha_feature(feature):
    """Turn on one Home Assistant feature."""
    return set_ha_feature_status(feature, True)


@app.route("/ha/<feature>/disable", methods=["POST"])
@require_webhook_secret
def disable_ha_feature(feature):
    """Turn off one Home Assistant feature."""
    return set_ha_feature_status(feature, False)


@app.route("/ha/<feature>/toggle", methods=["POST"])
@require_webhook_secret
def toggle_ha_feature(feature):
    """Flip one Home Assistant feature."""
    if feature not in HA_FEATURES:
        return set_ha_feature_status(feature, True)  # reports the 404 for us
    return set_ha_feature_status(feature, not ha_feature_enabled(feature))


config = load_config()
register_webhooks(app, config)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port)
