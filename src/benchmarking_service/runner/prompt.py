"""Prompt construction for a benchmark task (port of upstream `prompt.py`)."""


def build_prompt(task: str, session_id: str, context: dict | None = None) -> str:
    parts = [f"The task you are to complete is:\n{task}"]
    if context:
        parts.append("\nContext:")
        for key, value in context.items():
            parts.append(f"- {key}: {value}")
    return "\n".join(parts)
