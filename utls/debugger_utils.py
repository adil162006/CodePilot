from pathlib import Path
from typing import Any

import tiktoken
from langchain_core.prompts import ChatPromptTemplate

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx",
    ".java", ".cpp", ".c", ".go"
}

IGNORED_DIRS = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", "dist", "build"
}

LANGUAGE_MAP = {
    ".py": "Python",
    ".js": "JavaScript",
    ".jsx": "JavaScript (React JSX)",
    ".ts": "TypeScript",
    ".tsx": "TypeScript (React TSX)",
    ".java": "Java",
    ".cpp": "C++",
    ".c": "C",
    ".go": "Go",
    ".html": "HTML",
    ".css": "CSS",
    ".sql": "SQL",
    ".json": "JSON",
    ".md": "Markdown",
    ".rs": "Rust",
    ".php": "PHP",
    ".rb": "Ruby",
}


def scan_code_files(project_root: str) -> list[dict]:
    root = Path(project_root).resolve()
    manifest = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        # Ignore generated and dependency directories
        if any(part in IGNORED_DIRS for part in path.parts):
            continue

        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue

        # Avoid reading very large files
        if path.stat().st_size > 500_000:
            continue

        manifest.append({
            "path": str(path.relative_to(root)),
            "absolute_path": str(path),
            "extension": path.suffix.lower(),
            "language": LANGUAGE_MAP.get(
                path.suffix.lower(), "Unknown"
            ),
            "size": path.stat().st_size
        })

    return manifest


def read_code_file(file_path: str) -> dict:
    path = Path(file_path).resolve()

    if not path.is_file():
        raise FileNotFoundError(file_path)

    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError("Unsupported source file type")

    if path.stat().st_size > 500_000:
        raise ValueError("File exceeds the size limit")

    code = path.read_text(encoding="utf-8", errors="replace")

    return {
        "path": str(path),
        "code": code
    }


encoding = tiktoken.get_encoding("cl100k_base")


def count_prompt_tokens(prompt: ChatPromptTemplate, inputs: dict) -> int:
    """Estimate tokens in the fully rendered prompt."""
    messages = prompt.invoke(inputs).to_messages()

    rendered = "\n".join(
        f"{message.type}: {message.content}"
        for message in messages
    )

    return len(encoding.encode(rendered))


def prepare_chunks(
        prompt: ChatPromptTemplate,
        inputs: dict,
        context_limit: int,
        output_reserve: int = 2000,
        safety_margin: int = 2000,
) -> list[dict]:
    """
    Keep the complete file if it fits.
    Otherwise, split it into chunks that fit the estimated budget.
    """

    allowed_input = context_limit - output_reserve - safety_margin

    if allowed_input <= 0:
        raise ValueError("Context budget is too small.")

    code = inputs["code"]

    if count_prompt_tokens(prompt, inputs) <= allowed_input:
        return [{
            **inputs,
            "start_line": 1,
            "end_line": len(code.splitlines()),
        }]

    lines = code.splitlines()
    chunks = []
    start = 0

    def split_oversized_line(line: str, line_number: int) -> list[dict]:
        """Split a single minified/very long line until each prompt fits."""
        def split_fragment(fragment: str) -> list[dict]:
            fragment_inputs = {
                **inputs,
                "code": fragment,
                "start_line": line_number,
                "end_line": line_number,
            }
            if count_prompt_tokens(prompt, fragment_inputs) <= allowed_input:
                return [fragment_inputs]

            if len(fragment) <= 1:
                raise ValueError(
                    f"Cannot fit source line {line_number} into the prompt budget."
                )

            midpoint = len(fragment) // 2
            return (
                split_fragment(fragment[:midpoint])
                + split_fragment(fragment[midpoint:])
            )

        return split_fragment(line)

    while start < len(lines):
        # Begin with the remaining lines and shrink until they fit.
        end = len(lines)
        split_single_line = False

        while end > start:
            chunk_inputs = {
                **inputs,
                "code": "\n".join(lines[start:end]),
                "start_line": start + 1,
                "end_line": end,
            }

            if count_prompt_tokens(prompt, chunk_inputs) <= allowed_input:
                break

            if end == start + 1:
                # A single minified line can itself exceed the model budget.
                # Split its text so the loop always makes progress, while the
                # original line number remains available to the analyzer.
                chunks.extend(split_oversized_line(lines[start], start + 1))
                start = end
                split_single_line = True
                break

            end = start + max(1, (end - start) // 2)

        if start >= len(lines):
            break

        # The single-line fallback above already appended its chunks and moved
        # start forward. Continue with the next original source line.
        if split_single_line:
            continue

        chunks.append(chunk_inputs)
        start = end

    return chunks


def is_context_error(exc: Exception) -> bool:
    """Return True only when the error appears to be context-length related."""

    message = str(exc).lower()

    context_error_indicators = [
        "context_length_exceeded",
        "maximum context length",
        "context window exceeded",
        "exceeds the model's context",
        "too many tokens",
        "request too large",
        "input is too long",
        "prompt is too long",
    ]

    return any(
        indicator in message
        for indicator in context_error_indicators
    )


def analyze_with_retry(
        chain,
        inputs: dict[str, Any],
        max_depth: int = 3,
) -> list:
    """
    Analyze a code chunk using the LLM.

    If a context-length error occurs:
    1. Split the code into two smaller chunks.
    2. Analyze each chunk recursively.
    3. Combine their findings.

    max_depth limits how many times each chunk can be split.
    """

    try:
        # Attempt to analyze the current chunk.
        result = chain.invoke(inputs)

        # BugFindResponse contains a list named "findings".
        return result.findings

    except Exception as exc:

        # Don't retry unrelated errors.
        if not is_context_error(exc):
            raise

        # Stop if the splitting limit has been reached.
        depth = inputs.get("_retry_depth", 0)

        if depth >= max_depth:
            raise RuntimeError(
                f"Context error persists after {depth} splits "
                f"for {inputs['file_name']}, "
                f"lines {inputs['start_line']}-{inputs['end_line']}."
            ) from exc

        code = inputs["code"]
        lines = code.splitlines()

        # We cannot split a single line any further by line.
        if len(lines) <= 1:
            raise RuntimeError(
                f"Cannot split this code chunk further: "
                f"{inputs['file_name']}, "
                f"lines {inputs['start_line']}-{inputs['end_line']}."
            ) from exc

        # Split the code approximately in half.
        midpoint = len(lines) // 2

        original_start = inputs["start_line"]

        left_inputs = {
            **inputs,
            "code": "\n".join(lines[:midpoint]),
            "start_line": original_start,
            "end_line": original_start + midpoint - 1,
            "_retry_depth": depth + 1,
        }

        right_inputs = {
            **inputs,
            "code": "\n".join(lines[midpoint:]),
            "start_line": original_start + midpoint,
            "end_line": inputs["end_line"],
            "_retry_depth": depth + 1,
        }

        # Analyze both smaller chunks and combine their findings.
        left_findings = analyze_with_retry(
            chain,
            left_inputs,
            max_depth,
        )

        right_findings = analyze_with_retry(
            chain,
            right_inputs,
            max_depth,
        )

        return left_findings + right_findings



import difflib


def generate_unified_diff(
    original_code: str,
    corrected_code: str,
    relative_file_path: str,
) -> str:
    """Generate a Git-compatible unified diff."""

    original_lines = original_code.splitlines(keepends=True)
    corrected_lines = corrected_code.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        corrected_lines,
        fromfile=f"a/{relative_file_path}",
        tofile=f"b/{relative_file_path}",
        n=3,
    )

    return "".join(diff)
