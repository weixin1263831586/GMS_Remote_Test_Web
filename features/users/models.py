from pydantic import BaseModel, Field


class ClientInfoRequest(BaseModel):
    username: str | None = Field(default=None, max_length=64, pattern=r"^[\w.-]+$")
    password: str | None = None
    ip: str | None = None
