from pydantic import BaseModel


class FlowRequest(BaseModel):
    repo_full_name: str
    question: str


class FlowResult(BaseModel):
    step_node_ids: list[str]
