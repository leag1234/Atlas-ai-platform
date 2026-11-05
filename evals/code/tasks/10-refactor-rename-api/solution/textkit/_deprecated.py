import functools
import warnings


def alias(new, old_name):
    @functools.wraps(new)
    def wrapper(*args, **kwargs):
        warnings.warn(f"{old_name} is deprecated; use {new.__name__}", DeprecationWarning, stacklevel=2)
        return new(*args, **kwargs)

    wrapper.__name__ = old_name
    return wrapper
