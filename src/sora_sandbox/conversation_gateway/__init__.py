"""Production local-first conversation gateway for Project Sora."""

from .contracts import (
    GatewayError,
    GatewayEvent,
    ProtocolVersion,
    TurnAccepted,
    TurnSubmit,
)
from .client import ConversationGatewayClient, GatewayClientConfig
from .host import create_gateway_app
from .runtime_port import LocalConversationRuntimeAdapter
from .security import CapabilityClaims, LocalCapabilityProvider
from .service import ConversationGatewayService, GatewayPolicy
from .supervisor import GatewayChildProcess, GatewayProcessSupervisor

__all__ = [
    "CapabilityClaims",
    "ConversationGatewayClient",
    "ConversationGatewayService",
    "GatewayError",
    "GatewayChildProcess",
    "GatewayClientConfig",
    "GatewayEvent",
    "GatewayPolicy",
    "GatewayProcessSupervisor",
    "LocalCapabilityProvider",
    "LocalConversationRuntimeAdapter",
    "ProtocolVersion",
    "TurnAccepted",
    "TurnSubmit",
    "create_gateway_app",
]
