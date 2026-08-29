"""Model provider adapters."""

from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.model_providers.openai_compatible_chat import (
    OpenAICompatibleChatProvider,
)
from deskpilot.model_providers.openai_compatible_responses import (
    OpenAICompatibleResponsesProvider,
)

__all__ = [
    "FakeModelProvider",
    "OpenAICompatibleChatProvider",
    "OpenAICompatibleResponsesProvider",
]
