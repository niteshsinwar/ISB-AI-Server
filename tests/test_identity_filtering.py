import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.langgraph.application_graph import ApplicationGraphOrchestrator

async def test_identity_filtering():
    record_data = {
        "Applicant Name": "John Doe",
        "Adhaar": "XXXX-XXXX-1234",
        "Passport": "A1234567",
        "Gender": "M"
    }
    document_text = """
    REPUBLIC OF INDIA
    PASSPORT
    Name: John Doe
    Passport No: A1234567
    Sex: M
    Nationality: Indian
    """
    
    orchestrator = ApplicationGraphOrchestrator(
        record_data=record_data,
        document_text=document_text
    )
    result = await asyncio.to_thread(orchestrator.run)
    print("TEST RESULT:")
    for row in result.get('verification_analysis_report', []):
        print(f"Row: {row['field_name']} - Status: {row['status']} - Notes: {row['notes']}")

if __name__ == "__main__":
    asyncio.run(test_identity_filtering())
