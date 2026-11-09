"""Build the deliverable CloudFormation templates.

Replaces each "#INLINE <file>" line in templates/*.yaml with the content of
src/<file>, indented to the same column, and writes the result to dist/.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
OUTPUTS = {"central.yaml": "cdn-prewarm-central.yaml", "forwarder.yaml": "cdn-prewarm-forwarder.yaml"}
INLINE_RE = re.compile(r"^(?P<indent> *)#INLINE (?P<file>\S+)$")
TEMPLATE_BODY_LIMIT = 51200  # bytes accepted by CreateStack --template-body


def inline(line):
    match = INLINE_RE.match(line)
    if not match:
        return line
    indent = match["indent"]
    code = (ROOT / "src" / match["file"]).read_text().rstrip("\n").splitlines()
    return "\n".join(indent + text if text else "" for text in code)


def main():
    (ROOT / "dist").mkdir(exist_ok=True)
    for source, target in OUTPUTS.items():
        lines = (ROOT / "templates" / source).read_text().splitlines()
        body = "\n".join(inline(line) for line in lines) + "\n"
        out = ROOT / "dist" / target
        out.write_text(body)
        size = len(body.encode())
        note = "" if size <= TEMPLATE_BODY_LIMIT else " (over 51,200 bytes: upload through S3 or the console)"
        print(f"{out.relative_to(ROOT)}: {size} bytes{note}")


if __name__ == "__main__":
    main()
