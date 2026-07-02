import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.langgraph.employment_graph import EmploymentGraphOrchestrator

async def test_name_matching():
    record_data = {
        "applicantName": "John Smith Doe",
        "Company Name": "Acme Corp",
        "Compensation": "1400000"
    }
    document_text = """
    PAYSLIP 1 - April 2023
    Employee: John F Doe
    Employer: Acme Corp
    Monthly Salary: 100000 INR
    """
    
    orchestrator = EmploymentGraphOrchestrator(
        record_data=record_data,
        document_text=document_text
    )
    result = await asyncio.to_thread(orchestrator.run)
    print("TEST RESULT:")
    for row in result.get('field_comparison_summary', '').split('<tr>'):
        if 'applicantName' in row:
            print("Row:", row)

if __name__ == "__main__":
    asyncio.run(test_name_matching())
