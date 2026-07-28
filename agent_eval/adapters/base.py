from abc import ABC, abstractmethod
from typing import List

from agent_eval.models import AgentRequest


TASK_PROMPT = (
    "Do not narrate or announce planned actions. Start immediately by calling "
    "the read tool on TASK.md and AGENT_INSTRUCTIONS.md. Then implement the task "
    "completely, use tools to compile and self-test it, and continue until "
    "/workspace/TopModule.sv exists and compiles. A text-only response before "
    "TopModule.sv exists is a failure."
)


class AgentAdapter(ABC):
    name: str

    @abstractmethod
    def agent_command(self, request: AgentRequest) -> List[str]:
        raise NotImplementedError
