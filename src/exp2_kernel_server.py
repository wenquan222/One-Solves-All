# TCAD 大修 实验二：Jupyter 内核服务
# 照搬 spa_agent/python_jupyter_kernel_tool.py，改动只有三处：
#   1) 端口由 --port 指定（原版固定 5055），可以同时开多个
#   2) 内核的工作目录由 --cwd 指定（每次运行一个独立目录）
#   3) 新增 /health 与 /shutdown，方便智能体脚本确认就绪与干净退出

from flask import Flask, request, jsonify
from jupyter_client import KernelManager
import argparse
import sys
import os
import atexit
import signal
import threading

import matplotlib
matplotlib.use('Agg')

import warnings
matplotlib.rcParams['font.family'] = 'SimHei'
matplotlib.rcParams['axes.unicode_minus'] = False
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--cwd", required=True)
args = ap.parse_args()

app = Flask(__name__)

km = None
kc = None


def cleanup_kernel():
    global km
    if km:
        print("正在清理 Jupyter Kernel 进程...", flush=True)
        try:
            km.shutdown_kernel(now=True)
        except Exception as e:
            print(f"清理异常: {e}", flush=True)
        km = None
        print("Kernel 已关闭。", flush=True)


atexit.register(cleanup_kernel)


def signal_handler(sig, frame):
    print('\n收到退出信号，正在关闭服务...', flush=True)
    cleanup_kernel()
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

print(f"初始化 Jupyter Kernel Manager，工作目录 {args.cwd}", flush=True)
km = KernelManager()
km.start_kernel(cwd=args.cwd)
kc = km.client()
kc.start_channels()
print("Jupyter Kernel 已就绪。", flush=True)

init_msg_id = kc.execute("""
import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['font.family'] = 'SimHei'
matplotlib.rcParams['axes.unicode_minus'] = False
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")
import matplotlib.pyplot as plt
""")

print("等待 Kernel 初始化完成...", flush=True)
while True:
    try:
        msg = kc.get_iopub_msg(timeout=15)
        if (msg['header']['msg_type'] == 'status' and
                msg['content'].get('execution_state') == 'idle' and
                msg.get('parent_header', {}).get('msg_id') == init_msg_id):
            break
    except Exception:
        break
print("Kernel 初始化消息已清空，队列干净。", flush=True)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "cwd": args.cwd})


@app.route('/shutdown', methods=['POST'])
def shutdown():
    def _exit():
        cleanup_kernel()
        os._exit(0)
    threading.Timer(0.3, _exit).start()
    return jsonify({"ok": True})


@app.route('/execute', methods=['POST'])
def execute_code():
    data = request.json
    code = data.get("code", "")

    if not code:
        return jsonify({"error": "No code provided"}), 400

    try:
        print("\n===================== 收到代码执行请求 =====================", flush=True)
        print(code, flush=True)
        print("============================================================\n", flush=True)

        drained = 0
        while True:
            try:
                kc.get_iopub_msg(timeout=0.5)
                drained += 1
            except Exception:
                break
        if drained > 0:
            print(f"清理了 {drained} 条残留消息", flush=True)

        msg_id = kc.execute(code)
        print(f"执行请求已发送, msg_id={msg_id}", flush=True)

        outputs = []
        idle_received = False

        while not idle_received:
            try:
                msg = kc.get_iopub_msg(timeout=300)

                parent_msg_id = msg.get('parent_header', {}).get('msg_id')
                if parent_msg_id != msg_id:
                    print(f"跳过不匹配的消息: {msg['header']['msg_type']}", flush=True)
                    continue

                msg_type = msg['header']['msg_type']
                content = msg.get('content', {})

                if msg_type == 'status':
                    if content.get('execution_state') == 'idle':
                        idle_received = True
                        print("收到 idle 状态，执行完成", flush=True)
                    continue

                if msg_type == 'stream':
                    text = content.get('text', '')
                    outputs.append({'type': 'stream', 'content': text})
                    print(f"[Stream]: {text.strip()}", flush=True)

                elif msg_type in ('display_data', 'execute_result'):
                    data_dict = content.get('data', {})
                    if 'image/png' in data_dict:
                        outputs.append({'type': 'image/png', 'content': data_dict['image/png']})
                        print("[Image Generated]", flush=True)
                    elif 'text/plain' in data_dict:
                        text = data_dict['text/plain']
                        outputs.append({'type': 'text', 'content': text})
                        print(f"[Result]: {text[:100]}...", flush=True)

                elif msg_type == 'error':
                    traceback_content = "\n".join(content.get('traceback', []))
                    outputs.append({'type': 'error', 'content': traceback_content})
                    print(f"[Error]: {traceback_content[:200]}...", flush=True)

            except Exception as e:
                print(f"消息接收异常: {e}", flush=True)
                break

        if not outputs:
            outputs.append({'type': 'text', 'content': '(代码执行完成，无输出)'})

        print(f"代码执行完成，返回 {len(outputs)} 条输出。", flush=True)
        return jsonify({"outputs": outputs})

    except Exception as e:
        print(f"Server Error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"服务器内部错误: {str(e)}"}), 500


if __name__ == '__main__':
    print(f"正在启动 Flask 服务 (Port: {args.port})...", flush=True)
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True, use_reloader=False)
