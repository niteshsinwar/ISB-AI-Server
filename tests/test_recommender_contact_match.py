import asyncio
import os
import sys

# Add the project root to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.langgraph.recommender_graph import RecommenderGraphOrchestrator

async def test_contact_matching():
    # 1. Suspicious Match (Fraud)
    print("--- Test 1: Suspicious Match (Fraud) ---")
    recommender_record = {
        "First_Name__c": "John",
        "Last_Name__c": "Doe",
        "Email__c": "fraud@test.com",
        "MobilePhone__c": "+1234567890",
        "Status__c": "Submitted"
    }
    
    applicant_detail = {
        "First_Name__c": "Jane",
        "Last_Name__c": "Doe",
        "Email": "fraud@test.com", # Matching email!
        "MobilePhone": "+1234567890", # Matching mobile!
        "Parents_Name_From_Government_ID__c": "John Doe, Mary Doe"
    }
    
    responses = [
        {"Question__c": "How long have you known the applicant?", "Answer__c": "I have known my daughter Jane since birth."}
    ]
    
    orchestrator = RecommenderGraphOrchestrator(recommender_record, responses, applicant_detail)
    report = orchestrator.run()
    
    print(f"Confidence: {report['confidence_range']}")
    print(f"Mismatched Fields: {report['mismatched_field_list']}")
    print(f"Feedback: {report['overall_feedback']}")
    
    assert "email_cross_match_fraud" in report['mismatched_field_list']
    assert "mobile_cross_match_fraud" in report['mismatched_field_list']
    assert "FRAUD ALERT" in report['overall_feedback']
    assert int(report['confidence_range']) == 0 # 100 - 50 - 50 = 0

    print("Test passed!\n")

if __name__ == "__main__":
    asyncio.run(test_contact_matching())
