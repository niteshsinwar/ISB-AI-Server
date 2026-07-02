import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.langgraph.education_graph import EducationGraphOrchestrator

async def test_semester_handling():
    record_data = {
        "SF Full Name": "John Doe",
        "Institution": "Test University",
        "Degree/Qualification": "B.Tech",
        "SF Field of Study": "Engineering",
        "Major/Specialization": "Computer Science",
        "GPA/Percentage": "8.5"
    }
    document_text = """
    Test University - Marksheet
    Name: John Doe
    Degree: B.Tech Computer Science Engineering
    
    Semester 1
    Subjects: Math, Physics
    SGPA: 8.0
    
    Semester 2
    Subjects: Chem, CS
    SGPA: 9.0
    
    Semester 3
    Subjects: Algorithms, DS
    SGPA: 8.5
    """
    
    orchestrator = EducationGraphOrchestrator(
        record_data=record_data,
        document_text=document_text
    )
    result = await asyncio.to_thread(orchestrator.run)
    print("TEST RESULT:")
    for row in result.get('field_comparison_summary', '').split('<tr>'):
        if 'Number of Semesters' in row or 'GPA/Percentage' in row:
            print("Row:", row)

if __name__ == "__main__":
    asyncio.run(test_semester_handling())
