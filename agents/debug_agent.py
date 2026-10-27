import difflib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

# Support running this file directly as well as as a package module.
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command, interrupt
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from models.debug_state import DebuggingState, FileManifest, BugFindResponse, ProposedPatch, BugFinding, ValidationSchema

from prompts.debugger_prompt import BUG_FIND_PROMPT, BUG_FIXED_PROMPT, DEBUGGING_REPORT_PROMPT
from utls.generated_testing import (
    apply_patch_to_copy,
    validate_patch_set,
)
from utls.pick_llm import pick_llm, get_context_window_size
from utls.debugger_utils import scan_code_files, read_code_file, prepare_chunks, analyze_with_retry


# ------------------------------------------------------ DEBUG AGENT -----------------------------------------------------------------------------

def _build_simple_syntax_repair(source_code: str) -> tuple[int, str] | None:
    """Repair only Python's explicit `=`-versus-`==` syntax hint."""
    try:
        compile(source_code, "<debug-target>", "exec")
        return None
    except SyntaxError as exc:
        if (
            not exc.lineno
            or not exc.msg
            or "Maybe you meant '==' instead of '='" not in exc.msg
        ):
            return None

        lines = source_code.splitlines(keepends=True)
        if exc.lineno > len(lines):
            return None
        repaired_line, replacements = re.subn(
            r"(?<![=!<>])=(?!=)", "==", lines[exc.lineno - 1], count=1
        )
        if replacements != 1:
            return None

        lines[exc.lineno - 1] = repaired_line
        corrected_code = "".join(lines)
        try:
            compile(corrected_code, "<debug-target>", "exec")
        except SyntaxError:
            return None
        return exc.lineno, corrected_code


def _is_false_undefined_function_finding(finding, source_code: str) -> bool:
    """Drop NameError claims when the named function is plainly defined/imported."""
    description = finding.explanation.lower()
    if not any(term in description for term in ("not defined", "undefined", "nameerror")):
        return False

    match = re.search(r"function\s+[`'\"]?([A-Za-z_]\w*)", finding.explanation, re.IGNORECASE)
    if not match:
        return False
    name = re.escape(match.group(1))
    return bool(
        re.search(rf"^\s*(?:async\s+)?def\s+{name}\s*\(", source_code, re.MULTILINE)
        or re.search(
            rf"^\s*from\s+\S+\s+import\s+.*\b{name}\b",
            source_code,
            re.MULTILINE,
        )
        or re.search(rf"^\s*import\s+.*\b{name}\b", source_code, re.MULTILINE)
    )


def _invoke_with_rate_limit_retry(chain, inputs, max_attempts: int = 4):
    """Retry Groq 429 responses after the server-provided wait interval."""
    for attempt in range(max_attempts):
        try:
            return chain.invoke(inputs)
        except Exception as exc:
            message = str(exc)
            if "429" not in message and "rate_limit_exceeded" not in message:
                raise
            wait_match = re.search(
                r"try again in\s+([\d.]+)s", message, re.IGNORECASE
            )
            if not wait_match or attempt == max_attempts - 1:
                raise
            time.sleep(min(float(wait_match.group(1)) + 0.25, 30.0))

# This extract the important information by scanning the directory
def extract_file_manifest(state: DebuggingState) -> DebuggingState:
    """
    Extract the file manifest from the project directory.

    Each manifest entry contains:
        path: str
        absolute_path: str
        extension: str
        language: str
        size: int

    """
    project_dir: str = state.project_root
    manifest = scan_code_files(project_dir)

    state.file_manifest = [FileManifest(**file) for file in manifest]
    state.debug_status = "analyzing"
    state.messages = state.messages + [AIMessage(content=f"Found {len(manifest)} files in the project.")]
    return state


# This function runs the code first ,if the whole prompt is under the limit of the context window then the whole code file is send to llm ,and if not then it is being break into chunks
def find_bugs(state: DebuggingState) -> DebuggingState:
    """Analyze project files and return structured bug findings."""

    llm: ChatGroq = pick_llm("medium")
    # Groq's function-calling path can misinterpret the requested schema as a
    # tool (for example, trying to invoke a tool literally named "JSON").
    structured_llm = llm.with_structured_output(
        BugFindResponse, method="json_mode"
    )

    CONTEXT_LIMIT = get_context_window_size(llm.model_name)

    prompt = ChatPromptTemplate.from_template(BUG_FIND_PROMPT)
    chain = prompt | structured_llm

    bugs: list[dict] = []
    analysis_errors = []

    for file in state.file_manifest:
        code = ""
        syntax_repair = None
        try:
            code = read_code_file(file.absolute_path)['code']
            syntax_repair = _build_simple_syntax_repair(code)

            base_inputs = {
                "file_name": file.path,
                "absolute_path": file.absolute_path,
                "language": file.language,
                "code": code,
            }

            chunks = prepare_chunks(
                prompt=prompt,
                inputs=base_inputs,
                context_limit=CONTEXT_LIMIT,
                output_reserve=2000,
                safety_margin=2000,
            )

            for chunk in chunks:
                try:
                    findings = analyze_with_retry(
                        chain,
                        chunk,
                        max_depth=3,
                    )

                    for finding in findings:
                        if _is_false_undefined_function_finding(finding, code):
                            continue
                        if (
                            syntax_repair
                            and any(
                                marker in finding.explanation.lower()
                                for marker in ("syntaxerror", "syntax error")
                            )
                        ):
                            continue
                        bugs.append({
                            **finding.model_dump(),
                            "file_name": file.path,
                            "absolute_path": file.absolute_path,
                        })

                except Exception as exc:
                    analysis_errors.append({
                        "file_path": file.path,
                        "start_line": chunk["start_line"],
                        "end_line": chunk["end_line"],
                        "error": str(exc),
                    })

        except Exception as exc:
            analysis_errors.append({
                "file_path": file.path,
                "error": str(exc),
            })

        if syntax_repair:
            syntax_line, _ = syntax_repair
            bugs.append(BugFinding(
                file_name=file.path,
                absolute_path=file.absolute_path,
                line_number=syntax_line,
                severity="critical",
                explanation=(
                    "Python reports an assignment operator where an equality "
                    "comparison is expected; this prevents the file from compiling."
                ),
                suggested_fixes=["Replace the single '=' with '==' on this line."],
            ).model_dump())

    state.bug_findings = bugs
    state.debug_status = (
        "analyzing" if bugs
        else "error" if analysis_errors
        else "no_issues"
    )
    state.bugs_analysis_errors = analysis_errors
    state.messages = state.messages + [
        AIMessage(
            content=(
                f"Bug analysis finished. Found {len(bugs)} findings "
                f"across {len(state.file_manifest)} files. "
                f"Analysis errors: {len(analysis_errors)}."
            )
        )]

    return state


# This functions now generates the fixes for the bugs found in the previous node
def generate_proposed_patches(state: DebuggingState) -> DebuggingState:
    """
    Generate proposed code patches for each detected bug.

    Each bug is processed independently. Successful patches and
    individual generation failures are recorded in the state.
    """

    bugs = state.bug_findings

    llm = pick_llm("high", max_retries=1)
    # Use JSON mode consistently with find_bugs; this avoids accidental tool
    # calls and returns a schema-validated ProposedPatch.
    llm_with_structure = llm.with_structured_output(
        ProposedPatch, method="json_mode"
    )

    prompt = ChatPromptTemplate.from_template(BUG_FIXED_PROMPT)
    chain = prompt | llm_with_structure

    bugs_solutions: list[ProposedPatch] = []
    analysis_errors: list[dict[str, str]] = []

    for bug in bugs:
        bug_identifier = f"{bug.file_name}:{bug.line_number}"

        try:
            file_path = Path(bug.absolute_path)

            # Step 1: Read the original file without modifying it.
            original_code = file_path.read_text(encoding="utf-8")

            # Use Python's parser for its exact, actionable equality hint.
            # This local patch is independent of model rate limits.
            syntax_repair = _build_simple_syntax_repair(original_code)
            if syntax_repair and bug.line_number == syntax_repair[0]:
                syntax_line, corrected_code = syntax_repair
                diff = difflib.unified_diff(
                    original_code.splitlines(keepends=True),
                    corrected_code.splitlines(keepends=True),
                    fromfile=f"a/{Path(bug.file_name).as_posix()}",
                    tofile=f"b/{Path(bug.file_name).as_posix()}",
                    n=3,
                )
                bugs_solutions.append(ProposedPatch(
                    bug_identifier=f"{bug.file_name}:{syntax_line}",
                    target_file_path=bug.file_name,
                    bug_summary="Fix parser-confirmed assignment/comparison syntax error.",
                    root_cause="A single '=' in the conditional is invalid Python syntax.",
                    proposed_code_change="".join(diff),
                    corrected_file_content=corrected_code,
                    explanation=(
                        "Python's parser explicitly suggested '==' instead of '='; "
                        "the corrected file compiles."
                    ),
                    assumptions=[],
                    risk=[],
                    generated=True,
                    failure="",
                ))
                continue

            # Step 2: Ask the LLM for the complete corrected file.
            bug_result = _invoke_with_rate_limit_retry(chain, {
                "bug_details": bug.model_dump(),
                "source_code": original_code,
            })

            # Attach metadata from the actual bug finding.
            bug_result.bug_identifier = bug_identifier
            bug_result.target_file_path = bug.file_name

            # Step 3: Check whether the LLM generated a correction.
            if not bug_result.generated:
                if not bug_result.failure:
                    bug_result.failure = (
                        "The LLM could not generate a reliable correction."
                    )

                bug_result.proposed_code_change = ""
                bugs_solutions.append(bug_result)
                continue

            corrected_code = bug_result.corrected_file_content

            if not corrected_code.strip():
                bug_result.generated = False
                bug_result.failure = (
                    "The LLM returned empty corrected file content."
                )
                bug_result.proposed_code_change = ""
                bugs_solutions.append(bug_result)
                continue

            # LLMs often omit leading blank lines or normalize the final
            # newline when returning a whole file. Preserve those exact file
            # boundaries from the original so they do not produce unrelated
            # or malformed diff hunks.
            original_leading_newlines = len(original_code) - len(original_code.lstrip("\n"))
            corrected_code = corrected_code.lstrip("\n")
            corrected_code = "\n" * original_leading_newlines + corrected_code
            if original_code.endswith("\n"):
                corrected_code = corrected_code.rstrip("\n") + "\n"
            else:
                corrected_code = corrected_code.rstrip("\n")
            bug_result.corrected_file_content = corrected_code

            # Step 4: Generate the unified Git diff using Python.
            relative_path = Path(bug.file_name).as_posix()

            diff = difflib.unified_diff(
                original_code.splitlines(keepends=True),
                corrected_code.splitlines(keepends=True),
                fromfile=f"a/{relative_path}",
                tofile=f"b/{relative_path}",
                n=3,
            )

            proposed_diff = "".join(diff)

            # Step 5: Reject empty diffs.
            if not proposed_diff:
                bug_result.generated = False
                bug_result.failure = (
                    "The corrected file is identical to the original. "
                    "No changes were generated."
                )
                bug_result.proposed_code_change = ""
                bugs_solutions.append(bug_result)
                continue

            # Step 6: Store the generated diff for later validation.
            bug_result.proposed_code_change = proposed_diff
            bug_result.generated = True
            bug_result.failure = ""

            bugs_solutions.append(bug_result)

        except Exception as exc:
            analysis_errors.append({
                "bug_identifier": bug_identifier,
                "error": str(exc),
            })

    state.proposed_patches = bugs_solutions
    state.proposed_patches_analysis_errors = analysis_errors
    state.debug_status = "patch_generation_completed"

    return state


def check_generated_patches(state: DebuggingState) -> DebuggingState:
    """
    Validate generated patches and record their results.
    Original project files remain unchanged.
    """

    proper_patches = [
        patch for patch in state.proposed_patches
        if patch.generated
    ]

    project_root = Path(state.project_root).resolve()

    validation_results = validate_patch_set(proper_patches, project_root)
    validation_errors: list[dict[str, str]] = []

    for patch, result in zip(proper_patches, validation_results):
        if result.validation_status == "error":
            validation_errors.append({
                "bug_identifier": patch.bug_identifier,
                "error": "; ".join(result.validation_errors),
            })

    state.validation_results = validation_results
    state.validation_errors = validation_errors
    state.debug_status = (
        "patch_validation_completed"
        if any(result.validation_status == "passed" for result in validation_results)
        else "error" if validation_errors
        else "unresolved"
    )

    passed = sum(
        result.validation_status == "passed"
        for result in validation_results
    )

    state.messages = state.messages + [
        AIMessage(
            content=(
                f"Patch validation completed (individual applicability, then "
                f"combined syntax check). "
                f"Generated patches: {len(proper_patches)}. "
                f"Passed: {passed}. "
                f"Failed or incomplete: "
                f"{len(validation_results) - passed}. "
                f"Unexpected errors: {len(validation_errors)}."
            )
        )
    ]

    return state


def apply_approved_patches(state: DebuggingState) -> DebuggingState:
    """Apply approved, validated patches to the actual project."""

    project_root = Path(state.project_root).resolve()
    state.debug_status = "applying"

    approved_patches = [
        patch
        for patch in state.proposed_patches
        if patch.generated and patch.approved
    ]

    results: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    if not approved_patches:
        state.debug_status = "unresolved"

    else:
        # Revalidate exactly what was approved. Syntax validity can depend on
        # a separate syntax-repair patch from the same file, so the approved
        # subset must compile together before anything touches the project.
        approved_validations = validate_patch_set(approved_patches, project_root)
        validation_by_id = {
            result.bug_identifier: result
            for result in approved_validations
        }
        for index, result in enumerate(state.validation_results):
            replacement = validation_by_id.get(result.bug_identifier)
            if replacement is not None:
                state.validation_results[index] = replacement

        failed_approvals = [
            result for result in approved_validations
            if result.validation_status != "passed"
        ]

        if failed_approvals:
            for patch in approved_patches:
                result = validation_by_id[patch.bug_identifier]
                message = (
                    "Approved patch set did not pass revalidation: "
                    + "; ".join(result.validation_errors)
                    if result.validation_status != "passed"
                    else "Another approved patch for this file failed combined validation."
                )
                results.append({
                    "bug_identifier": patch.bug_identifier,
                    "status": "skipped",
                    "message": message,
                })
            errors.extend({
                "bug_identifier": result.bug_identifier,
                "error": "; ".join(result.validation_errors),
            } for result in failed_approvals)
            state.debug_status = "unresolved"

        else:
            try:
                if not project_root.is_dir():
                    raise FileNotFoundError(
                        f"Project directory not found: {project_root}"
                    )

                # Apply the approved set in one git operation so a later
                # conflict cannot leave only part of a multi-patch fix applied.
                combined_patch = "".join(
                    patch.proposed_code_change
                    for patch in approved_patches
                )
                check = subprocess.run(
                    ["git", "apply", "--check", "--"],
                    input=combined_patch,
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    cwd=project_root,
                    timeout=10,
                )

                if check.returncode != 0:
                    message = (
                        "Approved patch set no longer applies to the project: "
                        + check.stderr.strip()
                    )
                    for patch in approved_patches:
                        results.append({
                            "bug_identifier": patch.bug_identifier,
                            "status": "failed",
                            "message": message,
                        })
                    errors.append({"bug_identifier": "approved_patch_set", "error": message})
                    state.debug_status = "unresolved"
                else:
                    success, message = apply_patch_to_copy(
                        test_root=project_root,
                        patch_content=combined_patch,
                    )
                    for patch in approved_patches:
                        results.append({
                            "bug_identifier": patch.bug_identifier,
                            "status": "applied" if success else "failed",
                            "message": message,
                        })
                    if success:
                        state.debug_status = "passed"
                    else:
                        errors.append({"bug_identifier": "approved_patch_set", "error": message})
                        state.debug_status = "unresolved"

            except Exception as exc:
                message = str(exc)
                errors.append({"bug_identifier": "approved_patch_set", "error": message})
                results.extend({
                    "bug_identifier": patch.bug_identifier,
                    "status": "failed",
                    "message": message,
                } for patch in approved_patches)
                state.debug_status = "unresolved"

    # Step 5: Store application results.
    state.test_results["patch_application"] = results
    state.test_results["patch_application_errors"] = errors

    state.test_results.setdefault("iteration_history", []).append({
        "bugs": [bug.model_dump() for bug in state.bug_findings],
        "patches": [patch.model_dump() for patch in state.proposed_patches],
        "validations": [result.model_dump() for result in state.validation_results],
        "application_results": results,
    })
    state.iteration_after_apply_patch += 1
    state.debug_attempts += 1

    applied_count = sum(
        result["status"] == "applied"
        for result in results
    )

    failed_count = sum(
        result["status"] == "failed"
        for result in results
    )

    if applied_count != len(approved_patches) or failed_count > 0:
        state.debug_status = "unresolved"

    state.messages = state.messages + [
        AIMessage(
            content=(
                f"Patch application completed. "
                f"Approved patches: {len(approved_patches)}. "
                f"Applied: {applied_count}. "
                f"Failed: {failed_count}. "
                f"Errors: {len(errors)}."
            )
        )
    ]

    return state


def generate_report(state: DebuggingState) -> DebuggingState:
    "Read the state and generate a report."

    llm: ChatGroq = pick_llm("medium")
    prompt = ChatPromptTemplate.from_template(DEBUGGING_REPORT_PROMPT)

    chain = prompt | llm | StrOutputParser()

    report_inputs = {
        "project_root": state.project_root,
        "debug_status": state.debug_status,
        "bug_findings": [bug.model_dump() for bug in state.bug_findings],
        "bugs_analysis_errors": state.bugs_analysis_errors,
        "proposed_patches": [patch.model_dump() for patch in state.proposed_patches],
        "proposed_patches_analysis_errors": state.proposed_patches_analysis_errors,
        "validation_results": [result.model_dump() for result in state.validation_results],
        "validation_errors": state.validation_errors,
        "patch_application_results": state.test_results.get("patch_application", []),
        "iteration_history": state.test_results.get("iteration_history", []),
    }
    try:
        response = chain.invoke(report_inputs)
    except Exception as exc:
        # Reporting must not discard an otherwise completed workflow if the
        # report model is unavailable. Preserve all key outcomes locally.
        finding_lines = [
            f"- `{bug.file_name}:{bug.line_number}` ({bug.severity}): {bug.explanation}"
            for bug in state.bug_findings
        ] or ["- No findings in the final scan."]
        patch_lines = [
            f"- `{patch.bug_identifier}`: generated={patch.generated}, "
            f"approved={patch.approved}; {patch.bug_summary}"
            for patch in state.proposed_patches
        ] or ["- No patch proposals in the final scan."]
        validation_lines = [
            f"- `{result.bug_identifier}`: {result.validation_status}; "
            f"{' '.join(result.validation_errors)}"
            for result in state.validation_results
        ] or ["- No validation results in the final scan."]
        response = (
            f"# Debugging Report\n\n"
            f"- Project: `{state.project_root}`\n"
            f"- Status: **{state.debug_status}**\n"
            f"\n## Findings\n" + "\n".join(finding_lines) +
            f"\n\n## Patches\n" + "\n".join(patch_lines) +
            f"\n\n## Validation\n" + "\n".join(validation_lines) +
            f"\n\n## Patch applications\n"
            f"`{state.test_results.get('patch_application', [])}`\n\n"
            f"## Report model error\n{exc}"
        )

    state.debugging_report = response

    state.messages = state.messages + [
        AIMessage(content="Final debugging report generated.")
    ]

    return state


def human_approval(state: DebuggingState) -> DebuggingState:
    """
        Pause execution and ask a human to approve proposed patches.
    """
    patches_for_review = [
        {
            "bug_identifier": patch.bug_identifier,
            "target_file_path": patch.target_file_path,
            "bug_summary": patch.bug_summary,
            "proposed_code_change": patch.proposed_code_change,
            "explanation": patch.explanation,
            "risk": patch.risk,
        }
        for patch in state.proposed_patches
        if patch.generated and any(
            result.bug_identifier == patch.bug_identifier
            and result.validation_status == "passed"
            for result in state.validation_results
        )
    ]

    response = interrupt({
        "message": "Review the proposed patches.",
        "patches": patches_for_review,
        "note": (
            "Patch syntax was checked on the combined proposal set. For a file "
            "with a parser-confirmed syntax repair, approve that repair together "
            "with other patches for the same file. The approved subset is "
            "revalidated before application."
        ),
        "instructions": (
            "Resume with a dictionary containing "
            "'approved_bug_identifiers': a list of bug identifiers to approve."
        ),
    })
    if isinstance(response, dict):
        requested_ids = response.get("approved_bug_identifiers", [])
    elif isinstance(response, str):
        requested_ids = response.split(",")
    elif isinstance(response, (list, tuple, set)):
        requested_ids = response
    else:
        requested_ids = []
    if isinstance(requested_ids, str):
        requested_ids = requested_ids.split(",")
    elif not isinstance(requested_ids, (list, tuple, set)):
        requested_ids = []

    valid_ids = {
        result.bug_identifier
        for result in state.validation_results
        if result.validation_status == "passed"
    }
    reviewable_ids = {
        patch.bug_identifier
        for patch in state.proposed_patches
        if patch.generated and patch.bug_identifier in valid_ids
    }
    approved_ids = {
        str(identifier).strip()
        for identifier in requested_ids
        if str(identifier).strip() in reviewable_ids
    }

    # Return a fresh state with fresh patch models. Mutating nested Pydantic
    # objects in place was not reliably persisted through the graph checkpoint
    # on resume, so the router could see every patch as still unapproved.
    approved_patches = [
        patch.model_copy(update={
            "approved": patch.generated and patch.bug_identifier in approved_ids
        })
        for patch in state.proposed_patches
    ]
    approval_message = AIMessage(
        content=(
            f"Approval received for {len(approved_ids)} of "
            f"{len(reviewable_ids)} reviewable patches."
        )
    )
    return state.model_copy(update={
        "proposed_patches": approved_patches,
        "debug_status": "patch_validation_completed" if approved_ids else "unresolved",
        "messages": state.messages + [approval_message],
    })


# ------------------------------------------------------------- AGENT GRAPH  CODE----------------------------------------------------------------------

# graph
debugger_agent_graph = StateGraph(DebuggingState)

# nodes
debugger_agent_graph.add_node("extract_file_manifest", extract_file_manifest)
debugger_agent_graph.add_node("find_bugs", find_bugs)
debugger_agent_graph.add_node("generate_proposed_patches", generate_proposed_patches)
debugger_agent_graph.add_node("check_generated_patches", check_generated_patches)
debugger_agent_graph.add_node("apply_approved_patches", apply_approved_patches)
debugger_agent_graph.add_node("generate_report", generate_report)
debugger_agent_graph.add_node("human_approval", human_approval)


# router functions
def route_after_bug_detection(state: DebuggingState) -> Literal["generate_proposed_patches", "generate_report"]:
    # bugs were found : continue to patch generations
    if state.bug_findings:
        return "generate_proposed_patches"

    if state.bugs_analysis_errors:
        return "generate_report"

    return "generate_report"


def router_after_valid_patches_generation(
        state: DebuggingState,
) -> Literal["human_approval", "generate_report"]:
    # Continue to review if at least one patch is independently valid. Other
    # failures remain visible in the report and do not block a safe patch.
    if not state.proposed_patches or not state.validation_results:
        return "generate_report"

    if any(result.validation_status == "passed" for result in state.validation_results):
        return "human_approval"

    return "generate_report"


def route_iterate(state: DebuggingState) -> Literal["extract_file_manifest", "generate_report"]:
    # The apply node persists the iteration counter. Permit one reanalysis;
    # subsequent approved apply cycles finish with a report.
    return "generate_report" if state.iteration_after_apply_patch >= 2 else "extract_file_manifest"


def route_after_human_approval(state: DebuggingState) -> Literal["apply_approved_patches", "generate_report"]:
    if any(
        patch.generated and patch.approved
        for patch in state.proposed_patches
    ):
        return "apply_approved_patches"

    return "generate_report"


# edges

debugger_agent_graph.add_edge(START, "extract_file_manifest")
debugger_agent_graph.add_edge("extract_file_manifest", "find_bugs")

debugger_agent_graph.add_conditional_edges(
    "find_bugs",
    route_after_bug_detection
)

debugger_agent_graph.add_edge("generate_proposed_patches", "check_generated_patches")

debugger_agent_graph.add_conditional_edges(
    "check_generated_patches",
    router_after_valid_patches_generation
)

debugger_agent_graph.add_conditional_edges(
    "human_approval",
    route_after_human_approval
)

debugger_agent_graph.add_conditional_edges(
    "apply_approved_patches",
    route_iterate
)

debugger_agent_graph.add_edge("generate_report", END)

# ------------------------------------------------------------- GRAPH -------------------------------------------------------------
checkpointer = MemorySaver(
    serde=JsonPlusSerializer(
        allowed_msgpack_modules=[
            FileManifest,
            BugFinding,
            ProposedPatch,
            ValidationSchema,
        ]
    )
)
debugger_agent = debugger_agent_graph.compile(checkpointer=checkpointer)

if __name__ == "__main__":
    import sys
    import json
    from pprint import pformat
    from pathlib import Path

    # Fix Windows terminal output
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    project_path = str((Path(__file__).resolve().parents[1] / "data" / "debug").resolve())

    initial_state = DebuggingState(
        project_root=project_path
    )

    config = {
        "configurable": {
            "thread_id": "debug-run-1"
        }
    }

    print("\n" + "=" * 80)
    print("CODEPILOT AI - DEBUGGER AGENT")
    print("=" * 80)

    pending_input = initial_state
    while True:
        interrupted = False
        for event in debugger_agent.stream(
                pending_input,
                config=config,
                stream_mode="updates",
        ):
            interrupt_items = event.get("__interrupt__", ())
            if interrupt_items:
                request = interrupt_items[0].value
                print("\nPatch review required:")
                if request.get("note"):
                    print(request["note"])
                for patch in request.get("patches", []):
                    print(f"- {patch['bug_identifier']}: {patch['bug_summary']}")
                answer = input(
                    "Enter approved bug identifiers separated by commas "
                    "(or press Enter to reject all): "
                )
                pending_input = Command(resume={
                    "approved_bug_identifiers": [
                        item.strip() for item in answer.split(",") if item.strip()
                    ]
                })
                interrupted = True
                break

            for node_name, state_update in event.items():
                print(f"\n{'=' * 80}")
                print(f"NODE: {node_name}")
                print("=" * 80)

                # Convert the complete node update into readable JSON.
                if hasattr(state_update, "model_dump"):
                    state_update = state_update.model_dump(mode="json")

                elif isinstance(state_update, dict):
                    state_update = {
                        key: (
                            value.model_dump(mode="json")
                            if hasattr(value, "model_dump")
                            else value
                        )
                        for key, value in state_update.items()
                    }

                print(json.dumps(
                    state_update,
                    indent=4,
                    ensure_ascii=False,
                    default=lambda obj: (
                        obj.model_dump(mode="json")
                        if hasattr(obj, "model_dump")
                        else str(obj)
                    ),
                ))
        if not interrupted:
            break

    print("\n" + "=" * 80)
    print("GRAPH EXECUTION FINISHED")
    print("=" * 80)

    final_state = debugger_agent.get_state(config)

    print("\nFINAL COMPLETE STATE")
    print(json.dumps(
        final_state.values,
        indent=4,
        ensure_ascii=False,
        default=lambda obj: (
            obj.model_dump(mode="json")
            if hasattr(obj, "model_dump")
            else str(obj)
        ),
    ))
