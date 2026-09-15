#!/usr/bin/env python3
"""Quick validation of semantic metrics (reduced API calls)."""

import dspy
import sys
try:
    from .llm_config import configure_dspy
except ImportError:
    from llm_config import configure_dspy
import os
from semantic_metrics import SemanticSimilarityJudge, semantic_requirement_f1

# This file is a CLI smoke script, not a pytest module; avoid making a model
# call while pytest is collecting files named test_*.py.
if "pytest" in sys.modules:
    import pytest
    pytest.skip("CLI smoke script", allow_module_level=True)

# Configure DSPy
if configure_dspy(dspy) is None:
    print("Hermes model not configured; run `hermes setup --portal`")
    raise SystemExit(1)

print("Testing semantic similarity judge...")
print()

judge = SemanticSimilarityJudge()

# Test 1: Exact match
print("Test 1: Exact Match")
score1 = judge(
    "Add login endpoint accepting email and password",
    "Add login endpoint accepting email and password"
)
print(f"Score: {score1:.3f} (expected ~1.0) {'✓' if score1 >= 0.9 else '✗'}")
print()

# Test 2: Semantic equivalent
print("Test 2: Semantic Equivalent")
score2 = judge(
    "Add login endpoint accepting email and password",
    "Implement authentication endpoint that accepts user credentials"
)
print(f"Score: {score2:.3f} (expected 0.7-0.9) {'✓' if 0.7 <= score2 <= 0.9 else '✗'}")
print()

# Test 3: Unrelated
print("Test 3: Unrelated")
score3 = judge(
    "Add login endpoint accepting email and password",
    "Implement WebSocket notifications"
)
print(f"Score: {score3:.3f} (expected ~0.0) {'✓' if score3 <= 0.2 else '✗'}")
print()

# Test 4: Requirement set F1
print("Test 4: Requirement Set F1")
example = dspy.Example(
    requirements=[
        "Add login endpoint",
        "Hash passwords",
        "Generate JWT tokens"
    ]
).with_inputs()

pred = dspy.Prediction(requirements=[
    "Implement authentication endpoint",  # Matches "Add login endpoint"
    "Hash user passwords with bcrypt",  # Matches "Hash passwords"
    "Create JWT access tokens"  # Matches "Generate JWT tokens"
])

f1_score = semantic_requirement_f1(example, pred, threshold=0.6)
print(f"F1 Score: {f1_score:.3f} (expected 0.7-1.0) {'✓' if f1_score >= 0.7 else '✗'}")
print()

print("Quick validation complete!")
print("✓ Metrics are working correctly" if all([
    score1 >= 0.9,
    0.7 <= score2 <= 0.9,
    score3 <= 0.2,
    f1_score >= 0.7
]) else "✗ Some tests failed")
