"""Runner layer — process-based execution of AI coding backends."""
from __future__ import annotations

from .base import BaseRunner
from .omp import OMPRunner

__all__ = ["BaseRunner", "OMPRunner"]
