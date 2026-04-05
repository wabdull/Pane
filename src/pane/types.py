"""Shared types for Pane."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Metadata:
    """Speaker-emitted metadata alongside a response."""
    entities: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    facts: list[dict] = field(default_factory=list)  # [{entity, key, value}, ...]
    topic: str = ""
    summary: str = ""  # emitted on topic shift/resolution


@dataclass
class Message:
    """A single conversation turn."""
    role: str  # "user" or "assistant"
    content: str
    timestamp: str = ""
    metadata: Optional[Metadata] = None


@dataclass
class Topic:
    """A group of messages about one subject."""
    id: str
    title: str
    summary: str  # short summary for loading into context (not raw messages)
    start_message_id: int
    end_message_id: int
    window_id: str
    tags: list[str] = field(default_factory=list)


@dataclass
class Entity:
    """A person, place, project, tool, or category in the user's world."""
    name: str
    type: str  # "person", "place", "project", "tool", "category", etc.
    aliases: list[str] = field(default_factory=list)


@dataclass
class RecallResult:
    """Result from a recall query."""
    mode: str  # "topic", "entity", "not_found"
    entities: list[str] = field(default_factory=list)  # entities mentioned in query
    topics: list[tuple] = field(default_factory=list)  # (topic_dict, score)
    n_results: int = 0
