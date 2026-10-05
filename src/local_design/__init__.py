"""传动设备本地化设计闭环领域包。"""

from .contracts import LocalizationCase, stable_fingerprint, unique_by_identity
from .models import (
    ChangeKind,
    ConfigStatus,
    Conflict,
    ConflictStatus,
    ConflictType,
    DeliveredUnit,
    DesignDecision,
    EngineeringChange,
    Priority,
    ProductConfiguration,
    Requirement,
    RequirementStatus,
    Source,
    VerificationKind,
    VerificationRecord,
    VerificationResult,
    VoiceOfCustomer,
)
from .service import LocalDesignLoop, LoopError

__all__ = [
    "LocalizationCase",
    "stable_fingerprint",
    "unique_by_identity",
    "Source",
    "VoiceOfCustomer",
    "Requirement",
    "RequirementStatus",
    "Priority",
    "Conflict",
    "ConflictType",
    "ConflictStatus",
    "DesignDecision",
    "VerificationKind",
    "VerificationRecord",
    "VerificationResult",
    "ProductConfiguration",
    "ConfigStatus",
    "DeliveredUnit",
    "EngineeringChange",
    "ChangeKind",
    "LocalDesignLoop",
    "LoopError",
]
