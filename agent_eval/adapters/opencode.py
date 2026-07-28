from typing import List

from agent_eval.adapters.base import AgentAdapter, TASK_PROMPT
from agent_eval.models import AgentRequest


class OpenCodeAdapter(AgentAdapter):
    name = "opencode"

    def agent_command(self, request: AgentRequest) -> List[str]:
        return [
            "/agent-tools/node_modules/.bin/opencode",
            "run",
            "--format",
            "json",
            "--pure",
            "--auto",
            "--dir",
            "/workspace",
            "--model",
            f"vllm-local/{request.model}",
            "--agent",
            "build",
            TASK_PROMPT,
        ]
