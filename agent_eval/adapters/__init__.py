from agent_eval.adapters.base import AgentAdapter
from agent_eval.adapters.opencode import OpenCodeAdapter
from agent_eval.adapters.pi import PiAdapter


def create_adapter(name: str) -> AgentAdapter:
    if name == "pi":
        return PiAdapter()
    if name == "opencode":
        return OpenCodeAdapter()
    raise ValueError(f"unknown agent: {name}")


__all__ = ["AgentAdapter", "create_adapter"]
