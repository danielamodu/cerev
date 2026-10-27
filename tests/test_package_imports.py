"""Smoke tests for the Phase 0 package skeleton."""

from importlib import import_module


def test_planned_modules_import() -> None:
    """Confirm the reserved package modules are importable."""
    modules = (
        "config",
        "reference",
        "v21_simulation",
        "evidence",
        "validation",
        "policy",
        "receipt",
        "replay",
        "permit",
        "nonce",
        "retrieval_direct",
        "adapter",
        "orchestration",
        "nightshift",
    )

    for module in modules:
        import_module(f"cerev.{module}")
