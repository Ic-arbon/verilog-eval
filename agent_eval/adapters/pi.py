from typing import List

from agent_eval.adapters.base import AgentAdapter, TASK_PROMPT
from agent_eval.models import AgentRequest


class PiAdapter(AgentAdapter):
    name = "pi"

    def agent_command(self, request: AgentRequest) -> List[str]:
        return [
            "/agent-tools/node_modules/.bin/pi",
            "--mode",
            "json",
            "--no-session",
            "--approve",
            "--offline",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--provider",
            "vllm-local",
            "--model",
            request.model,
            "--api-key",
            "local",
            "--tools",
            "read,write,edit,bash",
            TASK_PROMPT,
        ]
