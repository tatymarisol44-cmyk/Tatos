"""Turn the failed tests of a JUnit report into GitHub annotations.

Annotations are readable by anyone who can see the repository, without signing in; the
raw job log is not. So a failure is diagnosable from the run page (and from the API) by
whoever has to fix it. Only the test id and the first lines of the failure are printed:
the test data is synthetic, and the report never contains secrets.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET  # our own pytest report, not untrusted input

MAX_LINES = 25


def escape(text: str) -> str:
    # GitHub workflow-command escaping for the message part.
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: str) -> int:
    failed = 0
    for case in ET.parse(path).getroot().iter("testcase"):  # noqa: S314 - our own report
        for problem in [*case.findall("failure"), *case.findall("error")]:
            failed += 1
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            body = (problem.get("message") or "") + "\n" + (problem.text or "")
            lines = [line for line in body.splitlines() if line.strip()]
            tail = "\n".join(lines[-MAX_LINES:])
            print(f"::error title={escape(name)}::{escape(tail)}")
    print(f"{failed} failed test(s) reported as annotations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml"))
