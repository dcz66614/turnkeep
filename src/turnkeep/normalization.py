"""Normalisation (T010). Declared string handling ONLY: Unicode NFC + JSON
key ordering. No semantic merging: case, negation, number sign and time
meaning are NOT normalised away. Synonyms / cross-key contradictions are
explicitly NOT detected by this module (contract §4)."""
from __future__ import annotations
from .storage import _normal, _json


def normalise_value(value):
    """Return the canonical JSON value (NFC, sorted keys)."""
    return _normal(value)


def encode_value(value):
    """Canonical JSON string used for equality comparison in the DB."""
    return _json(value)


def values_equal(a, b):
    return _json(a) == _json(b)
