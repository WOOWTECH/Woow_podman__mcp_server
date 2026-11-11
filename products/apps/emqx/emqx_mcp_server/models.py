"""Response models. Every tool returns one, so the model gets an outputSchema."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BrokerStats(BaseModel):
    """Live broker counters."""

    connections: int = Field(description="Currently connected MQTT clients.")
    sessions: int = Field(description="Sessions held, including offline ones.")
    subscriptions: int = Field(description="Active subscriptions across the cluster.")
    topics: int = Field(description="Distinct routed topics.")
    retained: int = Field(0, description="Retained messages held by the broker.")


class ClientInfo(BaseModel):
    """One MQTT client as EMQX reports it."""

    clientid: str = Field(description="MQTT client identifier.")
    username: str | None = Field(None, description="Authenticated username, if any.")
    connected: bool = Field(description="Whether the client is connected right now.")
    ip_address: str | None = Field(None, description="Peer IP address.")
    proto_ver: int | None = Field(None, description="MQTT version (4 = 3.1.1, 5 = 5.0).")
    connected_at: str | None = Field(None, description="ISO-8601 connection time.")


class ClientList(BaseModel):
    """A page of MQTT clients."""

    clients: list[ClientInfo]
    total: int = Field(description="Total clients matching the filters.")
    has_more: bool = Field(description="True when further pages exist.")


class KickResult(BaseModel):
    """Outcome of disconnecting a client."""

    clientid: str
    kicked: bool = Field(description="True when the broker accepted the disconnect.")


class RetainedMessage(BaseModel):
    """A retained message, with its payload already decoded."""

    topic: str = Field(description="Topic the message is retained on.")
    found: bool = Field(description="False when nothing is retained there.")
    payload: str | None = Field(None, description="Decoded payload text.")
    qos: int | None = Field(None, description="QoS the message was published at.")
    publish_at: str | None = Field(None, description="When it was published.")
    lookup: str | None = Field(
        None,
        description="How it was located: 'direct' or 'listing'. Hierarchical "
        "topics cannot be addressed directly by EMQX's REST API.",
    )


class AuthnUser(BaseModel):
    """An MQTT account in the built-in authentication database."""

    user_id: str = Field(description="The MQTT username.")
    is_superuser: bool = Field(False, description="Whether the account bypasses ACLs.")


class AuthnUserResult(BaseModel):
    """Result of a built-in-database user operation."""

    operation: str = Field(description="The operation that was performed.")
    users: list[AuthnUser] = Field(
        default_factory=list, description="Users returned by a read."
    )
    user_id: str | None = Field(None, description="Subject of a create or delete.")
    succeeded: bool = True
