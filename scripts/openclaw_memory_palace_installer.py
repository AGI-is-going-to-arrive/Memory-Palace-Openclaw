#!/usr/bin/env python3
"""Backward-compatible facade -- real code lives in scripts/installer/.

When test code does ``mock.patch.object(installer, "backend_root", ...)``, the
mock replaces the name only in *this* module's namespace.  Functions in the
sub-modules (``_utils``, ``_core``, …) that were split from the original
monolith still hold their **own** bindings imported via ``from ._xxx import *``.

To keep mock-based patching working transparently, this facade uses a thin
module wrapper (``_PatchableModule``) whose ``__setattr__`` propagates every
attribute write to the sub-modules that also own the same name.  This means
``setattr(installer, "backend_root", mock_obj)`` automatically updates
``installer._utils.backend_root``, ``installer._core.backend_root``, etc.
"""
from __future__ import annotations

import sys as _sys
import types as _types

from installer import *  # noqa: F401,F403
from installer._constants import *  # noqa: F401,F403
from installer._utils import *  # noqa: F401,F403
from installer._provider import *  # noqa: F401,F403
from installer._onboarding import *  # noqa: F401,F403
from installer._core import *  # noqa: F401,F403

# Re-export private names for backward compatibility
from installer import (  # noqa: F401
    _PROFILE_PLACEHOLDER_MARKERS,
    _ENV_ALIAS_WARNED,
    _ENV_LEGACY_ALIASES,
    _apply_private_file_permissions,
    _backend_api_service_ready,
    _build_pid_file_record,
    _build_onboarding_provider_sections,
    _build_onboarding_questions,
    _build_profile_boundary,
    _cleanup_timed_out_process,
    _component_accepted_forms,
    _component_title,
    _component_usage_summary,
    _dashboard_service_ready,
    _find_available_loopback_port,
    _is_process_alive,
    _kill_process_tree_windows,
    _localized_onboarding_text,
    _mask_example_value,
    _metadata_key,
    _normalize_pid_command,
    _normalize_port,
    _onboarding_command_preview,
    _onboarding_component_payload,
    _onboarding_field_payload,
    _onboarding_profile_boundary_payload,
    _path_exists,
    _pid_file_record_matches_running_process,
    _port_open,
    _provider_missing_fields_by_component,
    _provider_probe_detail_missing,
    _provider_probe_detail_not_checked,
    _provider_probe_detail_optional,
    _provider_probe_detail_pass,
    _provider_probe_summary_embedding_dimension,
    _provider_probe_summary_failures,
    _provider_probe_summary_fallback,
    _provider_probe_summary_incomplete,
    _provider_probe_summary_not_required,
    _provider_probe_summary_pass,
    _provider_requires_external_models,
    _quote_json_like_keys,
    _read_optional_int,
    _read_pid_file,
    _read_pid_file_record,
    _read_process_command_line,
    _read_process_start_marker,
    _remove_file_if_exists,
    _resolve_onboarding_provided_overrides,
    _run_runtime_migration_task,
    _runtime_paths,
    _strip_json_like_comments,
    _strip_json_like_trailing_commas,
    _strip_wrapping_quotes,
    _terminate_process,
    _wait_for_port_closed,
    _wait_for_process_exit,
    _wait_for_process_group_exit,
    _write_pid_file,
)
from installer._core import main

# ---------------------------------------------------------------------------
# Module wrapper: propagate ``setattr`` to sub-modules so ``mock.patch``
# applied to this facade also patches the sub-modules' own bindings.
# ---------------------------------------------------------------------------
import installer._constants as _mod_constants  # noqa: E402
import installer._utils as _mod_utils  # noqa: E402
import installer._provider as _mod_provider  # noqa: E402
import installer._onboarding as _mod_onboarding  # noqa: E402
import installer._core as _mod_core  # noqa: E402

_SUB_MODULES = (_mod_constants, _mod_utils, _mod_provider, _mod_onboarding, _mod_core)


class _PatchableModule(_types.ModuleType):
    """Drop-in module wrapper that propagates attribute writes to sub-modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for sub in _SUB_MODULES:
            if hasattr(sub, name):
                _types.ModuleType.__setattr__(sub, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for sub in _SUB_MODULES:
            if hasattr(sub, name):
                try:
                    _types.ModuleType.__delattr__(sub, name)
                except AttributeError:
                    pass


# Replace this module in sys.modules with the patchable wrapper.
_real = _sys.modules[__name__]
_wrapper = _PatchableModule(__name__, __doc__)
_wrapper.__dict__.update(_real.__dict__)
_wrapper.__file__ = _real.__file__
_wrapper.__loader__ = getattr(_real, "__loader__", None)
_wrapper.__spec__ = getattr(_real, "__spec__", None)
_wrapper.__path__ = getattr(_real, "__path__", [])
_wrapper.__package__ = getattr(_real, "__package__", None)
_sys.modules[__name__] = _wrapper


if __name__ == "__main__":
    raise SystemExit(main())
