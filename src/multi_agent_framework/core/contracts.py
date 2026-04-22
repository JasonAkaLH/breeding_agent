from typing import Any, Literal

from pydantic import BaseModel, Field

MessageRole = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    role: MessageRole
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionContext(BaseModel):
    request_id: str | None = None
    conversation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRequest(BaseModel):
    agent_name: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    context: ExecutionContext = Field(default_factory=ExecutionContext)


class AgentResponse(BaseModel):
    agent_name: str
    message: Message
    usage: dict[str, Any] = Field(default_factory=dict)
    trace: list[str] = Field(default_factory=list)


class AgentDescriptor(BaseModel):
    name: str
    description: str
