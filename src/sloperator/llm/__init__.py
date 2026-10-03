"""LLM layer: pluggable chat backends used by roles."""

from sloperator.llm.backends import (
    BackendUnavailableError,
    LlmBackend,
    LlmError,
    LlmRequest,
    LlmResponse,
    OpenAICompatibleBackend,
    StructuredOutputError,
)
from sloperator.llm.routing import ResolvedRoute, RoleRouter, UnknownRoleError

__all__ = [
    "BackendUnavailableError",
    "LlmBackend",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "OpenAICompatibleBackend",
    "ResolvedRoute",
    "RoleRouter",
    "StructuredOutputError",
    "UnknownRoleError",
]
