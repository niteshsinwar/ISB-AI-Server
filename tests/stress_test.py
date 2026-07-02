import asyncio
import os
import sys
import time
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()

from app.main import app

async def run_stress_test():
    app_id = 'a3lfs0000000gnFAAQ'  # PGP Application with docs
    eedl_app_id = 'a3lfs0000000gvJAAQ'  # EEDL Application with docs
    
    print("Starting API concurrent stress testing...")
    start_time = time.time()
    
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        tasks = []
        
        # 10 concurrent Admission webhook calls
        for i in range(10):
            tasks.append(client.post(
                f"/uat/api/v1/application/analyze",
                json={"record_id": app_id},
                headers={"x-trigger-source": f"stress_test_{i}"}
            ))
            
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        
        for i, res in enumerate(responses):
            if isinstance(res, Exception):
                print(f"Request {i} FAILED: {res}")
            else:
                print(f"Request {i} SUCCESS - Status Code: {res.status_code}")
                try:
                    print(res.json())
                except:
                    pass

    end_time = time.time()
    print(f"\nAPI Stress Test completed in {end_time - start_time:.2f} seconds.")
    
if __name__ == "__main__":
    asyncio.run(run_stress_test())
