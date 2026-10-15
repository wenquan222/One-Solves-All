"""Tool-calling SPA runner. Prompts are loaded from the frozen paper release."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from datetime import datetime
from typing import Any, List, Optional, Tuple

import requests
from langchain.agents import AgentExecutor, create_tool_calling_agent
from langchain.callbacks.base import BaseCallbackHandler
from langchain.tools import BaseTool
from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exp2_prompts as P  # noqa: E402

MODELS = {
    "gpt4o": dict(model="openai/gpt-4o-2024-11-20", base="https://openrouter.ai/api/v1", key="OPENROUTER_API_KEY"),
    "deepseek": dict(model="deepseek-ai/DeepSeek-V3.1-Terminus", base="https://api.siliconflow.cn/v1",
                     key="SILICONFLOW_API_KEY"),
}
LIMIT_MSG = "Tool call limit reached. No further code can be executed. Please give your final answer now."


def read_key(name):
    value=os.environ.get(name)
    if not value:
        raise SystemExit(f"Set the {name} environment variable.")
    return value


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


# ──────────────────────────────────────────────
# 内核服务的启动与关闭
# ──────────────────────────────────────────────

def _health(port):
    try:
        return requests.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200
    except Exception:
        return False


def stop_server(proc, port):
    try:
        requests.post(f"http://127.0.0.1:{port}/shutdown", timeout=5)
    except Exception:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=15)
        except Exception:
            subprocess.run(f"taskkill /PID {proc.pid} /T /F", shell=True, capture_output=True)
    for _ in range(20):
        if not _health(port):
            return
        time.sleep(0.5)


def start_server(port, work_dir, log_path):
    if _health(port):                                   # 上一次没关干净的服务
        stop_server(None, port)
    logf = open(log_path, "w", encoding="utf-8")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", TF_CPP_MIN_LOG_LEVEL="3")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "exp2_kernel_server.py"),
                             "--port", str(port), "--cwd", work_dir],
                            stdout=logf, stderr=subprocess.STDOUT, cwd=HERE, env=env)
    for _ in range(180):
        if proc.poll() is not None:
            raise RuntimeError(f"内核服务启动失败，见 {log_path}")
        if _health(port):
            return proc
        time.sleep(0.5)
    stop_server(proc, port)
    raise RuntimeError(f"内核服务 90 秒内未就绪，见 {log_path}")


# ──────────────────────────────────────────────
# JupyterAPITool（照搬原版，加调用上限、快照、逐次记录）
# ──────────────────────────────────────────────

class JupyterAPITool(BaseTool):
    name: str = "JupyterAPITool"
    description: str = (
        "Execute Python code inside Jupyter kernel backend "
        "and automatically save matplotlib plots."
    )
    server_port: int = 5061
    work_dir: str = ""
    run_dir: str = ""
    image_rel: str = "../images"                         # 相对内核工作目录
    max_calls: int = 160
    max_attempts: int = 0                                # 0 = 不按尝试文件数设上限（提示词不要求存尝试文件）

    _plot_counter: int = PrivateAttr(default=0)
    _st: dict = PrivateAttr(default_factory=lambda: dict(n_calls=0, n_refused=0, n_attempts=0, n_snap=0, seen={}))

    @staticmethod
    def _preprocess_code(raw: str) -> str:
        s = raw.strip()
        s = re.sub(r"^\s*```(?:python|py)?\s*\n?", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\n?\s*```\s*$", "", s, flags=re.IGNORECASE)
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        s = s.lstrip("\ufeff").replace("\u00A0", " ")
        s = textwrap.dedent(s)
        lines = s.split("\n")
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    @staticmethod
    def _indent_block(block: str, indent: str) -> str:
        return "\n".join((indent + l if l else l) for l in block.splitlines())

    def _generate_plot_path(self, save_dir: str) -> str:
        self._plot_counter += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"{save_dir}/img_{ts}_{self._plot_counter}.png"

    def _savefig_block(self, plot_path: str) -> str:
        return textwrap.dedent(
            f"""
            try:
                import matplotlib.pyplot as plt
                import json as _json
                plt.savefig({json.dumps(plot_path)}, dpi=160, bbox_inches='tight')
                plt.close()
                print(_json.dumps({{"plot_path": {json.dumps(plot_path)}}}, ensure_ascii=False))
            except Exception as e:
                print("图片保存失败:", str(e))
            """
        ).rstrip("\n")

    def _inject_savefig(self, code: str, save_dir: str) -> Tuple[str, List[str]]:
        if "plt.savefig" in code:
            return code, []
        plot_paths = []
        pat = re.compile(r"(?m)^(?P<indent>\s*)plt\.show\(\)\s*$")

        def repl(match):
            indent = match.group("indent") or ""
            plot_path = self._generate_plot_path(save_dir)
            plot_paths.append(plot_path)
            return self._indent_block(self._savefig_block(plot_path), indent)

        new_code, n = pat.subn(repl, code)
        if n == 0 and ("matplotlib" in code or "plt." in code):
            plot_path = self._generate_plot_path(save_dir)
            plot_paths.append(plot_path)
            new_code = new_code.rstrip() + "\n\n" + self._savefig_block(plot_path) + "\n"
        return new_code, plot_paths

    # ── 快照：work/ 下新增或内容有变化的 txt 另存一份，带调用序号 ──
    def _snapshot(self, call_idx: int) -> List[dict]:
        st = self._st
        cands = []
        for root, _, files in os.walk(self.work_dir):
            for fn in files:
                if not fn.lower().endswith(".txt"):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                    mt = os.stat(p).st_mtime_ns
                except OSError:
                    continue
                rel = os.path.relpath(p, self.work_dir).replace(os.sep, "/")
                sig = hashlib.sha1(data).hexdigest()
                if st["seen"].get(rel) != sig:
                    cands.append((mt, rel, sig, data))
        new = []
        for mt, rel, sig, data in sorted(cands):
            st["seen"][rel] = sig
            st["n_snap"] += 1
            snap = f"call{call_idx:03d}_{st['n_snap']:04d}_{rel.replace('/', '__')}"
            with open(os.path.join(self.run_dir, "snapshots", snap), "wb") as f:
                f.write(data)
            if rel.startswith("attempts/"):
                st["n_attempts"] += 1
            new.append(dict(rel=rel, snap=snap, bytes=len(data), mtime_ns=mt))
        return new

    def _log_call(self, rec: dict):
        with open(os.path.join(self.run_dir, "calls.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def _run(self, code: str) -> str:
        st = self._st
        if st["n_calls"] >= self.max_calls or (self.max_attempts and st["n_attempts"] >= self.max_attempts):
            st["n_refused"] += 1
            self._log_call(dict(call=None, refused=True, code=code))
            return LIMIT_MSG
        st["n_calls"] += 1
        idx = st["n_calls"]
        t0 = time.time()
        sent = self._preprocess_code(code)
        sent, _ = self._inject_savefig(sent, self.image_rel)
        try:
            resp = requests.post(f"http://127.0.0.1:{self.server_port}/execute", json={"code": sent}, timeout=3600)
            if resp.status_code != 200:
                result = f"❌ Jupyter API Error: {resp.status_code} - {resp.text}"
            else:
                result = ""
                for item in resp.json().get("outputs", []):
                    t = item.get("type")
                    if t in ("text", "stream"):
                        result += item.get("content", "")
                    elif t == "error":
                        result += "\n---ERROR---\n" + item.get("content", "")
                result = result.strip()
        except Exception as e:
            result = f"❌ Tool Error: {e}"
        new = self._snapshot(idx)
        self._log_call(dict(call=idx, sec=round(time.time() - t0, 2), code=code, output=result, new_files=new,
                            n_attempts_so_far=st["n_attempts"]))
        return result


# ──────────────────────────────────────────────
# 回调：Markdown 日志（格式同原版）与 token 用量
# ──────────────────────────────────────────────

class MarkdownLogHandler(BaseCallbackHandler):
    def __init__(self):
        self.records: List[str] = []
        self.step_idx = 0
        self.pending: Optional[str] = None

    def _flush(self):
        if self.pending:
            self.records.append(self.pending)
            self.records.append("\n**Observation:**\n_[no output from tool]_\n")
            self.pending = None

    def on_agent_action(self, action, **kwargs: Any) -> Any:
        self._flush()
        self.step_idx += 1
        raw = action.tool_input
        code = raw.get("code", json.dumps(raw, ensure_ascii=False)) if isinstance(raw, dict) else str(raw)
        self.pending = (f"\n---\n**Step {self.step_idx}**\n\n**Action:** `{action.tool}`\n"
                        f"\n**Action Input:**\n```python\n{code}\n```\n")

    def on_tool_end(self, output, **kwargs: Any) -> Any:
        content = "" if output is None else str(output).strip()
        if self.pending:
            self.records.append(self.pending)
            self.pending = None
        self.records.append("\n**Observation:**\n")
        self.records.append(f"{content}\n" if content else "_[no output from tool]_\n")

    def on_chain_end(self, outputs, **kwargs: Any) -> Any:
        self._flush()


class UsageHandler(BaseCallbackHandler):
    def __init__(self):
        self.calls: List[dict] = []

    def on_llm_end(self, response, **kwargs: Any) -> Any:
        u = dict(prompt=None, completion=None, cached=None)
        tu = (getattr(response, "llm_output", None) or {}).get("token_usage") or {}
        if tu:
            u = dict(prompt=tu.get("prompt_tokens"), completion=tu.get("completion_tokens"),
                     cached=(tu.get("prompt_tokens_details") or {}).get("cached_tokens"))
        else:
            try:
                um = getattr(response.generations[0][0].message, "usage_metadata", None) or {}
                u = dict(prompt=um.get("input_tokens"), completion=um.get("output_tokens"),
                         cached=(um.get("input_token_details") or {}).get("cache_read"))
            except Exception:
                pass
        self.calls.append(u)

    def totals(self):
        def s(k):
            vals = [c[k] for c in self.calls if c.get(k) is not None]
            return sum(vals) if vals else None
        prompts = [c["prompt"] for c in self.calls if c.get("prompt") is not None]
        return dict(n_llm_calls=len(self.calls), prompt_tokens=s("prompt"), completion_tokens=s("completion"),
                    cached_tokens=s("cached"), max_prompt_tokens_single_call=max(prompts) if prompts else None,
                    n_calls_without_usage=sum(1 for c in self.calls if c.get("prompt") is None))


# ──────────────────────────────────────────────
# 单次运行
# ──────────────────────────────────────────────

KEY_NAME = None                                       # --key-name 指定时覆盖 MODELS 里的密钥行名
TEMPERATURE = 0.0                                     # --temperature 指定；正式运行为 0，温度试验用单独输出目录
_SRC_MD5 = {}


def lib_versions():
    """库版本（不 import，读安装信息），离线重放时用来确认环境相同。"""
    from importlib.metadata import PackageNotFoundError, version
    out = dict(python=sys.version.split()[0])
    for name in ("numpy", "scipy", "scikit-learn", "umap-learn", "langchain", "langchain-core", "langchain-openai", "openai",
                 "ipykernel", "jupyter-client"):
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            out[name] = None
    return out


def run_one(model_key, trace, run_id, port, out_root, max_attempts, max_calls):
    m = MODELS[model_key]
    b = P.build(trace)
    run_dir = os.path.join(out_root, model_key, trace, f"run{run_id:02d}")
    if os.path.exists(os.path.join(run_dir, "run.json")):
        return "skip", None
    if os.path.exists(run_dir):                         # 上次中断留下的半截目录
        dst = os.path.join(out_root, "_incomplete", f"{model_key}_{trace}_run{run_id:02d}_{datetime.now():%Y%m%d_%H%M%S}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(run_dir, dst)
    work = os.path.join(run_dir, "work")
    for d in ("work/trace", "images", "snapshots"):
        os.makedirs(os.path.join(run_dir, d))
    trace_copy = os.path.join(work, "trace", b["file"])
    shutil.copy2(b["src"], trace_copy)                  # 复制而不是硬链接：模型改写文件也碰不到原数据
    if b["src"] not in _SRC_MD5:
        _SRC_MD5[b["src"]] = md5_of(b["src"])
    with open(os.path.join(run_dir, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write("System:\n" + b["system"] + "\nHuman: " + b["human"] + "\n")

    key = read_key(KEY_NAME or m["key"])
    md, usage = MarkdownLogHandler(), UsageHandler()
    tool = JupyterAPITool(server_port=port, work_dir=work, run_dir=run_dir, max_calls=max_calls, max_attempts=max_attempts)
    started = datetime.now()
    t0 = time.time()
    status, err, output = "finished", None, ""
    proc = None
    try:
        proc = start_server(port, work, os.path.join(run_dir, "kernel.log"))
        llm = ChatOpenAI(model=m["model"], temperature=TEMPERATURE, max_tokens=16383, disabled_params={"stop": None},
                         openai_api_base=m["base"], openai_api_key=key)
        prompt = ChatPromptTemplate.from_messages([
            SystemMessage(content=b["system"]),
            HumanMessagePromptTemplate.from_template("{input}"),
            MessagesPlaceholder(variable_name="agent_scratchpad"),
        ])
        agent = create_tool_calling_agent(llm, [tool], prompt)
        executor = AgentExecutor(agent=agent, tools=[tool], verbose=False, handle_parsing_errors=True,
                                 return_intermediate_steps=True, max_iterations=max_calls + 5,
                                 max_execution_time=7200, early_stopping_method="force", stream_runnable=False)
        result = executor.invoke({"input": b["human"]}, config={"callbacks": [md, usage]})
        output = (result.get("output") or "").strip()
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:2000]
        low = err.lower()
        # 对话太长超出模型上下文：已做过的尝试照样有效，这次运行到此为止，不当作出错重跑
        ctx_limit = tool._st["n_calls"] > 0 and (("context" in low and any(w in low for w in ("length", "maximum", "window")))
                                                 or "too long" in low or "too many tokens" in low)
        status = "finished" if ctx_limit else "error"
    else:
        ctx_limit = False
    finally:
        stop_server(proc, port)
    duration = time.time() - t0
    st = tool._st
    if status == "error":
        stop = "error"
    elif ctx_limit:
        stop = "context_limit"
    elif st["n_refused"] > 0:
        stop = "limit"
    elif output.startswith("Agent stopped"):
        stop = "executor_limit"
    else:
        stop = "final_answer"
    trace_intact = os.path.exists(trace_copy) and md5_of(trace_copy) == _SRC_MD5[b["src"]]
    if os.path.exists(trace_copy):
        os.remove(trace_copy)                           # 波形副本不留，省空间（原文件在 实验真实波_4 / trace2）
    rec = dict(model=model_key, model_id=m["model"], api_base=m["base"], api_key_name=KEY_NAME or m["key"],
               trace=trace, run_id=run_id, status=status,
               stop_reason=stop, started=started.strftime("%Y-%m-%d %H:%M:%S"), duration_sec=round(duration, 1),
               temperature=TEMPERATURE, max_attempts=max_attempts, max_calls=max_calls,
               n_tool_calls=st["n_calls"], n_refused=st["n_refused"], n_attempt_snapshots=st["n_attempts"],
               result_file=os.path.exists(os.path.join(work, "result.txt")), trace_file_intact=trace_intact,
               trace_md5=_SRC_MD5[b["src"]], tokens=usage.totals(), final_output=output, error=err, env=lib_versions())
    body = "".join(md.records)
    with open(os.path.join(run_dir, "agent_log.md"), "w", encoding="utf-8") as f:
        f.write(f"# Agent Execution Log\n**Query:**\n```text\n{b['human']}\n```\n"
                f"\n---\n## Execution Summary\n- **Model:** `{m['model']}`\n- **Time:** `{rec['started']}`\n"
                f"- **Duration:** `{duration:.2f}s`\n- **Path:** `{run_dir}`\n"
                f"\n---\n## Prompt\n<details><summary>Expand</summary>\n\n```\nSystem: \n{b['system']}\nHuman: {b['human']}\n```\n\n</details>\n"
                f"\n---\n## Execution Log\n{body}\n\n---\n**Final Output:**\n```text\n{output}\n```\n"
                + (f"\nERROR: {err}\n" if err else ""))
    if status == "error":                               # 出错的运行不算数：挪到 _failed/，不写 run.json
        with open(os.path.join(run_dir, "error.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        dst = os.path.join(out_root, "_failed", f"{model_key}_{trace}_run{run_id:02d}_{datetime.now():%Y%m%d_%H%M%S}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(run_dir, dst)
    else:
        with open(os.path.join(run_dir, "run.json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
    return status, rec


def parse_runs(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(MODELS))
    ap.add_argument("--traces", required=True, help="波形名（逗号分隔），或 real / sim / all")
    ap.add_argument("--runs", default="1", help="如 1、1-10、1,3,5")
    ap.add_argument("--port", type=int, default=5061)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(HERE), "local_runs"))
    ap.add_argument("--max-attempts", type=int, default=0, help="0 = 不设（默认）")
    ap.add_argument("--max-calls", type=int, default=160)
    ap.add_argument("--shard", default=None, help="i/n：只跑第 i 份（并行时每个进程一份，端口各不相同）")
    ap.add_argument("--time-budget-min", type=float, default=0,
                    help="开跑超过这么多分钟后不再开始新的运行（0 = 不限）。后台任务单次最长 2 小时，分段跑时用")
    ap.add_argument("--key-name", default=None, help="环境变量名，如 SILICONFLOW_API_KEY_2（默认按模型取）")
    ap.add_argument("--temperature", type=float, default=0.0, help="采样温度（默认 0；温度试验请配合 --out 用单独目录）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    traces = {"real": P.REAL, "sim": P.SIM, "all": P.ALL}.get(args.traces) or args.traces.split(",")
    for t in traces:
        assert t in P.SPEC, t
    tasks = [(t, r) for r in parse_runs(args.runs) for t in traces]
    if args.shard:
        i, n = map(int, args.shard.split("/"))
        tasks = tasks[i::n]
    print(f"模型 {args.model}（{MODELS[args.model]['model']}），{len(tasks)} 次运行，端口 {args.port}，"
          f"上限 {args.max_calls} 次工具调用，温度 {args.temperature}", flush=True)
    if args.dry_run:
        for t, r in tasks:
            print(" ", t, r)
        sys.exit(0)
    KEY_NAME = args.key_name                            # 主程序在模块层，直接赋值即可
    TEMPERATURE = args.temperature
    read_key(KEY_NAME or MODELS[args.model]["key"])     # 没填密钥就直接停
    t_start = time.time()
    n_left = 0
    for t, r in tasks:
        if os.path.exists(os.path.join(args.out, args.model, t, f"run{r:02d}", "run.json")):
            continue                                    # 已完成的直接跳过
        if args.time_budget_min and time.time() - t_start > args.time_budget_min * 60:
            n_left += 1
            continue
        for retry in range(3):
            status, rec = run_one(args.model, t, r, args.port, args.out, args.max_attempts, args.max_calls)
            if status != "error":
                break
            print(f"[{args.model} {t} run{r:02d}] 出错（第 {retry + 1} 次）：{rec['error'][:300]}", flush=True)
            time.sleep((30, 90, 180)[retry])
        if status == "error":
            raise SystemExit(f"[{args.model} {t} run{r:02d}] 连续 3 次出错，批量停止。出错的运行在 {args.out}\\_failed\\")
        tk = rec["tokens"]
        print(f"[{args.model} {t} run{r:02d}] {rec['stop_reason']}  调用 {rec['n_tool_calls']} 次  结果文件 {'有' if rec['result_file'] else '无'}  "
              f"输入 {tk['prompt_tokens']} / 输出 {tk['completion_tokens']} token（单次最大输入 {tk['max_prompt_tokens_single_call']}）  "
              f"{rec['duration_sec']:.0f}s", flush=True)
    print("全部完成" if not n_left else f"本段时间用完，还剩 {n_left} 次没开始", flush=True)
