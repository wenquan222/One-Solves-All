"""Load the exact prompts archived for the reported cohort."""
from pathlib import Path
import json
ROOT=Path(__file__).resolve().parents[1]
SPEC=json.loads((ROOT/"data/traces.json").read_text(encoding="utf-8"))
REAL=[t for t in SPEC if not t.startswith("Sim-")]
SIM=[t for t in SPEC if t.startswith("Sim-")]
ALL=REAL+SIM
def build(trace):
    info=SPEC[trace]
    raw=(ROOT/"prompts"/(trace+".txt")).read_text(encoding="utf-8")
    system,human=raw.removeprefix("System:\n").split("\nHuman: ",1)
    return dict(system=system,human=human.rstrip("\n"),src=str(ROOT/info["file"]),file=info["filename"])
