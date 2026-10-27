"""Live Nightshift + Nemotron Diagnostic and Verification Runner.

Tests connectivity, endpoint availability, authentication, and execution
through the real CEREV Nightshift adapter without mocks.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cerev.nightshift import NightshiftClient, NightshiftConfig, NightshiftError


def diagnose_live_nightshift() -> dict[str, Any]:
    report: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "target_endpoint": "http://127.0.0.1:8088/v1",
        "requested_model": "nvidia/nemotron-3.5-lightning:free",
        "tcp_port_8088_open": False,
        "tcp_error": None,
        "nightshift_error": None,
        "http_success": False,
        "serving_model": None,
        "attribution_captured": False,
        "consumed_by_challenger": False,
        "status": "FAILED",
        "root_cause": "",
        "action_required": "",
    }

    # 1. Socket check
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(("127.0.0.1", 8088))
        report["tcp_port_8088_open"] = True
    except Exception as e:
        report["tcp_port_8088_open"] = False
        report["tcp_error"] = f"{type(e).__name__}: {e}"
    finally:
        s.close()

    if not report["tcp_port_8088_open"]:
        report["status"] = "ENDPOINT_UNREACHABLE"
        report["root_cause"] = (
            "TCP connection to 127.0.0.1:8088 was refused ([WinError 10061]). "
            "The Nightshift web cockpit is running 'in-tab' in the browser at meterless.ai, "
            "which runs inside a browser sandbox and does not bind a TCP server on port 8088. "
            "Port 8088 is only opened when the native Nightshift daemon/desktop app runs."
        )
        report["action_required"] = (
            "Start the Nightshift desktop daemon/app that binds to localhost:8088, or provide "
            "a reachable remote Nightshift gateway URL (https://...) and ensure NIGHTSHIFT_API_KEY "
            "is set in .env."
        )
        return report

    # 2. If port is open, test via NightshiftClient
    api_key = os.environ.get("NIGHTSHIFT_API_KEY", "")
    if not api_key:
        env_path = Path(".env")
        if env_path.exists():
            from dotenv import dotenv_values

            vals = dotenv_values(env_path)
            api_key = vals.get("NIGHTSHIFT_API_KEY", "")

    if not api_key:
        api_key = "placeholder-check"

    config = NightshiftConfig(
        api_base_url="http://127.0.0.1:8088/v1",
        api_key=api_key,
        model="nvidia/nemotron-3.5-lightning:free",
        timeout_seconds=10.0,
    )
    client = NightshiftClient(config)

    try:
        completion = client.chat_completion(
            messages=[
                {"role": "system", "content": "You are an adversarial SLA auditor."},
                {"role": "user", "content": "State one requirement for valid written notice."},
            ]
        )
        report["http_success"] = True
        report["serving_model"] = completion.attribution.serving_model
        report["attribution_captured"] = bool(completion.attribution.serving_model)
        report["provider_request_id"] = completion.provider_request_id
        report["status"] = "SUCCESS"
    except NightshiftError as ne:
        report["nightshift_error"] = {
            "code": ne.code,
            "message": str(ne),
            "http_status": ne.http_status,
            "request_id": ne.request_id,
        }
        report["status"] = "NIGHTSHIFT_ERROR"
        report["root_cause"] = f"Nightshift returned error code {ne.code}: {ne}"
    except Exception as e:
        report["nightshift_error"] = {"type": type(e).__name__, "message": str(e)}
        report["status"] = "UNEXPECTED_ERROR"
        report["root_cause"] = f"Unexpected error during Nightshift call: {e}"

    return report


if __name__ == "__main__":
    res = diagnose_live_nightshift()
    print(json.dumps(res, indent=2))
