from pydantic import BaseModel


class AskRequest(BaseModel):
    repo_full_name: str
    question: str
    focused_node_id: str | None = None


class FlowRequest(BaseModel):
    repo_full_name: str
    question: str


class AskAnswer(BaseModel):
    answer: str
    #: owning modules, which is what the diagram renders
    highlighted_node_ids: list[str]
    #: the precise nodes, for drill-down
    references: list[str]


class FlowResult(BaseModel):
    step_node_ids: list[str]
