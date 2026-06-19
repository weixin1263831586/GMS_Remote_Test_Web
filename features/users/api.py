from fastapi import APIRouter

from .config_api import router as config_router
from .users_api import router as users_router


router = APIRouter()
router.include_router(users_router)
router.include_router(config_router)
