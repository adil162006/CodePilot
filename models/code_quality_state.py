from pydantic import BaseModel, Field
from typing import Annotated, Literal
from langgraph.graph.message import add_messages

from models.debug_state import FileManifest


class CodeQualityFinding(BaseModel):
    """Represents a single code quality issue and its suggested improvement."""

    file_name: str = Field(
        description="Name of the source file containing the issue."
    )

    line_number: int | None = Field(
        default=None,
        description="Approximate starting line of the issue, if identifiable."
    )

    category: Literal[
        "naming",
        "formatting",
        "comments",
        "readability",
        "complexity",
        "duplication",
    ] = Field(
        description="Category of the identified code quality issue."
    )

    severity: Literal["low", "medium", "high"] = Field(
        description="Impact of the issue on code readability and maintainability."
    )

    original_code: str = Field(
        description="Relevant snippet from the existing source code."
    )

    explanation: str = Field(
        description="Explanation of why the code could be improved."
    )


class CodeQualityAnalysisResponse(BaseModel):
    """Structured result returned for one source file or source chunk."""

    file_name: str = Field(
        description="Name of the source file being analyzed."
    )

    overall_assessment: str = Field(
        description="Brief quality assessment of the supplied source or chunk."
    )

    summary: str = Field(
        description="Concise summary of the quality review for the supplied source or chunk."
    )

    findings: list[CodeQualityFinding] = Field(
        default_factory=list,
        description="Specific actionable quality findings; empty if none are warranted.",
    )


class CodeQualityState(BaseModel):
    """Shared state for the Code Quality Agent workflow."""

    messages: Annotated[list, add_messages] = Field(
        default_factory=list,
        description="Messages exchanged during the workflow."
    )

    project_root: str = Field(
        description="Absolute path to the project working directory."
    )

    file_manifest: list[FileManifest] = Field(
        default_factory=list,
        description="Files discovered during project scanning."
    )

    findings: list[CodeQualityFinding] = Field(
        default_factory=list,
        description="Code quality issues and improvement suggestions."
    )

    # {file_name,line_number,category,severity,original_code,explanation}

    suggested_solutions: list[dict[str, str]] = Field(
        default_factory=list,
        description="Suggested solutions for the code quality issues."
    )

    analysis_errors: list[dict[str, str]] = Field(
        default_factory=list,
        description="Files or chunks that could not be analyzed, with error details."
    )
