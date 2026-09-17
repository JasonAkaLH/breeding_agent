from __future__ import annotations


def split_sql_script(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    in_dollar_block = False
    for line in script.splitlines():
        current.append(line)
        stripped = line.strip()
        if stripped.upper().startswith("DO $$") or " AS $$" in stripped.upper():
            in_dollar_block = True
        if in_dollar_block and stripped == "$$;":
            statements.append("\n".join(current).strip())
            current = []
            in_dollar_block = False
        elif not in_dollar_block and stripped.endswith(";"):
            statements.append("\n".join(current).strip())
            current = []
    if current:
        statements.append("\n".join(current).strip())
    return statements
