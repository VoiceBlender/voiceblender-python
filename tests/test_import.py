"""Smoke test: the package imports and exposes its public surface."""

from __future__ import annotations

import voiceblender


def test_version() -> None:
    assert isinstance(voiceblender.__version__, str)


def test_public_names_resolve() -> None:
    """Every name in __all__ must resolve to something non-None.

    Names that depend on M3+ generated modules are allowed to be ``None``
    during early milestones; once those milestones land the corresponding
    asserts here flip to is-not-None.
    """
    for name in voiceblender.__all__:
        assert hasattr(voiceblender, name), name


def test_error_predicates_callable() -> None:
    assert callable(voiceblender.is_not_found)
    assert callable(voiceblender.is_conflict)
    assert callable(voiceblender.is_bad_request)
