from abc import ABC, abstractmethod
from typing import List

from agent_eval.models import AgentRequest


TASK_PROMPT = (
    "Do not narrate or announce planned actions. The vLLM server requires "
    "Qwen3-Coder native tool syntax. Never write pseudo calls such as "
    "<read.filePath=...>. Start by calling read exactly in this format:\n"
    "<tool_call>\n<function=read>\n<parameter=filePath>\n"
    "/workspace/TASK.md\n</parameter>\n</function>\n</tool_call>\n"
    "Then read AGENT_INSTRUCTIONS.md using the same native format. Implement "
    "the task completely, use tools to compile and self-test it, and continue "
    "until /workspace/TopModule.sv exists and compiles. A text-only response "
    "before TopModule.sv exists is a failure."
)


class AgentAdapter(ABC):
    name: str

    @abstractmethod
    def agent_command(self, request: AgentRequest) -> List[str]:
        raise NotImplementedError
