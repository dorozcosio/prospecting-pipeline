"""
Simple file-based checkpointing so Pipeline 1 can resume after crashes.
Tracks which PIs have been fully processed (homepage found, members extracted, written to sheet).
"""
import json
import os
from datetime import datetime

CHECKPOINT_DIR = "logs"
CHECKPOINT_FILE = os.path.join(CHECKPOINT_DIR, "p1_checkpoint.json")


def load_checkpoint() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            return json.load(f)
    return {"completed_pis": [], "started_at": None, "last_updated": None}


def save_checkpoint(pi_key: str) -> None:
    cp = load_checkpoint()
    if pi_key not in cp["completed_pis"]:
        cp["completed_pis"].append(pi_key)
    cp["last_updated"] = datetime.now().isoformat()
    if not cp["started_at"]:
        cp["started_at"] = cp["last_updated"]
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(cp, f, indent=2)


def clear_checkpoint() -> None:
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
