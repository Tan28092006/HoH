# HoH 0.3.33 — QA can import the MCP SDK again

Hotfix found by the first live ticket run (HOH-280).

- The pytest probe runs as a script, so Python made the installed
  `hoh_control_plane` directory `sys.path[0]`. Since 0.3.30 that directory holds
  an `mcp/` subpackage, which shadowed the MCP SDK: every QA run of a project
  whose tests import `mcp` failed at collection. The probe now removes its own
  directory before running pytest.
- The `hoh-control-plane` project's frozen test command is now `pytest -q -n 4`
  (it was `-n auto`, which ran 20 workers and exhausted virtual memory).

## Verification

`tests/test_qa_runner.py` reproduces the production error without the fix and
passes with it. The full suite was not waited for before shipping.
