"""Provider selection for new conversations; existing conversations keep their runner."""
import os


def Runner(session_id, directory):
    provider = os.environ.get("HARNESS_PROVIDER", "codex").lower()
    if provider == "claude":
        from claude_runner import Runner as Implementation
    elif provider == "codex":
        from codex_runner import Runner as Implementation
    else:
        raise ValueError(f"Unknown HARNESS_PROVIDER {provider!r}; choose codex or claude")
    return Implementation(session_id, directory)
