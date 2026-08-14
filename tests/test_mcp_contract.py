"""MCP naming conformance for DeleGate.

docs/canonical/mcp.naming.md requires service-owned tools to be namespaced as
`<service>.<verb_or_resource>`, and section 3 fixes this service's prefix as
`delegate.*`. Thirteen of fourteen tools were advertised unprefixed --
`create_delegation_plan`, `register_worker`, `match_workers` and so on -- which
is what a compatible client would copy from tools/list.

These guard both halves of the transition: the advertised surface is canonical,
and the legacy bare names still dispatch so existing callers keep working.
"""

from __future__ import annotations

import pytest

from delegate.mcp_http import MCP_TOOLS, _canonical_tool_name

CANONICAL_PREFIX = "delegate."


def test_every_advertised_tool_is_namespaced() -> None:
    offenders = [t["name"] for t in MCP_TOOLS if not t["name"].startswith(CANONICAL_PREFIX)]
    assert not offenders, f"unprefixed tools in tools/list: {offenders}"


def test_core_planning_tool_is_advertised_canonically() -> None:
    """The tool that makes this the planning authority must be namespaced."""
    names = {t["name"] for t in MCP_TOOLS}
    assert "delegate.create_delegation_plan" in names
    assert "create_delegation_plan" not in names


def test_advertised_names_are_unique() -> None:
    names = [t["name"] for t in MCP_TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names advertised"


@pytest.mark.parametrize(
    "legacy",
    [
        "create_delegation_plan",
        "validate_plan",
        "get_plan",
        "list_plans",
        "analyze_intent",
        "register_worker",
        "search_workers",
        "match_workers",
        "list_workers",
        "worker_status",
        "delete_worker",
        "stats",
        "cache_clear",
    ],
)
def test_legacy_bare_names_map_to_canonical(legacy: str) -> None:
    """Renaming the advertised surface must not break existing callers."""
    assert _canonical_tool_name(legacy) == f"{CANONICAL_PREFIX}{legacy}"


def test_historic_delegategate_health_alias_still_resolves() -> None:
    """This service answered to two spellings of its own name.

    `delegategate` remains correct as a Problemata *primitive type* (see
    problemata.spec.md); it was never correct as a tool prefix.
    """
    assert _canonical_tool_name("delegategate.health") == "delegate.health"


def test_canonical_names_pass_through_unchanged() -> None:
    assert _canonical_tool_name("delegate.create_delegation_plan") == "delegate.create_delegation_plan"
    assert _canonical_tool_name("delegate.health") == "delegate.health"


def test_unknown_names_are_not_rewritten() -> None:
    """Normalization must not invent a tool that does not exist."""
    assert _canonical_tool_name("definitely_not_a_tool") == "definitely_not_a_tool"
    assert _canonical_tool_name("asyncgate.create_task") == "asyncgate.create_task"
