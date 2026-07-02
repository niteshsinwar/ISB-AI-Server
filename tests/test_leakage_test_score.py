import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.langgraph.test_score_graph import TestScoreGraphOrchestrator

async def test_leakage():
    # Pass a payload that maliciously contains employment and passport data
    record_data = {
        "applicantName": "John Doe",
        "Birthdate__c": "1990-01-01",
        "Job Title": "Software Engineer",
        "Passport Number": "A1234567",
        "API_VerbalScore": "30",
        "Applicant_VerbalScore": "30"
    }
    document_text = """
    GMAT SCORE REPORT
    Name: John Doe
    DOB: 1990-01-01
    Verbal: 30
    Job Title: Software Engineer
    Passport: A1234567
    """
    
    orchestrator = TestScoreGraphOrchestrator(
        record_data=record_data,
        document_text=document_text
    )
    result = await asyncio.to_thread(orchestrator.run)
    print("TEST RESULT:")
    print(result.get('field_comparison_summary', ''))

if __name__ == "__main__":
    asyncio.run(test_leakage())
