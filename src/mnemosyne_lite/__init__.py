"""
mnemosyne-lite — the standalone SQLite memory surface of this repository.

Not to be confused with the engine (``mnemosyne-memory``), which owns the
``mnemosyne`` import package and the ``mnemosyne`` console script. This package
is deliberately named ``mnemosyne_lite`` so the two can never merge: installing
both under the same top-level name overwrote ``mnemosyne/__init__.py`` and
``mnemosyne/cli.py``, which broke ``from mnemosyne import Mnemosyne`` in the
provider's engine and replaced the engine's CLI.

This surface is a standalone keyword-recall store + CLI. It is NOT a Hermes
memory provider — that is ``integrations/hermes-provider/`` under the provider
id ``mnemosyne``.
"""

__version__ = "2.4.0"

__all__ = []
