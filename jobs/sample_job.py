"""Sample automation job.

Each job module exposes a run(payload) function. `payload` is the parsed
JSON body of the incoming webhook request (or {} if none was sent).
Return a JSON-serializable value describing what happened.
"""

import datetime


def run(payload):
    print(f"[sample_job] triggered at {datetime.datetime.now().isoformat()}")
    print(f"[sample_job] payload received: {payload}")

    # ... replace this with real automation work ...

    return {
        "message": "sample_job ran successfully",
        "received_payload": payload,
    }
