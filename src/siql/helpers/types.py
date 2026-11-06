import builtins
from typing import Type

from .vector import Vector

# Column types that aren't builtins but are stored by name
_NAMED = {"Vector": Vector}


def type_name(t: Type) -> str:
    """Name used to store a column type in a table file."""
    return t.__name__

def type_from_name(name: str) -> Type:
    """Resolve a stored column type name. Builtins and `Vector` round-trip; anything else becomes `object`."""
    if name in _NAMED:
        return _NAMED[name]
    t = getattr(builtins, name, None)
    if isinstance(t, type):
        return t
    return type(None) if name == "NoneType" else object
