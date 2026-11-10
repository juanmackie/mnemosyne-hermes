"""Benchmark harnesses.

A package (not a namespace) so `pytest tests` can import the shared evaluation
helpers the way the tests already do (`from bench.precision_recall_eval import …`).
Without it a rootdir-relative import only works when bench/ happens to be on
sys.path, and the test module fails to collect.
"""
