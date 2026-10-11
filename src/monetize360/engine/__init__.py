"""PURE pricing engine. No I/O, no clock, no randomness, no imports from api/store/security/control.
Frozen during the demo.

Public API (docs/ENGINE_SPEC.md §1) - export exactly these:
    compile_pack, evaluate, explain, Context, Policy, Result, Row,
    EngineError, PackInvalid, SignalInvalid, SignalMissing, SignalStale,
    LookupMiss, ExclusiveConflict, BoundsConflict, RuleError, InvariantViolation
"""
from .compiler import compile_pack
from .evaluate import evaluate
from .explain import explain
from .models import (
    BoundsConflict,
    Context,
    EngineError,
    ExclusiveConflict,
    InvariantViolation,
    LookupMiss,
    PackInvalid,
    Policy,
    Result,
    Row,
    RuleError,
    SignalInvalid,
    SignalMissing,
    SignalStale,
)

__all__ = [
    "compile_pack", "evaluate", "explain", "Context", "Policy", "Result", "Row",
    "EngineError", "PackInvalid", "SignalInvalid", "SignalMissing", "SignalStale",
    "LookupMiss", "ExclusiveConflict", "BoundsConflict", "RuleError", "InvariantViolation",
]
