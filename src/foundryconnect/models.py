from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Protocol = Literal["responses", "chat", "anthropic"]
AGENTS = ("codex", "claude", "hermes", "opencode")
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
AI_SCOPE = "https://ai.azure.com/.default"


class FoundryError(Exception):
    def __init__(self, message: str, category: str = "configuration"):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class Profile:
    name: str
    endpoint: str
    deployment: str
    tenant: str
    subscription: str
    protocol: Protocol


@dataclass(frozen=True)
class Plan:
    agent: str
    path: Path
    format: Literal["json", "toml", "yaml"]
    changes: dict[tuple[str, ...], object]
    strategy: str
    guidance: tuple[str, ...] = ()
