from pydantic import BaseModel, Field
from typing import Annotated
from langgraph.graph.message import add_messages


class CodePilotState(BaseModel):
    project_root: str = Field(
        description="Absolute path to the project working directory."
    )

    messages: Annotated[list, add_messages] = Field(
        default_factory=list,
        description="Messages exchanged during the debugging workflow."
    )

    is_code_quality_agent: bool = Field(
        description="Whether the code quality agent is enabled."
    )

    is_documentation_agent: bool = Field(
        description="Whether the documentation agent is enabled."
    )





