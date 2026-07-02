import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()

from app.config import SALESFORCE_ORGS
from app.services.salesforce_service import SalesforceService

async def find_records():
    org = SALESFORCE_ORGS['uat']
    sf = SalesforceService(org['client_id'], org['client_secret'], org['token_url'], 'uat')
    sf._ensure_connected()
    
    print("Querying recent EEDL Opportunities...")
    query = """
    SELECT Id, Name, ParentRecordId, DocumentTypeId 
    FROM DocumentChecklistItem 
    ORDER BY CreatedDate DESC LIMIT 1000
    """
    result = sf._call_sf_api_with_retry(lambda: sf.sf.query(query))
    
    apps = {}
    for r in result.get('records', []):
        app_id = r.get('ParentRecordId')
        if app_id and app_id.startswith('006'):
            if app_id not in apps:
                apps[app_id] = []
            apps[app_id].append(r.get('Name'))
            
    print("\nApplications with most documents:")
    for app_id, docs in sorted(apps.items(), key=lambda x: len(x[1]), reverse=True)[:5]:
        print(f"App ID: {app_id} | Docs ({len(docs)}): {docs[:5]}")

if __name__ == "__main__":
    asyncio.run(find_records())
