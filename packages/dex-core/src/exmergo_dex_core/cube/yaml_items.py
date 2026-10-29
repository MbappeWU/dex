"""Where one cube or view sits in a Cube model file, by its YAML marks.

Both surgical writers of Cube files read this: derivation's annotate, which
rewrites one cube's list item and leaves every other byte alone, and
`maintain reconcile`'s removal (:mod:`.removal`), which cuts list items out of
one cube or view. A file is found by ``yaml.compose`` marks plus the
indentation rule in :mod:`..yaml_spans`, never by re-serializing the file.

Only block style is located. A flow-style list of elements shares its lines
with neighbours, so nothing here can bound one entry in it, and ``None`` says so.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..yaml_spans import before_trailing_blanks, block_end

__all__ = ["element_node", "locate"]


def element_node(
    root: yaml.Node, name: str, section: str = "cubes"
) -> yaml.MappingNode | None:
    """The block-style mapping of the element ``name`` under ``section``."""

    if not isinstance(root, yaml.MappingNode):
        return None
    for key, value in root.value:
        if key.value != section or not isinstance(value, yaml.SequenceNode):
            continue
        if value.flow_style:
            return None
        for item in value.value:
            if not isinstance(item, yaml.MappingNode) or item.flow_style:
                continue
            names = [v.value for k, v in item.value if k.value == "name"]
            if names == [name]:
                return item
    return None


def locate(
    text: str, name: str, section: str = "cubes"
) -> tuple[int, int, int, dict[str, Any]] | None:
    """Where the element ``name``'s list item starts and ends, its dash column,
    and its parsed body; ``None`` where it is not a block-style item."""

    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None
    item = element_node(root, name, section)
    if item is None:
        return None
    line_start = text.rfind("\n", 0, item.start_mark.index) + 1
    dash = text.find("-", line_start, item.start_mark.index)
    if dash < 0 or text[line_start:dash].strip():
        return None
    indent = dash - line_start
    end = before_trailing_blanks(text, block_end(text, line_start, indent))
    # The item's own lines parse as a one-item list, which keeps its
    # indentation consistent where a slice from the first key would not.
    parsed = yaml.safe_load(text[line_start:end])
    if not isinstance(parsed, list) or not isinstance(parsed[0], dict):
        return None
    return line_start, end, indent, parsed[0]
