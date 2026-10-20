"""Runtime environment-reader discovery shared by builds and the live schema."""

from pathlib import Path
import re

# Literal getenv reads plus the runtime's generic read helpers. Discover helper
# names from their function bodies rather than assuming every ANYPS3_ string in
# a comment or diagnostic is supported.
DIRECT = re.compile(r'\bgetenv\(\s*["\'](ANYPS3_[A-Z0-9_]+)["\']')
FUNCTION = re.compile(r'\b([A-Za-z_]\w*)\s*\([^;{}\n]*\b(?:const\s+char\s*\*|char\s+const\s*\*)\s*(\w+)[^;{}\n]*\)\s*\{')


def environment_reads(source):
    text = "\n".join(path.read_text() for path in sorted(Path(source).glob("*.py")))
    found = set(DIRECT.findall(text))
    helpers = set()
    for match in FUNCTION.finditer(text):
        name, parameter = match.groups()
        # All existing generic readers call getenv before their first closing
        # brace. Bound this to a single function to avoid crossing declarations.
        body = text[match.end():text.find("}", match.end())]
        if re.search(r"\bgetenv\(\s*"+re.escape(parameter)+r"\s*\)", body):
            helpers.add(name)
    if helpers:
        pattern = r"\b(?:"+"|".join(map(re.escape, helpers))+r')\(\s*["\'](ANYPS3_[A-Z0-9_]+)["\']'
        found.update(re.findall(pattern, text))
    return sorted(found)
