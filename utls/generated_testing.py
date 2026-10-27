import ast
import shutil
import tempfile
import subprocess
from pathlib import Path
from contextlib import contextmanager

from models.debug_state import ValidationSchema, ProposedPatch


@contextmanager
def create_isolated_project_copy(project_root: Path):
    """
    Create a temporary copy of the project for patch validation.
    The temporary directory is deleted automatically afterward.
    """
    with tempfile.TemporaryDirectory(prefix="debug-agent-") as temp_dir:
        isolated_path = Path(temp_dir) / project_root.name

        shutil.copytree(
            project_root,
            isolated_path,
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                "venv",
                "node_modules",
                "__pycache__",
                ".pytest_cache",
                "dist",
                "build",
                ".env",
            ),
        )

        yield isolated_path


def apply_patch_to_copy(
        test_root: Path,
        patch_content: str
) -> tuple[bool, str]:
    """
    Apply a unified diff to the isolated project copy.

    Returns:
        (True, message) if successful.
        (False, error_message) if unsuccessful.
    """

    result = subprocess.run(
        ["git", "apply", "--"],
        input=patch_content,
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=test_root,
        timeout=10,
    )

    if result.returncode != 0:
        return False, result.stderr.strip()

    return True, "Patch applied successfully."


def run_project_tests(test_root: Path) -> tuple[bool | None, list[str]]:
    """Run pytest if test files are available."""

    has_tests = (
            (test_root / "tests").is_dir()
            or any(test_root.glob("test_*.py"))
            or any(test_root.glob("*_test.py"))
    )

    if not has_tests:
        return None, ["No pytest test files found."]

    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "-q"],
            cwd=test_root,
            capture_output=True,
            encoding="utf-8",
            text=True,
            timeout=120,
        )

        passed = result.returncode == 0
        errors = []

        if not passed:
            errors.append(
                "Tests failed:\n"
                + result.stdout[-3000:]
                + result.stderr[-3000:]
            )

        return passed, errors

    except (subprocess.TimeoutExpired, OSError) as exc:
        return None, [f"Could not complete test execution: {exc}"]


def validate_single_patch(
        patch: ProposedPatch,
        project_root: Path,
        *,
        validate_syntax: bool = True,
) -> ValidationSchema:
    """Validate one patch's applicability, optionally checking syntax too."""

    errors: list[str] = []
    patch_valid = False
    syntax_valid = None

    try:
        with create_isolated_project_copy(project_root) as test_root:

            # Step 1: Check whether the patch can be applied.
            check = subprocess.run(
                ["git", "apply", "--check", "--"],
                input=patch.proposed_code_change,
                text=True,
                capture_output=True,
                encoding="utf-8",
                cwd=test_root,
                timeout=10,
            )

            if check.returncode != 0:
                errors.append(
                    f"Patch cannot be applied: {check.stderr.strip()}"
                )
                status = "failed"

            else:
                # Step 2: Apply the patch to the temporary copy.
                success, message = apply_patch_to_copy(
                    test_root=test_root,
                    patch_content=patch.proposed_code_change,
                )

                if not success:
                    errors.append(message)
                    status = "failed"

                else:
                    patch_valid = True

                    if not validate_syntax:
                        syntax_valid = None
                        status = "passed"
                        return ValidationSchema(
                            bug_identifier=patch.bug_identifier,
                            patch_valid=True,
                            syntax_valid=None,
                            validation_errors=[],
                            validation_status="passed",
                        )

                    # Step 3: Validate only the patched Python file. An
                    # unrelated syntax error elsewhere in the project must
                    # not invalidate an otherwise correct patch.
                    try:
                        target = (test_root / patch.target_file_path).resolve()
                        if not target.is_relative_to(test_root.resolve()):
                            raise ValueError("Patch target escapes the project root.")
                        if target.suffix.lower() == ".py":
                            if not target.is_file():
                                raise FileNotFoundError(target)
                            source = target.read_text(encoding="utf-8")
                            ast.parse(source, filename=str(target))

                        syntax_valid = True
                        status = "passed"

                    except (SyntaxError, UnicodeDecodeError) as exc:
                        syntax_valid = False
                        errors.append(f"Python syntax error: {exc}")
                        status = "failed"

    except Exception as exc:
        errors.append(f"Unexpected validation error: {exc}")
        status = "error"

    return ValidationSchema(
        bug_identifier=patch.bug_identifier,
        patch_valid=patch_valid,
        syntax_valid=syntax_valid,
        validation_errors=errors,
        validation_status=status,
    )


def validate_patch_set(
        patches: list[ProposedPatch],
        project_root: Path,
) -> list[ValidationSchema]:
    """Validate patches individually for applicability and together for syntax.

    A patch to one line should not fail merely because a separate proposed
    patch fixes a syntax error elsewhere in the same file. First check each
    patch against the original project, then apply all applicable patches to
    one isolated copy and parse each affected Python file.
    """

    results = [
        validate_single_patch(patch, project_root, validate_syntax=False)
        for patch in patches
    ]
    result_by_id = {result.bug_identifier: result for result in results}
    applicable_patches = [
        patch for patch in patches
        if result_by_id[patch.bug_identifier].patch_valid
    ]

    if not applicable_patches:
        return results

    applied_patches: list[ProposedPatch] = []
    with create_isolated_project_copy(project_root) as test_root:
        for patch in applicable_patches:
            check = subprocess.run(
                ["git", "apply", "--check", "--"],
                input=patch.proposed_code_change,
                text=True,
                capture_output=True,
                encoding="utf-8",
                cwd=test_root,
                timeout=10,
            )
            result = result_by_id[patch.bug_identifier]
            if check.returncode != 0:
                result.patch_valid = False
                result.syntax_valid = None
                result.validation_status = "failed"
                result.validation_errors.append(
                    "Patch conflicts with another proposed patch when applied "
                    f"together: {check.stderr.strip()}"
                )
                continue

            success, message = apply_patch_to_copy(
                test_root=test_root,
                patch_content=patch.proposed_code_change,
            )
            if not success:
                result.patch_valid = False
                result.syntax_valid = None
                result.validation_status = "failed"
                result.validation_errors.append(message)
                continue

            applied_patches.append(patch)

        syntax_by_target: dict[str, tuple[bool, str]] = {}
        for patch in applied_patches:
            relative_target = Path(patch.target_file_path)
            target = (test_root / relative_target).resolve()
            if not target.is_relative_to(test_root.resolve()):
                syntax_by_target[patch.target_file_path] = (
                    False,
                    "Patch target escapes the project root.",
                )
                continue
            if target.suffix.lower() != ".py":
                syntax_by_target[patch.target_file_path] = (True, "")
                continue
            try:
                ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
                syntax_by_target[patch.target_file_path] = (True, "")
            except (SyntaxError, UnicodeDecodeError, OSError) as exc:
                syntax_by_target[patch.target_file_path] = (
                    False,
                    f"Python syntax error after applying applicable proposed "
                    f"patches together: {exc}",
                )

    for patch in applied_patches:
        result = result_by_id[patch.bug_identifier]
        syntax_valid, syntax_error = syntax_by_target[patch.target_file_path]
        result.syntax_valid = syntax_valid
        if syntax_valid:
            result.validation_status = "passed"
        else:
            result.validation_status = "failed"
            if syntax_error not in result.validation_errors:
                result.validation_errors.append(syntax_error)

    return results
