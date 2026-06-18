from pydantic import BaseModel


class SNBurnRequest(BaseModel):
    devices: list[str]
    sn_code: str
