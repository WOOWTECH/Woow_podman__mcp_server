"""Declarative registry of every EMQX MCP tool.

Single source of truth shared by the MCP server (which registers the tools)
and the Admin GUI (which renders the on/off switches). Keep `name` in sync
with the `@mcp.tool(name=...)` used in `emqx_mcp_server.tools.*`.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class ToolCategory(str, Enum):
    CLUSTER = "Cluster & Monitoring"
    CLIENTS = "Client Management"
    TOPICS = "Topics & Subscriptions"
    MESSAGING = "Messaging"
    SECURITY = "Access Control"
    DIAGNOSTICS = "Diagnostics"
    INTEGRATION = "Data Integration"


class ToolSpec(BaseModel):
    """What the GUI needs to know about one tool."""

    name: str
    category: ToolCategory
    description: str
    operations: list[str] = Field(default_factory=list)
    dangerous: bool = False


def _t(name, category, description, operations=("read",), dangerous=False) -> ToolSpec:
    return ToolSpec(
        name=name,
        category=category,
        description=description,
        operations=list(operations),
        dangerous=dangerous,
    )


C = ToolCategory

TOOL_REGISTRY: list[ToolSpec] = [
    # ---------------------------------------------------------------- cluster
    _t("emqx_cluster_status", C.CLUSTER,
       "List cluster nodes with version, uptime, CPU and memory."),
    _t("emqx_node_detail", C.CLUSTER,
       "Full detail for one node, including load and connection counts."),
    _t("emqx_broker_stats", C.CLUSTER,
       "Live counters: connections, sessions, subscriptions, topics."),
    _t("emqx_metrics_current", C.CLUSTER,
       "Current throughput gauges used by the dashboard charts."),
    _t("emqx_metrics_history", C.CLUSTER,
       "Time-series metrics over a recent window."),
    _t("emqx_list_alarms", C.CLUSTER,
       "Active and historical broker alarms."),
    _t("emqx_prometheus_stats", C.CLUSTER,
       "Raw Prometheus exposition text for scraping or diffing."),

    # ---------------------------------------------------------------- clients
    _t("emqx_list_clients", C.CLIENTS,
       "List MQTT clients known to the broker, with filters."),
    _t("emqx_get_client", C.CLIENTS,
       "Full session detail for a single MQTT client."),
    _t("emqx_client_subscriptions", C.CLIENTS,
       "Topics a specific client is subscribed to."),
    _t("emqx_kick_client", C.CLIENTS,
       "Forcibly disconnect an MQTT client and clear its session.",
       ("delete",), True),
    _t("emqx_client_subscribe", C.CLIENTS,
       "Subscribe a client to a topic on its behalf.",
       ("create",), True),
    _t("emqx_client_unsubscribe", C.CLIENTS,
       "Remove a subscription from a client on its behalf.",
       ("delete",), True),

    # ----------------------------------------------------------------- topics
    _t("emqx_list_topics", C.TOPICS,
       "Routed topics currently present in the cluster."),
    _t("emqx_list_subscriptions", C.TOPICS,
       "All subscriptions cluster-wide, filterable by topic or client."),

    # -------------------------------------------------------------- messaging
    _t("emqx_publish", C.MESSAGING,
       "Publish one MQTT message through the broker.",
       ("create",), True),
    _t("emqx_publish_bulk", C.MESSAGING,
       "Publish a batch of MQTT messages in one call.",
       ("create",), True),
    _t("emqx_list_retained", C.MESSAGING,
       "Retained messages the broker is holding."),
    _t("emqx_get_retained", C.MESSAGING,
       "The retained message stored for one topic."),
    _t("emqx_delete_retained", C.MESSAGING,
       "Delete the retained message for one topic.",
       ("delete",), True),

    # --------------------------------------------------------------- security
    _t("emqx_list_authn", C.SECURITY,
       "Authenticator chain and the status of each authenticator."),
    _t("emqx_manage_authn_users", C.SECURITY,
       "List, create and delete MQTT users in the built-in database.",
       ("read", "create", "delete"), True),
    _t("emqx_list_authz_sources", C.SECURITY,
       "Authorization sources and their order in the chain."),
    _t("emqx_authz_settings", C.SECURITY,
       "Global authorization behaviour: no-match action, deny action, cache."),
    _t("emqx_manage_authz_rules", C.SECURITY,
       "Read and write built-in-database ACL rules for a user or client.",
       ("read", "create", "delete"), True),
    _t("emqx_list_banned", C.SECURITY,
       "Current ban list entries."),
    _t("emqx_ban", C.SECURITY,
       "Ban a client id, username or IP from connecting.",
       ("create",), True),
    _t("emqx_unban", C.SECURITY,
       "Lift a ban.",
       ("delete",), True),

    # ------------------------------------------------------------ diagnostics
    _t("emqx_list_traces", C.DIAGNOSTICS,
       "Packet traces currently defined on the broker."),
    _t("emqx_create_trace", C.DIAGNOSTICS,
       "Start a packet trace for a client, topic or IP.",
       ("create",), True),
    _t("emqx_get_trace_log", C.DIAGNOSTICS,
       "Read the captured log for a trace."),
    _t("emqx_delete_trace", C.DIAGNOSTICS,
       "Delete a trace; buffered events not yet flushed are lost.",
       ("delete",), True),
    _t("emqx_list_listeners", C.DIAGNOSTICS,
       "Listeners and their bind addresses and running state."),

    # ------------------------------------------------------------ integration
    _t("emqx_list_rules", C.INTEGRATION,
       "Rule-engine rules with their SQL and enabled state."),
    _t("emqx_get_rule_metrics", C.INTEGRATION,
       "Match, pass and failure counters for one rule."),
    _t("emqx_toggle_rule", C.INTEGRATION,
       "Enable or disable a rule.",
       ("update",), True),
    _t("emqx_test_rule_sql", C.INTEGRATION,
       "Dry-run rule SQL against a sample event without saving anything."),
    _t("emqx_list_connectors", C.INTEGRATION,
       "Data-integration connectors and their health."),
    _t("emqx_list_actions", C.INTEGRATION,
       "Outbound actions (bridges) and their health."),
]

TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOL_REGISTRY}


def categorized() -> dict[str, list[ToolSpec]]:
    """Registry grouped by category, in declaration order."""
    out: dict[str, list[ToolSpec]] = {c.value: [] for c in ToolCategory}
    for spec in TOOL_REGISTRY:
        out[spec.category.value].append(spec)
    return {k: v for k, v in out.items() if v}
