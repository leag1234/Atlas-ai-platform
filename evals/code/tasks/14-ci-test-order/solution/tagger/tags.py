DEFAULT_ALIASES = {"colour": "color", "grey": "gray"}
_aliases = dict(DEFAULT_ALIASES)


def load_aliases(mapping):
    """Replace the active alias table with DEFAULT_ALIASES updated by ``mapping``.

    Each call starts again from DEFAULT_ALIASES: aliases loaded by an earlier call do not survive.
    DEFAULT_ALIASES itself is never modified.
    """
    global _aliases
    table = dict(DEFAULT_ALIASES)
    table.update({k.lower(): v.lower() for k, v in mapping.items()})
    _aliases = table


def normalize(tag):
    """Lowercase, strip, and map through the active alias table."""
    tag = tag.strip().lower()
    return _aliases.get(tag, tag)


def add_tags(record, extra=()):
    """Return the sorted, de-duplicated normalised tags of ``record`` plus ``extra``.

    Neither ``record`` nor ``extra`` is modified, and calls are independent of each other.
    """
    combined = list(record.get("tags", ())) + list(extra)
    return sorted(set(normalize(t) for t in combined))
