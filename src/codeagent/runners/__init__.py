"""Runner layer — process-based execution of AI coding backends."""
from __future__ import annotations

from .base import BaseRunner
from .go_wrapper import GoWrapperRunner
from .omp import OMPRunner

__all__ = ["BaseRunner", "GoWrapperRunner", "OMPRunner"]
