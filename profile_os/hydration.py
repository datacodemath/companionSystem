"""Model-facing projection for companion hydration results.

The backend keeps delivery bookkeeping for its REST/API callers. MCP clients
need the companion context and actionable warnings, not the bookkeeping used
to decide which sections to assemble.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def model_facing_hydration(packet: dict[str, Any]) -> dict[str, Any]:
    """Return the context-bearing subset sent to an MCP model client.

    Preserve the canonical prompts, access and memory constraints, task brief,
    and conversational state. Strip receipt/reload/section inventory details,
    duplicate selection fields, and registry copies of prompt identity.
    """
    result = deepcopy(packet)
    hydration = result.pop("hydration", None)
    result.pop("selection", None)

    profile = result.get("profile")
    if isinstance(profile, dict):
        profile.pop("signature", None)  # top-level signature is canonical
        profile.pop("aliases", None)  # resolution is already complete

    invocation = result.get("invocation")
    if isinstance(invocation, dict):
        # Requested mode and policy version are transport bookkeeping. The
        # effective mode and its safety instructions are what the model needs.
        invocation.pop("requested_mode", None)
        invocation.pop("policy_version", None)
        invocation.pop("isolation", None)  # retained in actor instructions

    profile_id = profile.get("id") if isinstance(profile, dict) else None
    directory = result.get("companion_directory")
    if isinstance(directory, list) and profile_id:
        # The current companion is already represented by its canonical
        # profile prompts; avoid echoing its lane and signature in the roster.
        result["companion_directory"] = [
            item for item in directory
            if not isinstance(item, dict) or item.get("id") != profile_id
        ]

    if isinstance(hydration, dict):
        warnings = hydration.get("warnings")
        if isinstance(warnings, list) and warnings:
            existing_warnings = result.get("warnings")
            if isinstance(existing_warnings, list):
                result["warnings"] = [*existing_warnings, *warnings]
            elif existing_warnings is None:
                result["warnings"] = warnings
    return result
