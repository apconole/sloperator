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

__all__ = [
    "BackendUnavailableError",
    "LlmBackend",
    "LlmError",
    "LlmRequest",
    "LlmResponse",
    "OpenAICompatibleBackend",
    "StructuredOutputError",
]
