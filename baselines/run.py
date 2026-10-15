"""Fresh baseline runs using the released numerical implementation."""
from pathlib import Path
import argparse,json,random,sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
from baselines.search import Runner,all_configs
from baselines.hca import hca
def run(method,trace,seed,budget):
    info=json.loads((ROOT/"data/traces.json").read_text(encoding="utf-8"))[trace]
    x=np.load(ROOT/info["file"]).astype(float).ravel().reshape(info["n_ops"],-1)
    truth=(ROOT/info["truth_file"]).read_text(encoding="utf-8").strip()
    if method=="hca":
        return {"trace":trace,"seed":seed,"accuracy":hca(x,np.array([int(c=="A") for c in truth]),seed,three=info["constraints"]=="fpga3")}
    cfgs=all_configs();assert len(cfgs)==7008
    random.Random(seed).shuffle(cfgs)
    runner=Runner(x,info["n_major"],truth,seed,info["constraints"]);attempts=[]
    for cfg in cfgs[:budget]:
        r=runner.attempt(cfg);attempts.append(r)
        if r["passed"]:break
    final=next((r for r in attempts if r["passed"]),None) or next((r for r in reversed(attempts) if r["valid"] and r["acc"] is not None),None)
    return {"trace":trace,"seed":seed,"menu":"full","budget":budget,"attempts":attempts,"final":final}
if __name__=="__main__":
    p=argparse.ArgumentParser();p.add_argument("--method",choices=["hca","random"],required=True)
    p.add_argument("--trace",required=True);p.add_argument("--seed",type=int,default=0);p.add_argument("--budget",type=int,default=20)
    p.add_argument("--out",type=Path,default=ROOT/"local_baseline.json")
    a=p.parse_args();a.out.write_text(json.dumps(run(a.method,a.trace,a.seed,a.budget),indent=2)+"\n",encoding="utf-8")

