"""Insidious SDK: get a self-hosted D2C store through festive sale night."""

from .config import load_config, load_flags
from .db import FlagRoutedDatabase, PoolConfig, instrument_engine
from .degrade import degradable, get_context, set_context
from .events import EventTracker
from .flags import FlagClient, FlagSnapshot, FlagState, pick_variant
from .kit import Insidious
from .metrics import MetricsMiddleware

__all__ = [
    "Insidious",
    "FlagClient",
    "FlagSnapshot",
    "FlagState",
    "EventTracker",
    "MetricsMiddleware",
    "degradable",
    "set_context",
    "get_context",
    "load_config",
    "load_flags",
    "pick_variant",
    "FlagRoutedDatabase",
    "PoolConfig",
    "instrument_engine",
]

__version__ = "0.1.0"
