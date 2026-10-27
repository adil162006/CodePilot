from typing import Annotated

from pydantic import BaseModel, Field
from langgraph.graph.message import add_messages


class DocumentationState(BaseModel):
    messages: Annotated[list, add_messages] = Field(...,description="Messages exchanged during the debugging workflow.")
