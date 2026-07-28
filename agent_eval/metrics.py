import json
from typing import Iterable

from agent_eval.models import TrajectoryMetrics


def _usage_from_message(event):
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return 0, 0
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("input", 0) or 0), int(usage.get("output", 0) or 0)


def parse_trajectory(agent: str, lines: Iterable[str]) -> TrajectoryMetrics:
    metrics = TrajectoryMetrics()
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            metrics.parse_errors += 1
            continue

        event_type = str(event.get("type", ""))
        if agent == "pi":
            if event_type == "turn_end":
                metrics.turns += 1
            elif event_type == "tool_execution_start":
                metrics.tool_calls += 1
            if event_type == "message_end":
                input_tokens, output_tokens = _usage_from_message(event)
                metrics.input_tokens += input_tokens
                metrics.output_tokens += output_tokens
        else:
            lowered = event_type.lower()
            if lowered in {"step_finish", "turn_end"}:
                metrics.turns += 1
            if "tool" in lowered and any(
                marker in lowered for marker in ("start", "use", "call")
            ):
                metrics.tool_calls += 1
            usage = event.get("usage")
            if isinstance(usage, dict):
                metrics.input_tokens += int(usage.get("input", 0) or 0)
                metrics.output_tokens += int(usage.get("output", 0) or 0)

    return metrics
