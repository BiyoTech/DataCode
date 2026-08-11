"""Request/response schemas for HTTP API routes."""

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    user_id: str
    title: str = ""
    mode: str = "ask"
    execution_style: str = "agent"
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
