"""`:name` query placeholders for asyncpg, which only accepts positional
`$1, $2, ...` (cd-platform#166). A deliberately minimal shim -- not an
ORM or query builder:

    await pool.fetch(*named("SELECT ... WHERE user_id = :user_id", {"user_id": uid}))

Each distinct name gets a `$n` in first-seen order, so a name used twice
becomes one parameter, and the same query text always rewrites to the
same string (asyncpg caches prepared statements by exact query text).

Left alone: `::type` casts, '...' literals, "..." identifiers, and `--` /
`/* */` comments. Not handled, since no query here needs them:
dollar-quoted strings, E'...' backslash escapes, `arr[lo:hi]` slices.
"""

import re
from collections.abc import Mapping
from typing import Any

# Alternation is tried left to right at each position, so a literal,
# identifier, comment or `::` is consumed whole before `:name` can match
# inside it. Only the last alternative captures.
_TOKEN = re.compile(
    r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|--[^\n]*|/\*[\s\S]*?\*/|::|:([A-Za-z_]\w*)"""
)


def named(query: str, params: Mapping[str, Any]) -> tuple[Any, ...]:
    """Rewrite `:name` placeholders to `$n` and order `params` to match.
    Returns `(query, *args)`, ready to splat into fetch/fetchrow/execute.
    A placeholder missing from `params` raises KeyError; a key in `params`
    the query never uses raises ValueError (almost always a typo)."""
    slots: dict[str, int] = {}  # name -> n, insertion-ordered

    def number(match: re.Match[str]) -> str:
        name = match.group(1)
        if name is None:
            return match.group(0)
        return f"${slots.setdefault(name, len(slots) + 1)}"

    sql = _TOKEN.sub(number, query)
    if unused := params.keys() - slots.keys():
        raise ValueError(f"unused query parameters: {sorted(unused)}")
    return (sql, *(params[name] for name in slots))
