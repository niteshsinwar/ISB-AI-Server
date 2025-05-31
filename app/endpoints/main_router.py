# project_root/app/endpoints/main_router.py
from fastapi import APIRouter

from .application_endpoints import router as application_router
from .admin_endpoints import router as admin_router
from .legacy_endpoints import router as legacy_router # Assuming this will be populated

main_api_router = APIRouter()

# Include application-specific routes
main_api_router.include_router(application_router, prefix="/application", tags=["Application Analysis"])

# Include administrative routes
main_api_router.include_router(admin_router, prefix="/admin", tags=["Administration & Health"])

# Include legacy routes (if any, without a specific new prefix to maintain old paths if needed)
# Or add a prefix like "/legacy" if desired
main_api_router.include_router(legacy_router, tags=["Legacy"]) 

# Example: A root path for the main_api_router itself, if desired
@main_api_router.get("/", tags=["Root"])
async def read_api_root():
    return {"message": "Welcome to the Document Analysis API v1"}

# Note: The overall prefix like /api/v1 is applied when main_api_router is included in app/main.py
