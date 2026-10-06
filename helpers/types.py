import builtins
from typing import Type


def type_name(t: Type) -> str:
    """Name used to store a column type in a table file."""
    return t.__name__

def type_from_name(name: str) -> Type:
    """Resolve a stored column type name. Only builtins round-trip; anything else becomes `object`."""
    t = getattr(builtins, name, None)
    if isinstance(t, type):
        return t
    return type(None) if name == "NoneType" else object
