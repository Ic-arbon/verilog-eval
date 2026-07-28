from abc import ABC, abstractmethod
from typing import List

from agent_eval.models import AgentRequest


TASK_PROMPT = (
    "Read TASK.md and AGENT_INSTRUCTIONS.md. Implement the task completely, "
    "use the available tools to compile and self-test it, and leave the final "
    "answer in /workspace/TopModule.sv."
)


class AgentAdapter(ABC):
    name: str

    @abstractmethod
    def agent_command(self, request: AgentRequest) -> List[str]:
        raise NotImplementedError
