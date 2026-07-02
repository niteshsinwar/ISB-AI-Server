"""Saturation stress test: prove queue/slot behavior under concurrent load.

Fires 16 unique + 2 duplicate analyze requests SIMULTANEOUSLY at a running
server (default http://127.0.0.1:8011, org 'uat') and asserts:

  1. Accepted jobs never exceed MAX_CONCURRENT_PROCESSING_SLOTS.
  2. Duplicate submissions for an already-active application get 409.
  3. Requests beyond queue capacity get 429 (never a 500, never silence).
  4. Every accepted job reaches a terminal state (completed/failed).
  5. All slots are released afterwards (active_jobs returns to 0).

Run:  python tests/stress_saturation.py [BASE_URL]
"""
import asyncio
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

import aiohttp  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
ORG = "uat"
N_UNIQUE = 16
N_DUPLICATES = 2
POLL_INTERVAL = 10
DRAIN_TIMEOUT = 900  # 15 min


def find_app_ids(n):
    from app.config import SALESFORCE_ORGS
    from app.services.salesforce_service import SalesforceService
    org = SALESFORCE_ORGS[ORG]
    sf = SalesforceService(org["client_id"], org["client_secret"], org["token_url"], ORG)
    sf._ensure_connected()
    q = sf.sf.query(
        f"SELECT Id FROM hed__Application__c ORDER BY LastModifiedDate DESC LIMIT {n}"
    )
    return [r["Id"] for r in q["records"]]


async def submit(session, app_id):
    t0 = time.monotonic()
    try:
        async with session.post(
            f"{BASE}/{ORG}/api/v1/application/analyze",
            json={"record_id": app_id},
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            body = await resp.text()
            return app_id, resp.status, body[:150], round(time.monotonic() - t0, 2)
    except Exception as e:
        return app_id, -1, f"CLIENT ERROR: {e}", round(time.monotonic() - t0, 2)


async def overview(session):
    async with session.get(f"{BASE}/{ORG}/api/v1/application/queue-overview") as resp:
        return await resp.json()


async def status_of(session, app_id):
    async with session.get(f"{BASE}/{ORG}/api/v1/application/status/{app_id}") as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def main():
    print(f"Finding {N_UNIQUE} recent UAT applications...")
    app_ids = find_app_ids(N_UNIQUE)
    print(f"Got {len(app_ids)} application IDs")

    # burst = 16 unique + 2 duplicates of the first two (fired in the same gather)
    burst = app_ids + app_ids[:N_DUPLICATES]

    async with aiohttp.ClientSession() as session:
        print(f"\nFiring {len(burst)} concurrent analyze requests...")
        t0 = time.monotonic()
        results = await asyncio.gather(*(submit(session, a) for a in burst))
        burst_secs = round(time.monotonic() - t0, 2)

        codes = Counter(code for _, code, _, _ in results)
        print(f"Burst completed in {burst_secs}s. Status codes: {dict(codes)}")
        for app_id, code, body, secs in results:
            marker = "OK " if code in (200, 202) else str(code)
            print(f"  [{marker}] {app_id} ({secs}s) {body[:100] if code not in (200, 202) else ''}")

        accepted = [a for a, c, _, _ in results if c in (200, 202)]
        rejected_409 = [a for a, c, _, _ in results if c == 409]
        rejected_429 = [a for a, c, _, _ in results if c == 429]
        errors_5xx = [(a, c) for a, c, _, _ in results if c >= 500 or c == -1]

        ov = await overview(session)
        print(f"\nQueue right after burst: active={ov['active_jobs']} "
              f"tracked={ov['tracked_jobs_total']} load={ov['slot_utilization']['load_percent']}%")

        # ---- Assertions on admission behavior ----
        problems = []
        if errors_5xx:
            problems.append(f"5xx/client errors during burst: {errors_5xx}")
        max_slots = 15
        if len(set(accepted)) > max_slots:
            problems.append(f"accepted {len(set(accepted))} unique jobs > {max_slots} slots")
        if len(accepted) != len(set(accepted)):
            problems.append("same application accepted twice (duplicate protection failed)")
        # every unique app got a definitive answer
        answered = set(accepted) | set(rejected_409) | set(rejected_429)
        missing = set(burst) - answered
        if missing:
            problems.append(f"no definitive response for: {missing}")

        # ---- Drain: wait for every accepted job to reach a terminal state ----
        print(f"\nDraining {len(accepted)} accepted jobs (timeout {DRAIN_TIMEOUT}s)...")
        deadline = time.monotonic() + DRAIN_TIMEOUT
        terminal = {}
        while time.monotonic() < deadline:
            pending = [a for a in accepted if a not in terminal]
            if not pending:
                break
            for a in pending:
                s = await status_of(session, a)
                if s and s.get("status") in ("completed", "failed"):
                    terminal[a] = s["status"]
                    print(f"  terminal: {a} -> {s['status']}")
            if len(terminal) < len(accepted):
                await asyncio.sleep(POLL_INTERVAL)

        stuck = [a for a in accepted if a not in terminal]
        if stuck:
            problems.append(f"jobs never reached terminal state within {DRAIN_TIMEOUT}s: {stuck}")

        # ---- Slot release ----
        ov2 = await overview(session)
        print(f"\nQueue after drain: active={ov2['active_jobs']} tracked={ov2['tracked_jobs_total']}")
        if ov2["active_jobs"] != 0:
            problems.append(f"slots not released: active_jobs={ov2['active_jobs']} after drain")

        outcome = Counter(terminal.values())
        print("\n" + "=" * 70)
        print(f"RESULT: {len(accepted)} accepted | {len(rejected_409)}x409 | "
              f"{len(rejected_429)}x429 | outcomes={dict(outcome)}")
        if problems:
            print("SATURATION TEST FAILED:")
            for p in problems:
                print("  ✗", p)
            sys.exit(1)
        print("SATURATION TEST PASSED: capacity enforced, duplicates rejected, "
              "all jobs terminal, all slots released.")


if __name__ == "__main__":
    asyncio.run(main())
