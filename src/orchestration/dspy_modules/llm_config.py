"""DSPy adapter for the shared Hermes model configuration."""

try:
    from ..hermes_llm import configure_dspy
except ImportError:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from hermes_llm import configure_dspy

__all__ = ["configure_dspy"]
