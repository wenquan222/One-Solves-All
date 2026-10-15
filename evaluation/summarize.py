"""Recompute paper statistics from released records, with zero model calls."""
from pathlib import Path
from collections import defaultdict
import argparse,csv,json,math,statistics
ROOT=Path(__file__).resolve().parents[1]
def load(p):return json.loads((ROOT/p).read_text(encoding="utf-8"))
def csvout(p,rs):
    p.parent.mkdir(parents=True,exist_ok=True)
    with p.open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=list(rs[0]));w.writeheader();w.writerows(rs)
def endpoint(attempts,budget):
    sub=attempts[:budget]
    return next((a for a in sub if a.get("passed")),None) or next((a for a in reversed(sub) if a.get("valid") and a.get("acc") is not None),None)
def summarize(out):
    cohort=load("manifest.json");spec=load("data/traces.json")
    records=[];bytrace=defaultdict(list);costs=[]
    for row in cohort["runs"]:
        folder=ROOT/row["path"]
        scored=json.loads((folder/"metrics.json").read_text(encoding="utf-8"))
        pred=(folder/"final_sequence.txt").read_text(encoding="utf-8").strip()
        truth=(ROOT/spec[row["trace"]]["truth_file"]).read_text(encoding="utf-8").strip()
        acc=100*sum(x==y for x,y in zip(pred,truth))/len(truth) if len(pred)==len(truth) else 0
        assert abs(acc-scored["operation_accuracy"])<1e-7,(row["path"],acc,scored["operation_accuracy"])
        assert (pred==truth)==scored["sequence_exact"]
        scored.update(model=row["model"],attempts=row["attempts"])
        records.append(scored);bytrace[row["trace"]].append(scored)
        costs.append(json.loads((folder/"cost.json").read_text(encoding="utf-8")))
    metrics=records+load("baselines/records/scored_results.json")
    table=[]
    for trace in spec:
        llm=bytrace[trace]
        means={m:statistics.mean(r["attempts"] for r in llm if r["model"]==m) for m in ["gpt4o","deepseek"]}
        budget=max(1,math.ceil(statistics.mean(means.values())))
        row={"trace":trace,"random_budget":budget}
        for m in ["hca","random","gpt4o","deepseek"]:
            rs=[r for r in metrics if r["trace"]==trace and (r.get("method") or r.get("model"))==m]
            assert len(rs)==10
            row[m]=statistics.mean(r["operation_accuracy"] for r in rs)
            if m=="random":
                for r in rs:
                    d=load(r["source"]);e=endpoint(d["attempts"],budget)
                    assert abs((e["acc"] if e else 0)-r["operation_accuracy"])<1e-7
        row.update(gpt4o_attempts=means["gpt4o"],deepseek_attempts=means["deepseek"]);table.append(row)
    for row,e in zip(table,load("results/reported_table4.json")["rows"]):
        assert row["trace"]==e["trace"]
        for k,old in [("hca","hca"),("random","random"),("gpt4o","gpt"),("deepseek","ds")]:
            assert round(row[k],2)==round(e[old],2),(row["trace"],k,row[k],e[old])
    avg={"trace":"Average","random_budget":"","hca":statistics.mean(r["hca"] for r in table),
         "random":statistics.mean(r["random"] for r in table),"gpt4o":statistics.mean(r["gpt4o"] for r in table),
         "deepseek":statistics.mean(r["deepseek"] for r in table),"gpt4o_attempts":"","deepseek_attempts":""}
    table.append(avg)
    success=[]
    for m in ["hca","random","gpt4o","deepseek"]:
        rs=[r for r in metrics if (r.get("method") or r.get("model"))==m]
        n=sum(r["target_exact"] for r in rs)
        success.append({"method":m,"runs":len(rs),"successes":n,"success_rate":100*n/len(rs)})
    expense=[]
    for m in ["gpt4o","deepseek"]:
        rs=[r for r in costs if r["model"]==m]
        expense.append({"model":m,"runtime_sec":statistics.mean(r["duration_sec"] for r in rs),
          "input_tokens":statistics.mean(r["input_tokens"] for r in rs),"output_tokens":statistics.mean(r["output_tokens"] for r in rs),
          "cached_input_percent":100*sum(r["cached_tokens"] for r in rs)/sum(r["input_tokens"] for r in rs),
          "estimated_api_cost":statistics.mean(r["estimated_fee"] for r in rs),"currency":rs[0]["currency"]})
    curves=[]
    random_records=[load(str(p.relative_to(ROOT))) for p in sorted((ROOT/"baselines/records/random").glob("*.json"))]
    for m in ["random","gpt4o","deepseek"]:
        if m!="random":
            states=[load(r["path"]+"/attempts.json") for r in cohort["runs"] if r["model"]==m and not r["trace"].startswith("Sim-")]
        for budget in range(1,101):
            if m=="random":
                chosen=[endpoint(d["attempts"],budget) for d in random_records if not d["trace"].startswith("Sim-")]
                acc=[r["acc"] if r else 0 for r in chosen]
            else:
                ss=[r["attempts"][min(budget,len(r["attempts"]))-1]["score"] for r in states]
                acc=[r["accuracy"] if r else 0 for r in ss]
            assert len(acc)==70
            exact=sum(abs(x-100)<1e-9 for x in acc)
            curves.append({"model":m,"attempt":budget,"accuracy":statistics.mean(acc),"success_rate":100*exact/70,"exact":exact,"n":70})
    with (ROOT/"results/real_budget_metrics.csv").open(encoding="utf-8") as f:expected=list(csv.DictReader(f))
    for a,e in zip(curves,expected):
        assert a["model"]==e["model"] and a["attempt"]==int(e["attempt"])
        assert abs(a["accuracy"]-float(e["accuracy"]))<1e-7 and abs(a["success_rate"]-float(e["success_rate"]))<1e-7
    csvout(out/"table4.csv",table);csvout(out/"success_rates.csv",success)
    csvout(out/"table5_costs.csv",expense);csvout(out/"real_budget_metrics.csv",curves)
    print(json.dumps({"llm_runs":len(records),"traces":len(spec),"mean_accuracy":avg,"success":success},indent=2))
if __name__=="__main__":
    ap=argparse.ArgumentParser();ap.add_argument("--out",type=Path,default=ROOT/"results/recomputed")
    summarize(ap.parse_args().out)

