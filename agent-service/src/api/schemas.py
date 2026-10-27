"""Request/response schemas for HTTP API routes."""

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    user_id: str
    title: str = ""
    mode: str = "ask"
    # Empty = the client did not choose; the service's configured default
    # (agent.default_execution_style) applies.
    execution_style: str = ""
    scenario: str = "dev"


class UpdateSessionRequest(BaseModel):
    title: str


class SwitchModeRequest(BaseModel):
    mode: str


class SwitchExecutionStyleRequest(BaseModel):
    execution_style: str


class ChatMessageRequest(BaseModel):
    content: str
    workspace_path: str = ""
    mode: str = ""
    execution_style: str = ""
    model_id: str = ""
    environment: str = ""
    mentions: list[dict] = []


class ApprovalRequest(BaseModel):
    request_id: str
    approved: bool = True
    reason: str = ""
