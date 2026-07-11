#!/usr/bin/env python3
"""
Home Assistant Arm/Disarm Job for Blink Server.

This module handles arm/disarm requests for Home Assistant alarm panels.
"""

import os
import json
import logging
from typing import Dict, Any

import requests

try:
    # Logging engine — records arm/disarm events under the "blink" log type.
    from jobs.log_engine import log as write_log
except ImportError:  # pragma: no cover - allows running this file directly
    from log_engine import log as write_log

logger = logging.getLogger(__name__)

def load_config() -> Dict[str, str]:
    """
    Load configuration from a file instead of environment variables.

    Returns:
        dict: Configuration dictionary with HA_BASE_URL, HA_API_KEY, and HA_ENTITY_ID
    """
    config_file = os.path.join(os.path.dirname(__file__), "..", "home_assistant_config.json")

    try:
        with open(config_file, 'r') as f:
            config = json.load(f)

        # Validate required fields
        required_fields = ["HA_BASE_URL", "HA_API_KEY", "HA_ENTITY_ID"]
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Missing required configuration field: {field}")

        return config

    except FileNotFoundError:
        raise ValueError(f"Configuration file not found: {config_file}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in configuration file: {e}")
    except Exception as e:
        raise ValueError(f"Error loading configuration: {e}")

def run(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute arm or disarm action on Home Assistant alarm panel.

    Args:
        payload (dict): The request payload containing the action

    Returns:
        dict: Response with success/error message
    """
    # Debug logging for incoming payload
    logger.debug("Received payload: %s", payload)

    try:
        # Validate payload
        if not isinstance(payload, dict):
            error_msg = "Invalid payload format - Payload must be a JSON object"
            logger.error(error_msg)
            return {
                "error": "Invalid payload format",
                "message": error_msg
            }

        # Check for required 'action' field
        if "action" not in payload:
            error_msg = "Missing action field in payload"
            logger.error(error_msg)
            return {
                "error": "Missing action",
                "message": error_msg
            }

        action = payload["action"]

        # Validate action value
        if action not in ["arm", "disarm"]:
            error_msg = f"Invalid action '{action}' - must be either 'arm' or 'disarm'"
            logger.error(error_msg)
            return {
                "error": "Invalid action",
                "message": error_msg
            }

        # Load configuration from file
        config = load_config()
        ha_base_url = config["HA_BASE_URL"]
        ha_api_key = config["HA_API_KEY"]
        ha_entity_id = config["HA_ENTITY_ID"]

        # Debug logging for configuration
        logger.debug("Configuration loaded - Base URL: %s, Entity ID: %s", ha_base_url, ha_entity_id)

        # Construct the correct API endpoint for Home Assistant
        if action == "arm":
            api_endpoint = f"{ha_base_url}/api/services/alarm_control_panel/alarm_arm_away"
        elif action == "disarm":
            api_endpoint = f"{ha_base_url}/api/services/alarm_control_panel/alarm_disarm"

        # Debug logging for API endpoint
        logger.debug("API endpoint for %s: %s", action, api_endpoint)

        # Prepare headers
        headers = {
            "Authorization": f"Bearer {ha_api_key}",
            "Content-Type": "application/json"
        }

        # Prepare data payload for Home Assistant
        data = {
            "entity_id": ha_entity_id
        }

        # Debug logging for request data
        logger.debug("Request headers: %s", headers)
        logger.debug("Request data: %s", data)

        # Make the API call to Home Assistant
        logger.debug("Making API request to %s", api_endpoint)

        response = requests.post(
            api_endpoint,
            headers=headers,
            json=data,
            timeout=30
        )

        # Debug logging for response
        logger.debug("API Response Status: %d", response.status_code)
        logger.debug("API Response Text: %s", response.text)

        # Check if the request was successful
        if response.status_code == 200:
            message = f"Successfully {action}ed the system"
            logger.info(message)
            write_log("blink", f"{action.upper()} event: {message} (entity: {ha_entity_id})")
            return {
                "message": message,
                "status": "success"
            }
        else:
            error_msg = f"HTTP {response.status_code}: {response.text}"
            logger.error(f"Failed to {action} system: {error_msg}")
            write_log("blink", f"{action.upper()} event FAILED: {error_msg} (entity: {ha_entity_id})")
            return {
                "error": f"Failed to {action} system",
                "message": f"Error occurred while trying to {action} the system: {error_msg}",
                "status": "error"
            }

    except Exception as e:
        error_msg = str(e)
        action_label = payload.get("action", "unknown") if isinstance(payload, dict) else "unknown"
        logger.error(f"Unexpected error during {action_label} operation: {error_msg}")
        write_log("blink", f"{action_label.upper()} event ERROR: {error_msg}")
        return {
            "error": "Operation failed",
            "message": f"Error occurred while trying to {action_label} the system: {error_msg}",
            "status": "error"
        }

if __name__ == "__main__":
    # Test the function with sample payloads
    print("Testing Home Assistant arm/disarm functionality...")

    # Test arm
    result = run({"action": "arm"})
    print(f"Arm test: {result}")

    # Test disarm
    result = run({"action": "disarm"})
    print(f"Disarm test: {result}")

    # Test invalid payload
    result = run("invalid")
    print(f"Invalid payload test: {result}")

    # Test missing action
    result = run({})
    print(f"Missing action test: {result}")

    print("Testing completed!")