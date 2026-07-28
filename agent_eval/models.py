from dataclasses import asdict, dataclass
from typing import Dict


@dataclass
class TrajectoryMetrics:
    turns: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    parse_errors: int = 0

    def to_dict(self) -> Dict[str, int]:
        return asdict(self)
