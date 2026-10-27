from pathlib import Path
from langchain_core.output_parsers import StrOutputParser
from langgraph.graph import START, StateGraph, END
from models.documentation_state import DocumentationState
from prompts.documentation_agent import DOCUMENTATION_AGENT_SYSTEM_PROMPT
from utls.debugger_utils import scan_code_files, read_code_file
from utls.pick_llm import pick_llm
from langchain.tools import tool


# ---------------------------------------------------------------------- TOOLS -------------------------------------------------------------------------------------------

@tool
def get_project_structure(project_dir: str):
    """
        Extract the file manifest from the project directory.

        Each manifest entry contains:
            path: str
            absolute_path: str
            extension: str
            language: str
            size: int
    """
    file_manifest = scan_code_files(project_dir)
    return file_manifest


@tool
def read_code_from_file(absolute_path: str) -> str:
    """
    Read and return the source code from a file using its absolute path.

    Args:
        absolute_path: The absolute path of the source code file to read.

    Returns:
        The file's source code as a string.
    """
    result = read_code_file(absolute_path)
    return result["code"]


@tool
def list_files(directory: str = ".", extension: str = "") -> list[str]:
    """
    List files in a project directory.

    Args:
        directory: Directory to inspect, relative to the project root
                   or an absolute directory path.
        extension: Optional file extension filter, such as '.py'
                   or '.md'.

    Returns:
        A list of relative file paths.
    """
    root = Path(directory).resolve()

    if not root.exists():
        return [f"Error: directory does not exist: {directory}"]

    if not root.is_dir():
        return [f"Error: path is not a directory: {directory}"]

    ignored_dirs = {
        ".git", ".venv", "venv", "__pycache__",
        "node_modules", "dist", "build"
    }

    files = []

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        # Ignore files inside generated or dependency directories.
        if ignored_dirs.intersection(path.relative_to(root).parts):
            continue

        if extension and path.suffix.lower() != extension.lower():
            continue

        files.append(str(path.relative_to(root)))

    return sorted(files)


@tool
def analyze_code(code: str) -> str:
    """
    Analyze source code to understand its purpose, structure,
    functions, classes, dependencies, and behavior.

    Args:
        code: Source code to analyze.

    Returns:
        A structured explanation of the supplied code.
    """
    llm = pick_llm("medium")

    prompt = ChatPromptTemplate.from_template(
        """
        You are a code analysis assistant for a software documentation agent.

        Analyze the following source code to help generate accurate documentation.

        Identify:
        1. Overall purpose and responsibilities.
        2. Important functions and their responsibilities.
        3. Classes and their relationships, if any.
        4. Imports and external dependencies.
        5. Important inputs, outputs, and side effects.
        6. How the main components interact.
        7. Important implementation details worth documenting.

        Rules:
        - Analyze only the supplied code.
        - Do not invent behavior or dependencies.
        - If something cannot be determined, explicitly say so.
        - Do not modify or rewrite the source code.
        - Return clear, structured Markdown.

        Source code:
        ```text
        {code}
        ```
        """
    )

    chain = prompt | llm | StrOutputParser()

    return chain.invoke({"code": code})


@tool
def search_code(query: str, project_root: str = ".", file_extension: str = "") -> str:
    """
    Search project source files for a text pattern or symbol.

    Args:
        query: Function name, class name, variable, or text to find.
        project_root: Root directory of the project to search.
        file_extension: Optional extension filter, such as '.py'.

    Returns:
        Matching file paths, line numbers, and matching lines.
    """
    root = Path(project_root).resolve()

    if not root.exists() or not root.is_dir():
        return f"Error: invalid project directory: {project_root}"

    ignored_dirs = {
        ".git", ".venv", "venv", "__pycache__",
        "node_modules", "dist", "build"
    }
    ignored_files = {
        ".env", ".env.local", "id_rsa", "id_ed25519"
    }

    matches = []
    max_matches = 50

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative_path = path.relative_to(root)

        if ignored_dirs.intersection(relative_path.parts):
            continue

        if path.name in ignored_files:
            continue

        if file_extension and path.suffix.lower() != file_extension.lower():
            continue

        try:
            with path.open("r", encoding="utf-8", errors="ignore") as file:
                for line_number, line in enumerate(file, start=1):
                    if query.casefold() in line.casefold():
                        matches.append(
                            f"{relative_path}:{line_number}: {line.rstrip()}"
                        )

                        if len(matches) >= max_matches:
                            return (
                                    "\n".join(matches)
                                    + f"\n\nSearch limited to {max_matches} matches."
                            )
        except OSError:
            continue

    if not matches:
        return f"No matches found for '{query}'."

    return "\n".join(matches)


# ----------------------------------------------------------------- Binding LLM with tools ----------------------------------------------------------------------------
tools = [get_project_structure, read_code_from_file, list_files, analyze_code, search_code]

llm = pick_llm("high")
llm_with_tools = llm.bind_tools(tools)

tool_by_name = {
    "get_project_structure": get_project_structure,
    "read_code_from_file": read_code_from_file,
    "list_files": list_files,
    "analyze_code": analyze_code,
    "search_code": search_code,
}

# ------------------------------------------------------------------------- AGENT GRAPH -------------------------------------------------------------------------------------

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, ToolMessage, HumanMessage


def llm_node(state: DocumentationState) -> DocumentationState:
    """
    Invoke the LLM with the current conversation history and available tools.

    The LLM decides whether to call a tool or provide a final response.
    Its response is appended to the shared documentation agent state.

    Args:
        state: Shared state containing the conversation messages and
               documentation workflow context.

    Returns:
        Updated state containing the LLM response.
    """
    messages = state.messages

    prompt = ChatPromptTemplate.from_messages([
        SystemMessage(content=DOCUMENTATION_AGENT_SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="messages"),
    ])

    chain = prompt | llm_with_tools

    final_answer = chain.invoke({
        "messages": messages
    })

    state.messages = state.messages + [final_answer]

    return state


def tool_node(state: DocumentationState) -> DocumentationState:
    """Execute all tool calls requested by the latest AI message."""

    tool_calls = state.messages[-1].tool_calls
    tool_results = []

    for tool_call in tool_calls:
        tool = tool_by_name[tool_call["name"]]

        observation = tool.invoke(tool_call["args"])

        tool_results.append(
            ToolMessage(
                content=str(observation),
                tool_call_id=tool_call["id"]
            )
        )

    state.messages = state.messages + tool_results
    return state


# router function

def is_tool_call(state: DocumentationState):
    tool_calls = state.messages[-1].tool_calls

    if tool_calls:
        return "tool_node"
    else:
        return "end"


documentation_graph = StateGraph(DocumentationState)

# nodes
documentation_graph.add_node("llm_node", llm_node)
documentation_graph.add_node("tool_node", tool_node)

# edges
documentation_graph.add_edge(START, "llm_node")
documentation_graph.add_conditional_edges("llm_node", is_tool_call, {
    "tool_node": "tool_node",
    "end": END
})
documentation_graph.add_edge("tool_node", "llm_node")

documentation_graph_agent = documentation_graph.compile()

# ------------------------------------------------------ FOR TESTING ---------------------------------------------------------------------------------------


if __name__ == "__main__":
    project_dir = "../data/debug"

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

    result = documentation_graph_agent.invoke(
        {
            "messages": [
                HumanMessage(content=user_request)
            ]
        },
        config={"recursion_limit": 30},
    )

    print("\n========== DOCUMENTATION AGENT OUTPUT ==========\n")

    final_message = result["messages"][-1]
    print(final_message.content)

    print("\n========== EXECUTION TRACE ==========\n")

    for message in result["messages"]:
        message_type = type(message).__name__
        print(f"\n[{message_type}]")

        if getattr(message, "tool_calls", None):
            for call in message.tool_calls:
                print(f"Tool: {call['name']}")
                print(f"Arguments: {call['args']}")

        if message_type == "ToolMessage":
            print(f"Tool result: {message.content[:500]}")

    print("\n========== TEST COMPLETE ==========")
