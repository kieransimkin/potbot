"""Language-neutral debugger sessions on explicitly admitted adapter streams.

No process launch, credential discovery or network connection occurs on import.
"""

from potbot.debugging.contracts import DebugIdentity, DebugState, Frame, Stop, ValueReference
from potbot.debugging.dap import DapAdapter
from potbot.debugging.session import DebugSession

__all__ = [
    "DapAdapter",
    "DebugIdentity",
    "DebugSession",
    "DebugState",
    "Frame",
    "Stop",
    "ValueReference",
]
