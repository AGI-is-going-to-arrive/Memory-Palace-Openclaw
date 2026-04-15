from __future__ import annotations

import math
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api import maintenance as maintenance_api
import mcp_server
from mcp_runtime_services import safe_non_negative_int_impl


def test_mcp_server_safe_int_rejects_bool_values() -> None:
    assert mcp_server._safe_int(True, default=7) == 7
    assert mcp_server._safe_int(False, default=9) == 9


def test_mcp_server_safe_int_handles_overflow_values() -> None:
    assert mcp_server._safe_int(math.inf, default=11) == 11
    assert mcp_server._safe_int(-math.inf, default=12) == 12


def test_runtime_services_safe_non_negative_int_handles_overflow_values() -> None:
    assert safe_non_negative_int_impl(math.inf) == 0
    assert safe_non_negative_int_impl(-math.inf) == 0


def test_sanitize_search_event_rejects_bool_counts() -> None:
    payload = maintenance_api._sanitize_search_event(
        {
            "session_count": True,
            "global_count": False,
            "returned_count": True,
            "dedup_dropped": False,
            "session_contributed": True,
            "global_contributed": False,
        }
    )

    assert payload is not None
    assert payload["session_count"] == 0
    assert payload["global_count"] == 0
    assert payload["returned_count"] == 0
    assert payload["dedup_dropped"] == 0
    assert payload["session_contributed"] == 0
    assert payload["global_contributed"] == 0


def test_sanitize_search_event_rejects_overflow_counts() -> None:
    payload = maintenance_api._sanitize_search_event(
        {
            "session_count": math.inf,
            "global_count": -math.inf,
            "returned_count": math.inf,
            "dedup_dropped": -math.inf,
            "session_contributed": math.inf,
            "global_contributed": -math.inf,
        }
    )

    assert payload is not None
    assert payload["session_count"] == 0
    assert payload["global_count"] == 0
    assert payload["returned_count"] == 0
    assert payload["dedup_dropped"] == 0
    assert payload["session_contributed"] == 0
    assert payload["global_contributed"] == 0
