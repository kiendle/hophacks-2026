# Semantic automation configuration v2

Vendored from the user-provided `semantic-automation-config-v2.zip`, under
`semantic-automation-config/twitter-preparation/`.

The schema, both example configurations, and four Python files retain the supplied contract.
Python imports in `configure_automation.py` and `jev_protocol.py` are package-relative so they
cannot accidentally load the application's older filter/protocol implementations.

`automation_tools.py` calls the supplied offline compiler for strict schema validation, unique
IDs, target/group references, DuckDB RE2 compilation, and native protocol compatibility. It never
contacts a model or creates an automation. Dependencies: DuckDB 1.5.5 and jsonschema >=4.23,<5. The app launchers pin the
compiler's supplied DuckDB version so prerelease resolution cannot change its SQL behavior.

The actual schema has six root fields. Its prose mentions seven; the machine-readable properties
and required list are authoritative. Sentiment is the immutable five-label choice contract.

The session stores the evolving review envelope in `automation-proposal.json`. After the user
confirms the current revision, `automation-final.json` contains only the exact v2 input object.
Execution settings and approval to run inference are not part of this file.
