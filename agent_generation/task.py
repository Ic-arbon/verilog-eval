"""Public benchmark guidance shared by the Agent generator entry point."""

from __future__ import annotations

from typing import Optional


SPEC_TO_RTL_RULES = """\
Here are some additional rules and coding conventions.

 - Declare all ports and signals as logic; do not to use wire or reg.

 - For combinational logic with an always block do not explicitly specify
   the sensitivity list; instead use always @(*).

 - All sized numeric constants must have a size greater than zero
   (e.g, 0'b0 is not a valid expression).

 - An always block must read at least one signal otherwise it will never be
   executed; use an assign statement instead of an always block in
   situations where there is no need to read any signals.

 - if the module uses a synchronous reset signal, this means the reset
   signal is sampled with respect to the clock. When implementing a
   synchronous reset signal, do not include posedge reset in the
   sensitivity list of any sequential always block.
"""


def selected_rules(task: str, enabled: bool) -> Optional[str]:
    if not enabled or task != "spec-to-rtl":
        return None
    return SPEC_TO_RTL_RULES
