"""Ask-the-funnel analytics agent.

A small, guarded text-to-insight agent over a privacy-aware hearing-test
funnel (web events -> app events, linkable only through an identity bridge).
"""

from agent.agent import FunnelAgent
from agent.db import get_driver
from agent.llm import get_llm

__all__ = ["FunnelAgent", "get_driver", "get_llm"]
