"""Regression test for `python -m cbdb_agent ...` actually working as a real
subprocess invocation - every doc (README, 01-implementation-plan.md,
03-extraction-review-workflow.md) documents this as the CLI entry point, but the
package had no `__main__.py`, so it failed with "No module named
cbdb_agent.__main__" until src/cbdb_agent/__main__.py was added. cli.main() being
importable and callable directly (as every other test in this suite exercises it)
does NOT catch this - only an actual `python -m cbdb_agent` subprocess call does,
because `-m` requires `__main__.py` to exist, a check unrelated to whether the rest
of the package imports fine.
"""

import json
import subprocess
import sys


def test_python_dash_m_cbdb_agent_runs_without_module_not_found(tmp_path):
    input_path = tmp_path / "input.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "id": "p1",
                    "resource": "basicinformation",
                    "operation": "create",
                    "person_id": 900001,
                    "changes": {"c_name_chn": "x"},
                }
            ]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "cbdb_agent", "validate", "--input", str(input_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "No module named" not in result.stderr
    assert "cannot be directly executed" not in result.stderr
    assert result.returncode == 0
    assert "no issues found" in result.stdout
