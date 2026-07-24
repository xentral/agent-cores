"""Xentral Basic's own emulated business objects.

This core owns every emulated entity it exposes: the concrete adapters live in
this package (customer, sales order, invoice, …), not in a shared pool, so Basic
is fully independent of any other core. The shared ``entity_registry.emulated``
package provides only the engine (adapter base class, gateway, composition
machinery). See ``registry`` for the ordered adapter set; ``manifest`` activates
it.
"""

from __future__ import annotations

from .registry import adapters

__all__ = ["adapters"]
