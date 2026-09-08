from fastapi import APIRouter

from .apk_api import router as apk_router
from .firmware_api import router as firmware_router
from .gsi_sn_burn import router as gsi_sn_router
from .shares_api import router as shares_router


router = APIRouter()
router.include_router(firmware_router)
router.include_router(gsi_sn_router)
router.include_router(apk_router)
router.include_router(shares_router)
