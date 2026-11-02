import sqlite3
from pathlib import Path
from harness_awm.contracts import TaskSpec
from harness_awm.io import db_fingerprint, digest, file_hash, write_json

SERVER = '''
import os, time, sqlite3
from fastapi import FastAPI
from sqlalchemy import create_engine
import uvicorn
app = FastAPI()
engine = create_engine("sqlite:///placeholder.db")

@app.get("/state", operation_id="read_state")
def read_state():
    with sqlite3.connect(engine.url.database) as db:
        return {"value": db.execute("SELECT value FROM items WHERE id=1").fetchone()[0]}

@app.post("/write", operation_id="write_value")
def write_value(value: int, delay: float = 0):
    with sqlite3.connect(engine.url.database) as db:
        db.execute("UPDATE items SET value=? WHERE id=1", (value,))
    time.sleep(delay)
    return {"value": value}

@app.get("/long", operation_id="long_result")
def long_result():
    return {"records": "A" * 20000 + "LAST_RECORD"}

@app.get("/file", operation_id="probe_path")
def probe_path(path: str):
    try:
        with open(path) as f:
            return {"readable": True, "content": f.read()}
    except OSError:
        return {"readable": False}

@app.get("/crash", operation_id="crash")
def crash():
    os._exit(9)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
'''

VERIFIER = '''def verify_task(initial_db_path, final_db_path):
    import sqlite3
    with sqlite3.connect(initial_db_path) as a, sqlite3.connect(final_db_path) as b:
        return {"initial": a.execute("select value from items").fetchone()[0],
                "final": b.execute("select value from items").fetchone()[0]}
'''


def fixture(root, verifier_code=VERIFIER):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = root / "initial.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value INTEGER)")
        conn.execute("INSERT INTO items VALUES(1,10)")
    env = {"scenario": "harness_fixture", "full_code": SERVER}
    verifier = {"scenario": "harness_fixture", "task_idx": 0, "task": "Set value to 80 then report it.",
                "verification": {"code": verifier_code}}
    write_json(root / "environment.json", env)
    write_json(root / "verifier.json", verifier)
    return TaskSpec(dataset_version="fixture-v1", lineage_group_id="fixture-world-1", scenario="harness_fixture",
        native_task_id=0, task_instance_id="fixture:0", instruction=verifier["task"],
        initial_state_ref=str(db.resolve()), initial_state_hash=file_hash(db), initial_state_fingerprint=db_fingerprint(db),
        environment_ref=str((root / "environment.json").resolve()), environment_code_hash=digest(env),
        verifier_ref=str((root / "verifier.json").resolve()), verifier_hash=digest(verifier), source_manifest_hash="fixture")


def finish(answer="done"):
    return {"type": "finish", "answer": answer}


def call(name, **arguments):
    return {"type": "call_tool", "name": name, "arguments": arguments}


LIST = {"type": "list_tools"}
