from langchain_core.messages import AIMessage
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import StateGraph, START, END

from models.code_quality_state import (
    CodeQualityAnalysisResponse,
    CodeQualityFinding,
    CodeQualityState,
)
from models.debug_state import FileManifest
from prompts.code_quality_prompt import ANALYZE_CODE_QUALITY_PROMPT, SUGGEST_IMPROVEMENTS_PROMPT
from utls.pick_llm import pick_llm, get_context_window_size
from utls.debugger_utils import scan_code_files, read_code_file, prepare_chunks

import json
import random as random_module
import time

from groq import RateLimitError


# helper

def _invoke_with_rate_limit_retry(chain, payload, max_attempts=4):
    """Retry an LLM invocation when Groq's rate limit is exceeded."""

    for attempt in range(max_attempts):
        try:
            return chain.invoke(payload)

        except RateLimitError:
            if attempt == max_attempts - 1:
                raise

            wait_seconds = min(5 * (2 ** attempt), 30)
            wait_seconds += random_module.uniform(0, 1)

            print(
                f"Rate limit reached. "
                f"Retrying in {wait_seconds:.1f} seconds "
                f"(attempt {attempt + 2}/{max_attempts})..."
            )

            time.sleep(wait_seconds)


# ---------------------------------------------- Agent Functions ----------------------------------------------------

def extract_file_manifest(state: CodeQualityState) -> CodeQualityState:
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
    state.messages = state.messages + [AIMessage(content=f"Found {len(manifest)} files in the project.")]
    return state


def analyze_code_quality(state: CodeQualityState) -> CodeQualityState:
    """
        Analyze the source files in the project to identify code quality
        improvements related to naming conventions, formatting, readability,
        comments, documentation, and unnecessary complexity.

        This function only analyzes the code and produces recommendations.
        It must not modify source files or apply any changes.

        Args:
            state (CodeQualityState): Shared workflow state containing the
                project file manifest and fields required for storing
                code quality analysis results.

        Returns:
            CodeQualityState: Updated workflow state containing the
                structured code quality findings for further processing
                by the Code Quality Agent.
    """

    llm = pick_llm("medium")
    structured_llm = llm.with_structured_output(
        CodeQualityAnalysisResponse,
        method="json_mode",
    )
    context_limit = get_context_window_size(llm.model_name)
    prompt = ChatPromptTemplate.from_template(ANALYZE_CODE_QUALITY_PROMPT)
    chain = prompt | structured_llm

    findings: list[CodeQualityFinding] = []
    analysis_errors: list[dict[str, str]] = []
    file_summaries: list[str] = []

    for file in state.file_manifest:
        try:
            source_code = read_code_file(file.absolute_path)["code"]
            total_lines = len(source_code.splitlines())

            base_inputs = {
                "file_name": file.path,
                "absolute_path": file.absolute_path,
                "language": file.language,
                "code": source_code,
                "start_line": 1,
                "end_line": total_lines,
            }

            chunks = prepare_chunks(
                prompt=prompt,
                inputs=base_inputs,
                context_limit=context_limit,
                output_reserve=2000,
                safety_margin=2000,
            )

            chunk_summaries: list[str] = []
            chunk_assessments: list[str] = []

            for chunk in chunks:
                start_line = chunk["start_line"]
                end_line = chunk["end_line"]
                try:
                    result: CodeQualityAnalysisResponse = chain.invoke(chunk)
                    chunk_summaries.append(
                        f"Lines {start_line}-{end_line}: {result.summary.strip()}"
                    )
                    if result.overall_assessment.strip():
                        chunk_assessments.append(result.overall_assessment.strip())

                    for finding in result.findings:
                        # The manifest path is authoritative for this finding.
                        finding.file_name = file.path
                        findings.append(finding)

                except Exception as exc:
                    analysis_errors.append({
                        "file_path": file.path,
                        "start_line": str(start_line),
                        "end_line": str(end_line),
                        "error": str(exc),
                    })

            assessment = "; ".join(dict.fromkeys(chunk_assessments))
            summary = " ".join(chunk_summaries)
            file_summary = (
                f"{file.path}: {assessment or 'No overall assessment returned.'} "
                f"{summary}"
            ).strip()
            file_summaries.append(file_summary)

        except Exception as exc:
            analysis_errors.append({
                "file_path": file.path,
                "error": str(exc),
            })

    state.findings = findings
    state.analysis_errors = analysis_errors
    state.messages = state.messages + [
        AIMessage(
            content=(
                f"Code quality analysis finished. Found {len(findings)} findings "
                f"across {len(state.file_manifest)} files. "
                f"Analysis errors: {len(analysis_errors)}."
            )
        )
    ]
    if file_summaries:
        state.messages = state.messages + [
            AIMessage(content="\n".join(file_summaries))
        ]

    return state


def suggest_improvements(state: CodeQualityState) -> CodeQualityState:
    """
    Generate actionable improvement recommendations for identified
    code quality findings without modifying source files.

    Args:
        state (CodeQualityState): Shared workflow state containing the
            project file manifest and code quality findings.

    Returns:
        CodeQualityState: Updated state containing improvement
            recommendations and any errors encountered.
    """

    llm = pick_llm("high")
    prompt = ChatPromptTemplate.from_template(SUGGEST_IMPROVEMENTS_PROMPT)
    chain = prompt | llm | StrOutputParser()

    findings: list[CodeQualityFinding] = state.findings
    suggestions: list[dict[str, str]] = []
    analysis_errors: list[dict[str, str]] = []

    project_manifest = json.dumps(
        [file.model_dump() for file in state.file_manifest],
        indent=2
    )

    for finding in findings:
        payload = {
            "project_manifest": project_manifest,
            "code_quality_findings": finding.model_dump_json(indent=2)
        }
        try:
            response = _invoke_with_rate_limit_retry(chain, payload)

            suggestions.append({
                "file_name": finding.file_name,
                "line_number": (
                    str(finding.line_number)
                    if finding.line_number is not None
                    else "Unknown"
                ),
                "category": finding.category,
                "severity": finding.severity,
                "original_code": finding.original_code,
                "explanation": finding.explanation,
                "suggested_improvement": response.strip()
            })

        except Exception as exc:
            analysis_errors.append({
                "file_name": finding.file_name,
                "line_number": (
                    str(finding.line_number)
                    if finding.line_number is not None
                    else "Unknown"
                ),
                "error": str(exc)
            })

    state.suggested_solutions = suggestions
    state.analysis_errors.extend(analysis_errors)

    return state


#  -------------------------------------------------------------------- AGENT GRAPH ----------------------------------------------------------------------------------------


code_quality_agent_graph = StateGraph(CodeQualityState)

# nodes
code_quality_agent_graph.add_node("extract_file_manifest", extract_file_manifest)
code_quality_agent_graph.add_node("analyze_code_quality", analyze_code_quality)
code_quality_agent_graph.add_node("suggest_improvements", suggest_improvements)

# edges
code_quality_agent_graph.add_edge(START, "extract_file_manifest")
code_quality_agent_graph.add_edge("extract_file_manifest", "analyze_code_quality")
code_quality_agent_graph.add_edge("analyze_code_quality", "suggest_improvements")
code_quality_agent_graph.add_edge("suggest_improvements", END)

# graph
code_quality_agent = code_quality_agent_graph.compile()

if __name__ == "__main__":
    state = {
        "messages": [],
        "project_root": "../data/debug",
        "file_manifest": [],
        "findings": [],
        "analysis_errors": [],
        "suggested_solutions": [],
    }

    # Maintain a snapshot of the complete workflow state.
    current_state = state.copy()

    print("\n" + "=" * 80)
    print("CODE QUALITY AGENT — EXECUTION STARTED")
    print("=" * 80)

    for update in code_quality_agent.stream(
            state,
            stream_mode="updates",
    ):
        for node_name, node_output in update.items():

            # Convert Pydantic models into JSON-compatible dictionaries.
            if hasattr(node_output, "model_dump"):
                node_output = node_output.model_dump(mode="json")

            if isinstance(node_output, dict):
                current_state.update(node_output)

            print(f"\n{'=' * 80}")
            print(f"NODE COMPLETED: {node_name}")
            print("=" * 80)

            print(
                json.dumps(
                    current_state,
                    indent=2,
                    ensure_ascii=False,
                    default=lambda obj: (
                        obj.model_dump(mode="json")
                        if hasattr(obj, "model_dump")
                        else str(obj)
                    ),
                )
            )

    print("\n" + "=" * 80)
    print("CODE QUALITY AGENT — EXECUTION COMPLETED")
    print("=" * 80)
