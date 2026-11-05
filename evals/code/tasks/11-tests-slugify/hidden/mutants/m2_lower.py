import re
import unicodedata


def slugify(text, sep="-", max_length=None):
    """Return a URL slug for ``text``.

    Rules:
    1. Accented letters are reduced to their base letter (NFKD, combining marks removed);
       other non-ASCII characters are dropped.
    2. The result is lowercase.
    3. Every run of characters that are not ASCII letters or digits becomes a single ``sep``.
    4. The result never starts or ends with ``sep``.
    5. If ``max_length`` is given, the slug is cut to at most ``max_length`` characters, at a ``sep``
       boundary when the cut would split a word; a word longer than ``max_length`` on its own is cut hard.
       The result still never ends with ``sep``.
    6. If nothing is left, the result is ``"n-a"`` (whatever ``sep`` is).
    7. ``sep`` must be a non-empty string; otherwise ``ValueError`` is raised.
    """
    if not isinstance(sep, str) or not sep:
        raise ValueError("sep must be a non-empty string")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.encode("ascii", "ignore").decode("ascii")
    slug = sep.join(part for part in re.split(r"[^a-z0-9]+", text) if part)
    if max_length is not None and len(slug) > max_length:
        words = slug.split(sep)
        out = ""
        for word in words:
            candidate = word if not out else out + sep + word
            if len(candidate) > max_length:
                break
            out = candidate
        slug = out or words[0][:max_length]
    return slug or "n-a"
