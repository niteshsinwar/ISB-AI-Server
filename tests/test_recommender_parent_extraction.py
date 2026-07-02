import logging
import asyncio
import os
import sys

logging.basicConfig(level=logging.INFO)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.processors.recommender_processor import process_single_recommender_detail

# Mock the Salesforce service
class MockSFService:
    class SF:
        def query(self, query: str):
            if "ISB_Recommender_Details__c" in query:
                return {"records": [{"Id": "123", "First_Name__c": "John", "Last_Name__c": "Doe", "Email__c": "a@a.com", "Mobile__c": "123", "Status__c": "Submitted"}]}
            elif "ISB_Recommender_Response__c" in query:
                return {"records": [{"Question__c": "Q", "Answer__c": "A"}]}
            elif "hed__Application__c" in query:
                return {"records": [{"hed__Applicant__c": "456", "hed__Applicant__r": {"FirstName": "Jane", "LastName": "Doe"}}]}
            return {"records": []}
            
    sf = SF()
    
    def get_record_detail_from_apex(self, app_id, obj_name):
        return {
            "recordData": {},
            "documentPayload": {
                "base64Data": "JVBERi0xLjcKCjEgMCBvYmogICUgZW50cnkgcG9pbnQKPDwKICAvVHlwZSAvQ2F0YWxvZwogIC9QYWdlcyAyIDAgUgo+PgplbmRvYmoKCjIgMCBvYmoKPDwKICAvVHlwZSAvUGFnZXMKICAvTWVkaWFCb3ggWyAwIDAgMjAwIDIwMCBdCiAgL0NvdW50IDEKICAvS2lkcyBbIDMgMCBSIF0KPj4KZW5kb2JqCgozIDAgb2JqCjw8CiAgL1R5cGUgL1BhZ2UKICAvUGFyZW50IDIgMCBSCiAgL1Jlc291cmNlcyA8PAogICAgL0ZvbnQgPDwKICAgICAgL0YxIDQgMCBSCisgICAgPj4KICA+PgogIC9Db250ZW50cyA1IDAgUgo+PgplbmRvYmoKCjQgMCBvYmoKPDwKICAvVHlwZSAvRm9udAogIC9TdWJ0eXBlIC9UeXBlMQogIC9CYXNlRm9udCAvVGltZXMtUm9tYW4KPj4KZW5kb2JqCgo1IDAgb2JqCjw8IC9MZW5ndGggMzkgPj4Kc3RyZWFtCkJUCi9GMSAxOCBUZgowIDUwIFRkCihGYXRoZXIncyBOYW1lOiBBYnJhaGFtIExpbmNvbG4pIFRqCkVUCmVuZHN0cmVhbQplbmRvYmoKCnhyZWYKMCA2CjAwMDAwMDAwMDAgNjUzMzUgZiAKMDAwMDAwMDAxMCAwMDAwMCBuIAowMDAwMDAwMDYwIDAwMDAwIG4gCjAwMDAwMDAxNTIgMDAwMDAgbiAKMDAwMDAwMDI4MiAwMDAwMCBuIAowMDAwMDAwMzcwIDAwMDAwIG4gCnRyYWlsZXIKPDwKICAvU2l6ZSA2CiAgL1Jvb3QgMSAwIFIKPj4Kc3RhcnR4cmVmCjQ2MwolJUVPRgo=",
                "fileExtension": "pdf"
            }
        }
        
    def upsert_verification_summary(self, **kwargs):
        print("Upserted summary:", kwargs)
        return "AVS_123"
        
    def get_existing_avs_metadata(self, **kwargs):
        return None

async def main():
    try:
        await process_single_recommender_detail(MockSFService(), "REC_123", "APP_123")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
