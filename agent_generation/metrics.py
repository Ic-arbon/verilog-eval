"""Conservative token, turn, and tool aggregation from Agent JSONL streams."""

from __future__ import annotations

from typing import Optional

from agent_generation.contracts import AgentUsage
from agent_generation.drivers.base import AgentDriver


def _nonnegative_integer(value: object) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def _token_value(tokens: dict, *names: str) -> Optional[int]:
    for name in names:
        value = _nonnegative_integer(tokens.get(name))
        if value is not None:
            return value
    return None


def aggregate_trajectory_usage(
    driver: AgentDriver,
    trajectory: str,
) -> AgentUsage:
    """Aggregate only explicit usage events; never infer tokens from text."""

    input_tokens = 0
    output_tokens = 0
    saw_input_tokens = False
    saw_output_tokens = False
    turns = 0
    tool_calls = 0

    for line in trajectory.splitlines():
        event = driver.parse_event(line)
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")

        if event_type in {"turn_end", "step_finish"}:
            turns += 1
        if event_type in {"tool_execution_start", "tool_use"}:
            tool_calls += 1

        tokens = None
        reasoning_tokens = None
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                usage = message.get("usage")
                if isinstance(usage, dict):
                    tokens = usage
        elif event_type == "step_finish":
            part = event.get("part")
            if isinstance(part, dict):
                value = part.get("tokens")
                if isinstance(value, dict):
                    tokens = value
                    reasoning_tokens = _token_value(value, "reasoning")

        if tokens is None:
            continue
        input_value = _token_value(tokens, "input", "prompt_tokens")
        output_value = _token_value(tokens, "output", "completion_tokens")
        if input_value is not None:
            input_tokens += input_value
            saw_input_tokens = True
        if output_value is not None:
            output_tokens += output_value + (reasoning_tokens or 0)
            saw_output_tokens = True

    return AgentUsage(
        input_tokens=input_tokens if saw_input_tokens else None,
        output_tokens=output_tokens if saw_output_tokens else None,
        turns=turns,
        tool_calls=tool_calls,
        usage_source="trajectory",
    )
