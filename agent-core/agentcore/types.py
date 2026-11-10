"""Shared data types that flow between the seven layers."""
from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal


class FrozenAttributes(dict):
    """A read-only dict (name -> tuple of strings) that still copies, pickles and serialises like one."""

    def _read_only(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Principal.attributes is read-only")

    __setitem__ = __delitem__ = _read_only
    clear = pop = popitem = setdefault = update = _read_only
    __ior__ = _read_only

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenAttributes, (dict(self),))

    def __copy__(self) -> "FrozenAttributes":
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> "FrozenAttributes":
        return self                                     # immutable: keys are str, values tuples of str


@dataclass(frozen=True)
class Principal:
    """Who the agent is acting for. Scopes come from a verified token (Keycloak JWT in Project 01).

    `attributes` holds the token's other verified claims that policy decides on, such as the regions a
    caller may see: name -> tuple of strings. They are read-only and are not part of the hash."""
    id: str
    scopes: frozenset[str] = frozenset()
    attributes: Mapping[str, tuple[str, ...]] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        clean = {str(k): (v,) if isinstance(v, str) else tuple(str(x) for x in v)
                 for k, v in dict(self.attributes).items()}
        object.__setattr__(self, "attributes", FrozenAttributes(clean))

    def attr(self, name: str) -> tuple[str, ...]:
        """Values of one attribute; empty when the principal does not have it."""
        return self.attributes.get(name, ())


@dataclass
class Percept:
    """PERCEIVE output: a typed, validated view of the raw input. The LLM never sees raw input."""
    kind: str                      # e.g. "question", "edi_850", "incident", "pull_request"
    content: str                   # normalized text the reasoner reads
    principal: Principal
    entities: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    raw: Any = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class Step:
    """One unit of work in a PLAN, or a single ACT chosen by the reasoner."""
    description: str
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "done", "failed", "denied"] = "pending"


@dataclass
class Plan:
    goal: str
    steps: list[Step]


@dataclass
class ToolResult:
    tool: str
    ok: bool
    output: Any = None
    error: str | None = None
    latency_ms: float = 0.0
    denied: bool = False


@dataclass
class Decision:
    """THINK output. kind == 'answer' on the first iteration is the 'Can I answer directly?' = YES branch."""
    kind: Literal["answer", "tool", "plan", "escalate"]
    answer: str | None = None
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""


@dataclass
class Outcome:
    status: Literal["answered", "escalated", "rejected"]
    answer: str
    iterations: int
    tool_calls: int
    cost_usd: float
    trace_id: str
    answered_directly: bool = False
    tools_used: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)
