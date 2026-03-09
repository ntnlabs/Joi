"""
Wind - Joi's proactive messaging capability.

Phase 0-1: Shadow mode (data model + logging, no actual sends).
"""

from .config import WindConfig
from .state import WindStateManager, WindState
from .topics import TopicManager, PendingTopic
from .logging import WindDecisionLogger, WindDecision
from .impulse import ImpulseEngine, GateResult, ImpulseResult
from .orchestrator import WindOrchestrator

__all__ = [
    "WindConfig",
    "WindStateManager",
    "WindState",
    "TopicManager",
    "PendingTopic",
    "WindDecisionLogger",
    "WindDecision",
    "ImpulseEngine",
    "GateResult",
    "ImpulseResult",
    "WindOrchestrator",
]
