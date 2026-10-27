from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_config
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END

from models.code_pilot_state import CodePilotState
from models.debug_state import DebuggingState

from .debug_agent import debugger_agent
from .code_quality_agent import (
    code_quality_agent as quality_subgraph
)
from .documentation_agent import documentation_graph_agent


def debugger_agent_node(state: CodePilotState) -> CodePilotState:
    """
    Runs the debugger subgraph for the project specified in CodePilotState
    and returns its results to the parent CodePilot graph.
    """

    print("\n" + "=" * 80)
    print("DEBUGGER AGENT — EXECUTION STARTED")
    print("=" * 80)

    # 1. Validate the project directory.
    project_root = state.project_root

    if not project_root:
        raise ValueError("project_root must be provided.")

    # 2. Build the debugger's initial state.
    debugging_state = DebuggingState(
        project_root=project_root
    )

    # 3. Reuse the parent graph's runtime configuration.
    config = get_config()

    # 4. Invoke the existing debugger workflow.
    # Its nodes should perform manifest extraction, bug detection,
    # patch generation, and patch validation.
    result = debugger_agent.invoke(
        debugging_state,
        config=config,
    )

    # 5. Extract the debugger's output using the DebuggingState field names.
    bug_reports = result.get("bug_findings", [])
    proposed_patches = result.get("proposed_patches", [])
    validated_patches = result.get("validation_results", [])

    summary = (
        "Debugger workflow completed.\n"
        f"Project: {project_root}\n"
        f"Bug reports: {len(bug_reports)}\n"
        f"Proposed patches: {len(proposed_patches)}\n"
        f"Validated patches: {len(validated_patches)}"
    )

    state.messages = state.messages + [AIMessage(content=summary)]
    print(summary)

    # 6. Return updated state
    return state


def code_quality_agent_node(state: CodePilotState) -> CodePilotState:
    """
    Runs the code_quality subgraph for the project specified in
    CodePilotState and returns its results to the parent graph.
    """

    print("\n" + "=" * 80)
    print("CODE QUALITY AGENT — EXECUTION STARTED")
    print("=" * 80)

    # 1. Initialize the code quality subgraph state.
    quality_state = {
        "messages": [],
        "project_root": state.project_root,
        "file_manifest": [],
        "findings": [],
        "analysis_errors": [],
        "suggested_solutions": [],
    }

    # 2. Invoke the code quality subgraph.
    config = get_config()

    result = quality_subgraph.invoke(
        quality_state,
        config=config,
    )

    # 3. Extract the results from the completed workflow.
    findings = result.get("findings", [])
    analysis_errors = result.get("analysis_errors", [])
    suggested_solutions = result.get("suggested_solutions", [])

    # 4. Build a summary for the parent workflow.
    summary = (
        "Code quality analysis completed.\n"
        f"Project: {state.project_root}\n"
        f"Findings: {len(findings)}\n"
        f"Analysis errors: {len(analysis_errors)}\n"
        f"Suggested solutions: {len(suggested_solutions)}"
    )

    state.messages = state.messages + [AIMessage(content=summary)]
    print(summary)

    # 5. Return the update to the parent graph.
    return state


def documentation_agent_node(state: CodePilotState) -> CodePilotState:
    """
    Runs the documentation subgraph for the project specified in
    CodePilotState and returns the generated documentation to the
    parent CodePilot graph.
    """

    print("\n" + "=" * 80)
    print("DOCUMENTATION AGENT — EXECUTION STARTED")
    print("=" * 80)

    # 1. Prepare the documentation request.
    project_dir = state.project_root

    user_request = f"""
    Generate a README.md for the project located at: {project_dir}

    Follow these steps:
    1. Inspect the project structure.
    2. Identify the main modules and entry points.
    3. Read the relevant source files.
    4. Understand the responsibilities of the main components.
    5. Generate a README with the project overview, features,
       tech stack, architecture, setup instructions, and usage.

    Base the README on the actual code you inspect.
    Do not invent commands, dependencies, or features.
    Return the README content in Markdown.
    Do not claim to have saved a file.
    """

    # 2. Invoke the documentation subgraph.
    result = documentation_graph_agent.invoke(
        {
            "messages": [
                HumanMessage(content=user_request)
            ]
        },
        config=get_config(),
    )

    # 3. Extract the final response.
    messages = result.get("messages", [])

    if not messages:
        raise ValueError(
            "Documentation agent completed without returning messages."
        )

    final_message = messages[-1]
    readme_content = final_message.content

    state.messages = state.messages + [
        AIMessage(
            content=(
                "Documentation generation completed.\n"
                f"Project: {project_dir}\n\n"
                "Generated README content:\n\n"
                f"{readme_content}"
            )
        )
    ]
    return state


# -------------------------------- CODEPILOT GRAPH --------------------------------

code_pilot_builder = StateGraph(CodePilotState)

code_pilot_builder.add_node(
    "debugger_agent_node",
    debugger_agent_node,
)
code_pilot_builder.add_node(
    "code_quality_agent_node",
    code_quality_agent_node,
)
code_pilot_builder.add_node(
    "documentation_agent_node",
    documentation_agent_node,
)


def route_after_debugger(state: CodePilotState) -> str:
    if state.is_code_quality_agent:
        return "quality"

    if state.is_documentation_agent:
        return "documentation"

    return "end"


def route_after_quality(state: CodePilotState) -> str:
    if state.is_documentation_agent:
        return "documentation"

    return "end"


code_pilot_builder.add_edge(
    START,
    "debugger_agent_node",
)

code_pilot_builder.add_conditional_edges(
    "debugger_agent_node",
    route_after_debugger,
    {
        "quality": "code_quality_agent_node",
        "documentation": "documentation_agent_node",
        "end": END,
    },
)

code_pilot_builder.add_conditional_edges(
    "code_quality_agent_node",
    route_after_quality,
    {
        "documentation": "documentation_agent_node",
        "end": END,
    },
)

code_pilot_builder.add_edge(
    "documentation_agent_node",
    END,
)

code_pilot_graph = code_pilot_builder.compile(
    checkpointer=MemorySaver(),
)
