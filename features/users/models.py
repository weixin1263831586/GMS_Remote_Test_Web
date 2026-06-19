from pydantic import BaseModel


class ClientInfoRequest(BaseModel):
    username: str | None = None
    password: str | None = None
    ip: str | None = None
