"""Static guardrails for autonomous policy decision contracts.

Runtime replay can only audit branches that a particular run encounters.
This small source check complements it by rejecting new event branches that
call ``choose_index`` without supplying comparable candidate scores.  It is
deliberately narrow: forced protocol transitions are not policy choices, and
all strategic event choices should use the same scored-candidate contract.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _literal_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def unscored_event_choice_calls(source_path):
    """Return event ``choose_index`` calls without candidate score evidence."""

    source_path = Path(source_path)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    event_function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "choose_event_action"
        ),
        None,
    )
    if event_function is None:
        return [{"line": None, "reason": "choose_event_action_missing"}]

    violations = []
    for node in ast.walk(event_function):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "choose_index"
        ):
            continue
        has_positional_scores = len(node.args) >= 3
        has_keyword_scores = any(
            keyword.arg == "candidate_scores" for keyword in node.keywords
        )
        if has_positional_scores or has_keyword_scores:
            continue
        violations.append({
            "line": int(node.lineno),
            "reason": (
                _literal_string(node.args[1])
                if len(node.args) >= 2
                else None
            ),
        })
    return sorted(violations, key=lambda row: row["line"] or -1)
