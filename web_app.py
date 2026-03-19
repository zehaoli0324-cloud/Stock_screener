"""
A股选股器 - 网页版
运行此文件后，手机浏览器访问 http://电脑IP:5000 即可使用
"""

import os
import sys
import threading
import queue
import json
import pathlib
from datetime import datetime
from flask import Flask, render_template_string, Response, jsonify

app = Flask(__name__)

# ── 复用原有选股逻辑（内联，避免import路径问题）──
for _k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "FTP_PROXY", "ftp_proxy"]:
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

import requests, requests.adapters, urllib3
urllib3.disable_warnings()

_orig_send = requests.adapters.HTTPAdapter.send
def _force_direct(self, request, **kwargs):
    kwargs["proxies"] = {"http": None, "https": None, "ftp": None, "no": "*"}
    kwargs["verify"]  = False
    return _orig_send(self, request, **kwargs)
requests.adapters.HTTPAdapter.send = _force_direct

_orig_init = requests.Session.__init__
def _patch_init(self, *a, **kw):
    _orig_init(self, *a, **kw)
    self.trust_env = False
    self.verify    = False
requests.Session.__init__ = _patch_init

import akshare as ak
import pandas as pd
import numpy as np
import time, warnings
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

# 导入原有选股逻辑
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from a_share_screener import (
    get_hs300_change, get_spot_data, score_stock
)

# ── 全局状态 ──
task_status = {"running": False, "results": None, "error": None}
log_queue = queue.Queue()

HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股选股器</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, sans-serif; background: #0f1117; color: #e0e0e0; min-height: 100vh; }
  .header { background: linear-gradient(135deg, #1a1f2e, #252a3a); padding: 20px; text-align: center; border-bottom: 1px solid #2a3040; }
  .header h1 { font-size: 22px; color: #4fc3f7; letter-spacing: 2px; }
  .header p { font-size: 12px; color: #666; margin-top: 6px; }
  .container { max-width: 900px; margin: 0 auto; padding: 16px; }
  .btn { display: block; width: 100%; padding: 16px; background: linear-gradient(135deg, #1565c0, #1976d2);
         color: white; border: none; border-radius: 12px; font-size: 18px; font-weight: bold;
         cursor: pointer; margin: 16px 0; letter-spacing: 1px; transition: all 0.2s; }
  .btn:active { transform: scale(0.97); }
  .btn:disabled { background: #333; color: #666; cursor: not-allowed; }
  .btn.running { background: linear-gradient(135deg, #6a1b9a, #7b1fa2); }
  .log-box { background: #1a1f2e; border: 1px solid #2a3040; border-radius: 10px;
             padding: 12px; height: 200px; overflow-y: auto; font-size: 12px;
             font-family: monospace; color: #a0a8b8; margin-bottom: 16px; }
  .log-box p { margin: 2px 0; line-height: 1.6; }
  .log-box p.err { color: #ef5350; }
  .results { margin-top: 8px; }
  .card { background: #1a1f2e; border: 1px solid #2a3040; border-radius: 12px;
          padding: 14px; margin-bottom: 10px; }
  .card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .stock-name { font-size: 16px; font-weight: bold; color: #fff; }
  .stock-code { font-size: 12px; color: #666; margin-left: 6px; }
  .score-badge { background: #1565c0; color: #fff; border-radius: 20px;
                 padding: 3px 10px; font-size: 13px; font-weight: bold; }
  .score-badge.high { background: #c62828; }
  .metrics { display: flex; flex-wrap: wrap; gap: 8px; margin: 8px 0; }
  .metric { background: #252a3a; border-radius: 6px; padding: 4px 10px; font-size: 12px; }
  .metric span { color: #4fc3f7; font-weight: bold; }
  .tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .tag { background: #1b3a2a; color: #66bb6a; border-radius: 4px; padding: 2px 8px; font-size: 11px; }
  .pct-up { color: #ef5350; }
  .pct-dn { color: #66bb6a; }
  .empty { text-align: center; padding: 40px; color: #555; }
  .summary { background: #1a2a1a; border: 1px solid #2a402a; border-radius: 10px;
             padding: 12px 16px; margin-bottom: 16px; color: #81c784; font-size: 14px; }
  .hs300 { font-size: 12px; color: #888; text-align: center; margin-bottom: 12px; }
</style>
</head>
<body>
<div class="header">
  <h1>📈 A股多条件选股器</h1>
  <p>每日自动评分，输出得分最高前20只</p>
</div>
<div class="container">
  <button class="btn" id="runBtn" onclick="startScan()">🔍 开始选股</button>
  <div class="log-box" id="logBox"><p>点击"开始选股"启动分析...</p></div>
  <div id="results"></div>
</div>

<script>
let evtSource = null;

function startScan() {
  const btn = document.getElementById('runBtn');
  const logBox = document.getElementById('logBox');
  const results = document.getElementById('results');

  btn.disabled = true;
  btn.textContent = '⏳ 分析中，请耐心等待...';
  btn.classList.add('running');
  logBox.innerHTML = '';
  results.innerHTML = '';

  fetch('/start', {method:'POST'}).then(r => r.json()).then(d => {
    if (d.status === 'already_running') {
      addLog('已有任务运行中，请稍候...', false);
    }
    listenLogs();
  });
}

function listenLogs() {
  evtSource = new EventSource('/stream');
  evtSource.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.type === 'log') {
      addLog(data.msg, data.err || false);
    } else if (data.type === 'done') {
      evtSource.close();
      document.getElementById('runBtn').disabled = false;
      document.getElementById('runBtn').textContent = '🔄 重新选股';
      document.getElementById('runBtn').classList.remove('running');
      renderResults(data.results, data.hs300);
    } else if (data.type === 'error') {
      evtSource.close();
      document.getElementById('runBtn').disabled = false;
      document.getElementById('runBtn').textContent = '🔍 重新选股';
      document.getElementById('runBtn').classList.remove('running');
      addLog('❌ 错误：' + data.msg, true);
    }
  };
}

function addLog(msg, isErr) {
  const logBox = document.getElementById('logBox');
  const p = document.createElement('p');
  if (isErr) p.className = 'err';
  p.textContent = msg;
  logBox.appendChild(p);
  logBox.scrollTop = logBox.scrollHeight;
}

function renderResults(results, hs300) {
  const div = document.getElementById('results');
  if (!results || results.length === 0) {
    div.innerHTML = '<div class="empty">今日未发现符合多条件的标的</div>';
    return;
  }
  let html = '';
  html += `<div class="hs300">沪深300当日涨跌幅：<b style="color:${hs300>=0?'#ef5350':'#66bb6a'}">${hs300>=0?'+':''}${hs300.toFixed(2)}%</b></div>`;
  html += `<div class="summary">✅ 共发现 ${results.length} 只符合≥2条件，以下为得分最高前20只</div>`;
  html += '<div class="results">';
  results.forEach((r, i) => {
    const scoreClass = r.得分 >= 5 ? 'score-badge high' : 'score-badge';
    const pctClass = r['涨幅%'] >= 0 ? 'pct-up' : 'pct-dn';
    const tags = r.符合条件.split(' | ').map(t => `<span class="tag">${t}</span>`).join('');
    html += `
    <div class="card">
      <div class="card-header">
        <div><span class="stock-name">${r.名称}</span><span class="stock-code">${r.代码}</span></div>
        <span class="${scoreClass}">${r.得分}/8分</span>
      </div>
      <div class="metrics">
        <div class="metric">涨幅 <span class="${pctClass}">${r['涨幅%']>=0?'+':''}${r['涨幅%']}%</span></div>
        <div class="metric">现价 <span>${r.现价}</span></div>
        <div class="metric">量比 <span>${r.量比}</span></div>
        <div class="metric">换手 <span>${r['换手%']}%</span></div>
        <div class="metric">市值 <span>${r['流通市值亿']}亿</span></div>
      </div>
      <div class="tags">${tags}</div>
    </div>`;
  });
  html += '</div>';
  div.innerHTML = html;
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML)

@app.route("/start", methods=["POST"])
def start():
    if task_status["running"]:
        return jsonify({"status": "already_running"})
    task_status["running"] = True
    task_status["results"] = None
    task_status["error"] = None
    # 清空队列
    while not log_queue.empty():
        log_queue.get_nowait()
    threading.Thread(target=run_screener, daemon=True).start()
    return jsonify({"status": "started"})

def run_screener():
    hs300_chg = 0.0
    try:
        def emit(msg, err=False):
            log_queue.put({"type": "log", "msg": msg, "err": err})

        emit("[1/4] 获取沪深300涨跌幅...")
        hs300_chg = get_hs300_change()
        emit(f"    沪深300：{hs300_chg:+.2f}%")

        emit("[2/4] 获取全市场实时行情...")
        spot = get_spot_data()

        spot = spot[~spot["name"].str.contains("ST|退", na=False)]
        spot = spot[~spot["code"].str.startswith(("688", "8", "4"))]
        spot = spot.dropna(subset=["pct_chg"])
        spot = spot[spot["pct_chg"].between(1.0, 6.0)]
        spot.reset_index(drop=True, inplace=True)
        emit(f"    预筛候选：{len(spot)} 只")

        emit("[3/4] 逐只评分中，请耐心等待...")
        records = []
        total = len(spot)
        for idx, row in enumerate(spot.itertuples(), 1):
            r = row._asdict()
            code = str(r.get("code", "")).zfill(6)
            if idx % 100 == 0 or idx == total:
                emit(f"    进度：{idx}/{total}")
            sc, hits = score_stock(r, hs300_chg)
            if sc >= 2:
                records.append({
                    "代码": code,
                    "名称": r.get("name", ""),
                    "得分": sc,
                    "现价": round(r.get("price", 0) or 0, 2),
                    "涨幅%": round(r.get("pct_chg", 0) or 0, 2),
                    "量比": round(r.get("vol_ratio", 0) or 0, 2),
                    "换手%": round(r.get("turnover", 0) or 0, 2),
                    "流通市值亿": round((r.get("circ_cap", 0) or 0) / 1e8, 1),
                    "符合条件": " | ".join(hits),
                })
            time.sleep(0.08)

        emit("[4/4] 分析完成！")

        df = pd.DataFrame(records)
        if len(df) > 0:
            df.sort_values(["得分", "涨幅%"], ascending=[False, False], inplace=True)
            df = df.head(20).reset_index(drop=True)
            # 保存CSV
            csv_path = pathlib.Path(__file__).parent / "a_share_results.csv"
            df.to_csv(csv_path, index=True, encoding="utf-8-sig")
            emit(f"结果已保存：{csv_path}")

        log_queue.put({
            "type": "done",
            "results": df.to_dict("records") if len(records) > 0 else [],
            "hs300": hs300_chg,
        })

    except Exception as e:
        log_queue.put({"type": "error", "msg": str(e)})
    finally:
        task_status["running"] = False

@app.route("/stream")
def stream():
    def generate():
        while True:
            try:
                item = log_queue.get(timeout=60)
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                if item["type"] in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type':'log','msg':'等待中...'})}\n\n"
    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_render = os.environ.get("RENDER", False)

    if not is_render:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        print("=" * 55)
        print("  A股选股器 - 网页版已启动！")
        print("=" * 55)
        print(f"  电脑访问：http://localhost:{port}")
        print(f"  手机访问：http://{ip}:{port}")
        print(f"  （手机和电脑需在同一个WiFi下）")
        print("=" * 55)
    else:
        print(f"  A股选股器已在 Render 上启动，端口：{port}")

    app.run(host="0.0.0.0", port=port, debug=False)
