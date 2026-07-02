import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.langgraph.employment_graph import EmploymentGraphOrchestrator

async def test_currency_handling():
    record_data = {
        "applicantName": "John Doe",
        "Company Name": "Global Tech",
        "Compensation": "1000 USD"
    }
    document_text = """
    PAYSLIP
    Employee: John Doe
    Employer: Global Tech
    Monthly Salary: 7200 INR
    Annual Salary (Total): 86400 INR
    """
    
    orchestrator = EmploymentGraphOrchestrator(
        record_data=record_data,
        document_text=document_text
    )
    result = await asyncio.to_thread(orchestrator.run)
    print("TEST RESULT:")
    for row in result.get('field_comparison_summary', '').split('<tr>'):
        if 'Compensation' in row:
            print("Row:", row)

if __name__ == "__main__":
    asyncio.run(test_currency_handling())
