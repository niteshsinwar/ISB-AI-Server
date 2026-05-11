from fastapi import APIRouter

# Import the factory functions and dependencies
from app.endpoints.application_endpoints import create_application_router
from app.endpoints.admin_endpoints import create_admin_router
from app.endpoints.eedl_endpoints import create_eedl_router
from app.services.salesforce_service import get_salesforce_service, get_default_dev_service

main_api_router = APIRouter()

# --- Create Router Instances using the Factories ---

# Admission: explicit org + default dev
multi_org_app_router = create_application_router(sf_service_dependency=get_salesforce_service)
multi_org_admin_router = create_admin_router(sf_service_dependency=get_salesforce_service)
default_app_router = create_application_router(sf_service_dependency=get_default_dev_service)
default_admin_router = create_admin_router(sf_service_dependency=get_default_dev_service)

# EEDL: explicit org + default dev
multi_org_eedl_router = create_eedl_router(sf_service_dependency=get_salesforce_service)
default_eedl_router = create_eedl_router(sf_service_dependency=get_default_dev_service)


# --- Include All Router Instances ---

# A. Application Analysis Routes (Admission)
main_api_router.include_router(multi_org_app_router, prefix="/{org_alias}/api/v1/application", tags=["Application Analysis"])
main_api_router.include_router(default_app_router, prefix="/api/v1/application", tags=["Application Analysis (Default to Dev)"])

# B. Admin Routes
main_api_router.include_router(multi_org_admin_router, prefix="/{org_alias}/api/v1/admin", tags=["Administration & Health"])
main_api_router.include_router(default_admin_router, prefix="/api/v1/admin", tags=["Administration & Health (Default to Dev)"])

# C. EEDL Routes
main_api_router.include_router(multi_org_eedl_router, prefix="/{org_alias}/api/v1/eedl", tags=["EEDL Analysis"])
main_api_router.include_router(default_eedl_router, prefix="/api/v1/eedl", tags=["EEDL Analysis (Default to Dev)"])


@main_api_router.get("/", tags=["Root"])
async def read_api_root():
    return {"message": "Welcome to the Multi-Org Document Analysis API"}