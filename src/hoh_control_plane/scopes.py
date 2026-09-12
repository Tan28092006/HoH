"""What a file-scope pattern points at, as plain string logic.

Two things need this and neither should own it: lease collision decides whether
two tickets may run at once, and experience decides whether a lesson reaches the
work in front of you. If they disagree about what overlaps, one of them is
wrong, and which one is wrong depends on which file you happen to read.

Kept free of any dependency so the store can import it too. The store needs it
to index what a note attaches to, and a module that imports the service layer
cannot be imported by the layer underneath it.
"""
from __future__ import annotations

_GLOB_CHARACTERS = "*?["


def scope_root(scope: str) -> str:
    """The fixed leading path of a pattern, with the glob tail removed.

    "src/payments/**" is rooted at "src/payments"; "src/**/test_*.py" at "src".
    A pattern that begins with a glob has no root at all, which means it could
    match anywhere -- represented as the empty string, and treated by callers as
    overlapping everything rather than nothing.
    """
    normalized = scope.replace("\\", "/").strip("/")
    parts: list[str] = []
    for part in normalized.split("/"):
        if any(character in part for character in _GLOB_CHARACTERS):
            break
        if part:
            parts.append(part)
    return "/".join(parts)


def roots_of(scopes: list[str] | tuple[str, ...]) -> list[str]:
    """The distinct roots of a scope list, in a stable order."""
    seen: dict[str, None] = {}
    for scope in scopes or ():
        seen.setdefault(scope_root(str(scope)), None)
    return list(seen)


def roots_overlap(left: str, right: str) -> bool:
    """Whether two roots can name the same file.

    An empty root matches everything, and otherwise one has to contain the
    other -- compared segment-wise, so "src/pay" does not contain "src/payments".
    """
    if not left or not right:
        return True
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


def ancestors_of(root: str) -> list[str]:
    """Every root that contains this one, including itself and the empty root.

    This is the half of an overlap test that an index can answer with equality:
    a note rooted at "src" reaches a ticket rooted at "src/payments/retry", and
    the list of roots that could do so is short and known in advance.
    """
    if not root:
        return [""]
    parts = root.split("/")
    return [""] + ["/".join(parts[: index + 1]) for index in range(len(parts))]


def descendant_range(root: str) -> tuple[str, str] | None:
    """A half-open range containing exactly the roots nested under this one.

    The other half of the overlap test, and the reason it can stay in SQL: "/"
    is 0x2F, so everything starting with "root/" sorts below "root0". Expressed
    as a range rather than a LIKE because a range uses an ordinary index on both
    backends, while a pattern match is only sometimes able to.
    """
    if not root:
        return None
    return (f"{root}/", f"{root}0")
