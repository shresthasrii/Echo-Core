import json
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings
from backend.memory import memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("echo_core.seed")

async def seed_memory(bank_id: str = None, data_path: Path = None, limit: int = None):
    target_bank = bank_id or settings.HINDSIGHT_BANK_ID
    target_path = data_path or settings.SEED_DATA_PATH
    
    logger.info(f"Starting memory seeding for bank '{target_bank}'...")
    logger.info(f"Reading seed incidents from: {target_path}")
    
    if not target_path.exists():
        logger.error(f"Seed file not found at {target_path}")
        return False
        
    with open(target_path, "r", encoding="utf-8") as f:
        incidents = json.load(f)
        
    if limit:
        incidents = incidents[:limit]
        
    logger.info(f"Loaded {len(incidents)} seed incidents to ingest.")
    
    # 1. Ensure memory bank exists
    bank_ready = await memory.ensure_bank(target_bank)
    if not bank_ready:
        logger.error(f"Failed to ensure bank '{target_bank}'. Aborting seed.")
        return False
        
    # 2. Retain each incident into Hindsight
    success_count = 0
    fail_count = 0
    
    for idx, inc in enumerate(incidents, 1):
        inc_id = inc.get("id")
        svc = inc.get("svc")
        title = inc.get("title")
        logger.info(f"[{idx}/{len(incidents)}] Retaining {inc_id} ({svc}): {title[:40]}...")
        
        # Simulate realistic engineer feedback on past incidents
        feedback = {
            "worked": True,
            "notes": f"Verified by {inc.get('resolver', 'SRE')}. Fix restored service metrics within SLO."
        }
        
        res = await memory.retain_incident(inc, feedback=feedback, bank_id=target_bank)
        if res.get("success"):
            success_count += 1
        else:
            fail_count += 1
            logger.warning(f"Failed to retain {inc_id}: {res.get('error')}")
            
        # Modest pacing between retain requests to ensure smooth processing
        await asyncio.sleep(0.3)
        
    logger.info(f"Seeding completed: {success_count} succeeded, {fail_count} failed.")
    return success_count > 0

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Seed Echo-Core incidents into Hindsight")
    parser.add_argument("--bank", type=str, default=settings.HINDSIGHT_BANK_ID, help="Hindsight bank ID")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of incidents to seed")
    args = parser.parse_args()
    
    asyncio.run(seed_memory(bank_id=args.bank, limit=args.limit))
