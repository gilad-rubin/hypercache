from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _python_examples() -> list[object]:
    examples = []
    for document in DOCS:
        for index, source in enumerate(PYTHON_BLOCK.findall(document.read_text()), start=1):
            examples.append(pytest.param(document, source, id=f"{document.name}:{index}"))
    return examples


@pytest.mark.parametrize(("document", "source"), _python_examples())
def test_python_examples_compile(document: Path, source: str):
    compile(source, f"{document.name} python example", "exec")
