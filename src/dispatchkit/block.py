"""The `<!-- dispatchkit ... -->` machine block (D3).

Every issue body ends with this block, so the scheduler never has to parse
prose. Two rules govern the code below, both driven by the fact that issue
bodies are attacker-influencable:

1. **The grammar is fixed and tiny** — `key: value`, where a value is a plain
   scalar, a double-quoted string, or a flow list of those. The output is valid
   YAML, but nothing here is a general YAML loader: a closed key set and a
   closed value grammar is a strictly smaller attack surface than a safe loader
   plus a schema check, and it needs no dependency.
2. **Ambiguity is refused, not resolved.** A body carrying two blocks is an
   error rather than "use the first one", because "the first one" is precisely
   the assumption an injected second block would exploit.

Rendering is deterministic: `apply` diffs bodies to decide whether to update,
so an unstable renderer would rewrite every issue on every pass.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from dispatchkit.errors import GraphError, GraphIssue
from dispatchkit.model import CAPABILITIES, SLUG, Lane, Task, TaskId, Verify

OPEN = "<!-- dispatchkit"
CLOSE = "-->"
KEYS = ("id", "plan", "milestone", "lane", "requires", "verify", "spend", "depends", "touches")

_PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_LINE = re.compile(r"^(?P<key>[a-z_]+):[ ](?P<value>.*)$")


@dataclass(frozen=True, slots=True)
class MachineBlock:
    """The scheduler's view of a task, recovered from an issue body."""

    id: TaskId
    plan: str
    milestone: str
    lane: Lane
    requires: tuple[str, ...]
    verify: Verify
    spend: bool
    depends: tuple[TaskId, ...]
    touches: tuple[str, ...]


def render_block(task: Task, *, plan: str) -> str:
    values = {
        "id": _scalar(task.id),
        "plan": _scalar(plan),
        "milestone": _scalar(task.milestone),
        "lane": task.lane.value,
        "requires": _list(task.requires),
        "verify": task.verify.value,
        "spend": "true" if task.spend else "false",
        "depends": _list(edge.on for edge in task.depends),
        "touches": _list(task.touches),
    }
    body = "\n".join(f"{key}: {values[key]}" for key in KEYS)
    return f"{OPEN}\n{body}\n{CLOSE}"


def parse_block(body: str) -> MachineBlock:
    """Recover a `MachineBlock` from an issue body. Raises `GraphError`."""
    lines = _extract(body)
    issues: list[GraphIssue] = []
    raw: dict[str, str] = {}

    for line in lines:
        match = _LINE.match(line)
        if match is None:
            issues.append(GraphIssue("block-syntax", "block", f"unparsable line: {line!r}"))
            continue
        key = match.group("key")
        if key not in KEYS:
            issues.append(GraphIssue("unknown-key", "block", f"unknown key `{key}`"))
        elif key in raw:
            issues.append(GraphIssue("duplicate-key", "block", f"`{key}` appears twice"))
        else:
            raw[key] = match.group("value")

    for key in KEYS:
        if key not in raw:
            issues.append(GraphIssue("missing-key", "block", f"missing key `{key}`"))
    if issues:
        raise GraphError(issues)

    block = _build(raw, issues)
    if issues:
        raise GraphError(issues)
    return block


def _build(raw: dict[str, str], issues: list[GraphIssue]) -> MachineBlock:
    task_id = _slug(_read_scalar(raw["id"], "id", issues), "id", issues)
    plan = _slug(_read_scalar(raw["plan"], "plan", issues), "plan", issues)
    depends = tuple(
        _slug(item, "depends", issues) for item in _read_list(raw["depends"], "depends", issues)
    )
    requires = _read_list(raw["requires"], "requires", issues)
    unknown = [tag for tag in requires if tag not in CAPABILITIES]
    if unknown:
        issues.append(
            GraphIssue("unknown-capability", task_id or "block", f"unknown capability {unknown}")
        )
    return MachineBlock(
        id=TaskId(task_id),
        plan=plan,
        milestone=_read_scalar(raw["milestone"], "milestone", issues),
        lane=_read_enum(Lane, raw["lane"], "lane", issues),
        requires=requires,
        verify=_read_enum(Verify, raw["verify"], "verify", issues),
        spend=_read_bool(raw["spend"], issues),
        depends=tuple(TaskId(dep) for dep in depends),
        touches=_read_list(raw["touches"], "touches", issues),
    )


def _extract(body: str) -> list[str]:
    lines = body.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == OPEN]
    blocks: list[list[str]] = []
    for start in starts:
        for end in range(start + 1, len(lines)):
            if lines[end].strip() == CLOSE:
                blocks.append(lines[start + 1 : end])
                break
    if not blocks:
        raise GraphError(
            [GraphIssue("block-missing", "block", f"no terminated `{OPEN} ... {CLOSE}` block")]
        )
    if len(blocks) > 1:
        raise GraphError(
            [
                GraphIssue(
                    "block-duplicate",
                    "block",
                    f"{len(blocks)} dispatchkit blocks in one body; "
                    "refusing to guess which is real",
                )
            ]
        )
    return blocks[0]


def _scalar(value: str) -> str:
    if _PLAIN.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _list(values: Iterable[str]) -> str:
    return "[" + ", ".join(_scalar(value) for value in values) + "]"


def _read_scalar(raw: str, key: str, issues: list[GraphIssue]) -> str:
    if _PLAIN.match(raw):
        return raw
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return _unescape(raw[1:-1])
    issues.append(GraphIssue("block-syntax", "block", f"`{key}` is not a scalar: {raw!r}"))
    return ""


def _read_list(raw: str, key: str, issues: list[GraphIssue]) -> tuple[str, ...]:
    if not (raw.startswith("[") and raw.endswith("]")):
        issues.append(GraphIssue("block-syntax", "block", f"`{key}` is not a list: {raw!r}"))
        return ()
    inner = raw[1:-1].strip()
    if not inner:
        return ()
    return tuple(_read_scalar(item.strip(), key, issues) for item in _split_items(inner))


def _split_items(inner: str) -> list[str]:
    """Split a flow list on commas that are not inside a quoted string."""
    items: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in inner:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            current.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == "," and not quoted:
            items.append("".join(current))
            current = []
        else:
            current.append(char)
    items.append("".join(current))
    return items


def _unescape(value: str) -> str:
    out: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            out.append({"n": "\n", '"': '"', "\\": "\\"}.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def _slug(value: str, key: str, issues: list[GraphIssue]) -> str:
    if not SLUG.match(value):
        issues.append(GraphIssue("invalid-id", "block", f"`{key}` is not a slug: {value!r}"))
        return ""
    return value


def _read_bool(raw: str, issues: list[GraphIssue]) -> bool:
    if raw not in {"true", "false"}:
        issues.append(
            GraphIssue("invalid-type", "block", f"`spend` must be true/false, got {raw!r}")
        )
        return False
    return raw == "true"


def _read_enum[E: (Lane, Verify)](
    enum: type[E], raw: str, key: str, issues: list[GraphIssue]
) -> E:
    try:
        return enum(raw)
    except ValueError:
        allowed = ", ".join(member.value for member in enum)
        issues.append(
            GraphIssue("invalid-enum", "block", f"`{key}` must be one of {allowed}, got {raw!r}")
        )
        return next(iter(enum))
