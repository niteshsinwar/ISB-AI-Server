from app.core.processing_utils import should_skip_processing

def test_skip_logic():
    # Scenario 1: AVS is 100%, but record was updated AFTER AVS -> Should NOT skip
    existing_avs = {
        "Percentage_Confidence__c": 100,
        "LastModifiedDate": "2024-01-01T10:00:00.000+0000"
    }
    record_date = "2024-01-02T10:00:00.000+0000"
    doc_date = "2023-12-01T10:00:00.000+0000"
    
    skip, reason = should_skip_processing(existing_avs, record_date, doc_date)
    print(f"Scenario 1 (100% confidence but newer record): Skip = {skip}, Reason = {reason}")
    assert not skip

    # Scenario 2: AVS is 50%, but it is NEWER than both -> Should skip!
    existing_avs = {
        "Percentage_Confidence__c": 50,
        "LastModifiedDate": "2024-01-05T10:00:00.000+0000"
    }
    record_date = "2024-01-02T10:00:00.000+0000"
    doc_date = "2023-12-01T10:00:00.000+0000"
    
    skip, reason = should_skip_processing(existing_avs, record_date, doc_date)
    print(f"Scenario 2 (50% confidence but data unchanged): Skip = {skip}, Reason = {reason}")
    assert skip

    print("All tests passed!")

if __name__ == "__main__":
    test_skip_logic()
