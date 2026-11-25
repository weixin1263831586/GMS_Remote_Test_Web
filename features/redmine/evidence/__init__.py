"""Redmine evidence collection, storage, derivation and search."""

# Load the service before its fetch engine, which imports shared service types.
from . import service


__all__ = ["service"]
