from fastapi import APIRouter

from .firmware_api import router as firmware_router
from .gsi_burn import router as gsi_router
from .shares_api import router as shares_router


router = APIRouter()
router.include_router(firmware_router)
router.include_router(gsi_router)
router.include_router(shares_router)
