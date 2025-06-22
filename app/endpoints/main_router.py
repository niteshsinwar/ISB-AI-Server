from fastapi import APIRouter

# Import the factory functions and dependencies
from app.endpoints.application_endpoints import create_application_router
from app.endpoints.admin_endpoints import create_admin_router
from app.services.salesforce_service import get_salesforce_service, get_default_dev_service
from .legacy_endpoints import router as legacy_router

main_api_router = APIRouter()

# --- Create Router Instances using the Factories ---

# Create routers for explicit orgs (e.g., /dev/..., /uat/...) by pairing the factory with the path-based dependency
multi_org_app_router = create_application_router(sf_service_dependency=get_salesforce_service)
multi_org_admin_router = create_admin_router(sf_service_dependency=get_salesforce_service)

# Create routers for the default "dev" org by pairing the factory with the default dependency
default_app_router = create_application_router(sf_service_dependency=get_default_dev_service)
default_admin_router = create_admin_router(sf_service_dependency=get_default_dev_service)


# --- Include All Router Instances with MODIFIED Prefixes ---

# A. Application Analysis Routes
# New Prefix: /{org_alias}/api/v1/application
main_api_router.include_router(multi_org_app_router, prefix="/{org_alias}/api/v1/application", tags=["Application Analysis"])
# New Prefix for default: /api/v1/application
main_api_router.include_router(default_app_router, prefix="/api/v1/application", tags=["Application Analysis (Default to Dev)"])

# B. Admin Routes
# New Prefix: /{org_alias}/api/v1/admin
main_api_router.include_router(multi_org_admin_router, prefix="/{org_alias}/api/v1/admin", tags=["Administration & Health"])
# New Prefix for default: /api/v1/admin
main_api_router.include_router(default_admin_router, prefix="/api/v1/admin", tags=["Administration & Health (Default to Dev)"])

# C. Legacy and Root Routes
main_api_router.include_router(legacy_router, tags=["Legacy"])

@main_api_router.get("/", tags=["Root"])
async def read_api_root():
    return {"message": "Welcome to the Multi-Org Document Analysis API"}