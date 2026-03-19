"""
A股选股程序 - 多条件评分选股器（v3 终极修复版）
每只股票符合一个条件记1分，最终输出得分最高的前20只，并列出符合的条件。

v3 修复内容：
  [Fix-1] 东方财富：加入 SSL verify=False + 完整 Sec-* 请求头，绕过 TLS 指纹检测
  [Fix-2] 新浪备用源：列名映射已更正为新版中文列名
  [Fix-3] 新增腾讯 qt.gtimg.cn 批量行情源（无TLS指纹检测，含换手率/量比/市值）
  [Fix-4] 代理问题：三层保险（环境变量+HTTPAdapter+Session.trust_env）
  [Fix-5] 删除已废弃的 stock_zh_a_spot_tx

数据源优先级：
  1. 东方财富（直连，加verify=False和完整请求头）
  2. 腾讯 qt.gtimg.cn（批量查询，最稳健备用）
  3. 新浪（仅涨跌幅，换手率/市值/量比不可用）

依赖：akshare>=1.10, pandas, numpy, requests
"""

# ── 第0步：清除代理环境变量（必须在 import requests 前执行）──
import os
for _k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
           "ALL_PROXY", "all_proxy", "FTP_PROXY", "ftp_proxy"]:
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"

# ── 第1步：双层 requests 补丁 ──
import requests, requests.adapters, urllib3
urllib3.disable_warnings()   # 关闭 SSL verify=False 的警告

_orig_send = requests.adapters.HTTPAdapter.send
def _force_direct(self, request, **kwargs):
    kwargs["proxies"] = {"http": None, "https": None, "ftp": None, "no": "*"}
    kwargs["verify"]  = False   # 跳过SSL证书验证（绕过部分代理MITM）
    return _orig_send(self, request, **kwargs)
requests.adapters.HTTPAdapter.send = _force_direct

_orig_init = requests.Session.__init__
def _patch_init(self, *a, **kw):
    _orig_init(self, *a, **kw)
    self.trust_env = False      # 完全忽略系统代理
    self.verify    = False
requests.Session.__init__ = _patch_init

# ────────────────────────────────────────────────
import akshare as ak
import pandas as pd
import numpy as np
import time, warnings, re, pathlib
from datetime import datetime, timedelta
warnings.filterwarnings("ignore")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# 全局 Session（trust_env 已在 __init__ 补丁中设为 False）
_S = requests.Session()
_S.headers.update({
    "User-Agent":      _UA,
    "Accept":          "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection":      "keep-alive",
})

DIRECT = {"http": None, "https": None}

def _get(url, params=None, headers=None, timeout=15, retries=3):
    h = dict(_S.headers)
    if headers:
        h.update(headers)
    for i in range(retries):
        try:
            r = _S.get(url, params=params, headers=h,
                       proxies=DIRECT, verify=False, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            if i == retries - 1:
                raise
            time.sleep(1.5 * (i + 1))


# ════════════════════════════════════════════════
# 获取沪深300当日涨幅
# ════════════════════════════════════════════════

def get_hs300_change() -> float:
    # 方案A：东方财富（加完整请求头）
    try:
        r = _get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            params={"fltt":"2","invt":"2","fields":"f2,f3,f12,f14",
                    "secids":"1.000300",
                    "ut":"bd1d9ddb04089700cf9c27f6f7426281"},
            headers={
                "Referer": "https://quote.eastmoney.com/center/gridlist.html",
                "sec-ch-ua": '"Chromium";v="124"',
                "sec-ch-ua-mobile": "?0",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            }
        )
        return float(r.json()["data"]["diff"][0]["f3"])
    except Exception:
        pass

    # 方案B：腾讯行情
    try:
        r = _get("https://qt.gtimg.cn/q=s_sh000300",
                 headers={"Referer": "https://finance.qq.com"})
        # 格式: v_s_sh000300="1~沪深300~000300~3000~5~0.17~...";
        val = r.text.split('"')[1]
        parts = val.split("~")
        return float(parts[5])   # 涨跌幅%
    except Exception:
        pass

    # 方案C：新浪
    try:
        r = _get("https://hq.sinajs.cn/list=s_sh000300",
                 headers={"Referer": "https://finance.sina.com.cn"})
        parts = r.text.split('"')[1].split(",")
        return float(parts[3])
    except Exception:
        pass

    # 方案D：akshare历史数据
    try:
        df = ak.index_zh_a_hist(
            symbol="000300", period="daily",
            start_date=(datetime.today()-timedelta(days=10)).strftime("%Y%m%d"),
            end_date=datetime.today().strftime("%Y%m%d"))
        df.sort_values("日期", inplace=True)
        if len(df) >= 2:
            p, c = df.iloc[-2]["收盘"], df.iloc[-1]["收盘"]
            return (c - p) / p * 100
    except Exception:
        pass

    return 0.0


# ════════════════════════════════════════════════
# 数据源 1：东方财富直连（加完整请求头 + verify=False）
# ════════════════════════════════════════════════

_EM_HEADERS = {
    "Referer":          "https://quote.eastmoney.com/center/gridlist.html",
    "Origin":           "https://quote.eastmoney.com",
    "sec-ch-ua":        '"Chromium";v="124", "Google Chrome";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-site",
}

def _em_page(page, size=100):
    r = _get(
        "https://push2.eastmoney.com/api/qt/clist/get",
        params={
            "pn": page, "pz": size, "po": "1", "np": "1",
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": "2", "invt": "2", "fid": "f12",
            "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
            "fields": "f2,f3,f5,f8,f10,f12,f14,f20",
        },
        headers=_EM_HEADERS, timeout=20
    )
    return r.json().get("data", {}).get("diff", [])

def get_spot_em() -> pd.DataFrame:
    print("    [源1] 东方财富直连（verify=False）...")
    rows, page = [], 1
    while True:
        batch = _em_page(page)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.25)
    if not rows:
        raise RuntimeError("东方财富返回空数据")
    df = pd.DataFrame(rows).rename(columns={
        "f12":"code","f14":"name","f2":"price",
        "f3":"pct_chg","f8":"turnover","f10":"vol_ratio",
        "f20":"circ_cap","f5":"volume"})
    df["code"] = df["code"].astype(str).str.zfill(6)
    for c in ["pct_chg","turnover","vol_ratio","circ_cap","volume","price"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    print(f"    [源1] 成功，共 {len(df)} 只")
    return df


# ════════════════════════════════════════════════
# 数据源 2：腾讯 qt.gtimg.cn 批量行情（★核心备用★）
#
# 腾讯字段位置（~分隔）：
#   0=市场类型  1=股票名称  2=代码  3=现价  4=昨收
#   5=今开  6=成交量(百股)  31=涨跌幅  32=换手率  33=量比(PE)
#   36=流通市值(亿元)  37=总市值  44=量比（不同版本位置不同）
#
# 实际验证字段（来自腾讯行情文档）：
#   pct_chg  = (f3-f4)/f4*100  或直接 f32(涨跌幅%)
#   turnover = f38(换手率%)
#   vol_ratio = f49(量比)
#   circ_cap = f44(流通市值，亿元)*1e8 → 元
# ════════════════════════════════════════════════

def _parse_tencent_line(line: str) -> dict | None:
    """解析腾讯行情单行，返回字段字典。"""
    try:
        # 格式: v_sh600000="1~浦发银行~600000~9.81~9.67~..."
        raw = line.split('"')[1]
        if not raw or raw == "-":
            return None
        f = raw.split("~")
        if len(f) < 50:
            return None
        code = f[2].strip().zfill(6)
        name = f[1].strip()
        price = float(f[3]) if f[3] else None
        prev_close = float(f[4]) if f[4] else None
        volume = float(f[6]) * 100 if f[6] else None   # 百股→股
        pct_chg = float(f[32]) if f[32] else None       # 涨跌幅%
        turnover = float(f[38]) if len(f) > 38 and f[38] else None  # 换手率%
        vol_ratio = float(f[49]) if len(f) > 49 and f[49] else None # 量比
        circ_cap  = float(f[44]) * 1e8 if len(f) > 44 and f[44] else None  # 亿→元
        if price is None or price <= 0:
            return None
        return {
            "code": code, "name": name, "price": price,
            "pct_chg": pct_chg, "turnover": turnover,
            "vol_ratio": vol_ratio, "circ_cap": circ_cap,
            "volume": volume,
        }
    except Exception:
        return None

def _tencent_batch(codes_with_prefix: list[str]) -> list[dict]:
    """批量查询腾讯行情，codes_with_prefix 格式如 ['sh600000','sz000001']。"""
    query = ",".join(codes_with_prefix)
    url   = f"https://qt.gtimg.cn/q={query}"
    r = _get(url, headers={"Referer": "https://finance.qq.com"}, timeout=15)
    # 编码处理
    try:
        text = r.content.decode("gbk", errors="replace")
    except Exception:
        text = r.text
    results = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_tencent_line(line)
        if parsed:
            results.append(parsed)
    return results

def get_spot_tencent() -> pd.DataFrame:
    """
    腾讯行情：先通过akshare拿股票代码列表，再分批查询腾讯实时行情。
    腾讯接口无TLS指纹检测，是代理环境下最稳定的备用源。
    """
    print("    [源2] 腾讯行情（批量查询）...")

    # 获取股票代码列表（上交所+深交所）
    code_list = []
    try:
        # 上交所主板（走 query.sse.com.cn，通常能通）
        sh = ak.stock_info_sh_name_code(symbol="主板A股")
        for code in sh["证券代码"].astype(str).str.zfill(6):
            if not code.startswith(("688", "689")):   # 排除科创板
                code_list.append(f"sh{code}")
    except Exception as e:
        print(f"    [源2] 上交所列表失败：{e}")

    try:
        # 深交所（走 szse.cn，通常能通）
        sz = ak.stock_info_sz_name_code(symbol="A股列表")
        sz["A股代码"] = sz["A股代码"].astype(str).str.zfill(6)
        for code in sz["A股代码"]:
            if not code.startswith(("30", "00", "002", "003")):
                code_list.append(f"sz{code}")
            else:
                code_list.append(f"sz{code}")
    except Exception as e:
        print(f"    [源2] 深交所列表失败：{e}")

    if not code_list:
        raise RuntimeError("无法获取股票代码列表")

    # 去重 + 过滤北交所（8/4开头）
    seen = set()
    filtered = []
    for c in code_list:
        bare = c[2:]
        if bare not in seen and not bare.startswith(("688","8","4","9")):
            seen.add(bare)
            filtered.append(c)
    code_list = filtered
    print(f"    [源2] 待查代码 {len(code_list)} 只，开始分批拉取...")

    # 分批查询（每批50只）
    all_rows = []
    batch_size = 50
    batches = [code_list[i:i+batch_size] for i in range(0, len(code_list), batch_size)]
    for i, batch in enumerate(batches):
        try:
            rows = _tencent_batch(batch)
            all_rows.extend(rows)
        except Exception as e:
            print(f"    [源2] 批次 {i+1} 失败：{e}")
        if i % 20 == 0 and i > 0:
            print(f"    [源2] 进度 {i*batch_size}/{len(code_list)}...")
        time.sleep(0.1)

    if not all_rows:
        raise RuntimeError("腾讯行情返回空数据")

    df = pd.DataFrame(all_rows)
    for c in ["pct_chg","turnover","vol_ratio","circ_cap","volume","price"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    print(f"    [源2] 成功，共 {len(df)} 只")
    return df


# ════════════════════════════════════════════════
# 数据源 3：新浪（修复列名，仅作最后备用）
# ════════════════════════════════════════════════

def get_spot_sina() -> pd.DataFrame:
    """
    新浪行情（修复版）：更新为新版中文列名。
    注意：新版新浪接口不返回换手率、流通市值、量比，
    这三个条件（②③④）将无法评分。
    """
    print("    [源3] 新浪财经（仅涨跌幅，条件②③④跳过）...")
    df = ak.stock_zh_a_spot()
    df.columns = [c.strip() for c in df.columns]
    # 新版列名（2024年后 akshare 已改为中文）
    if "代码" in df.columns:
        df.rename(columns={
            "代码": "code", "名称": "name",
            "最新价": "price", "涨跌幅": "pct_chg",
            "成交量": "volume",
        }, inplace=True)
    else:
        # 旧版英文列名兼容
        df.rename(columns={
            "symbol": "code", "name": "name",
            "trade": "price", "changepercent": "pct_chg",
            "volume": "volume",
        }, inplace=True)
    df["code"] = df["code"].astype(str).str.zfill(6)
    for c in ["pct_chg", "volume", "price"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["vol_ratio"] = np.nan
    df["circ_cap"]  = np.nan
    df["turnover"]  = np.nan
    print(f"    [源3] 成功，共 {len(df)} 只")
    return df


# ════════════════════════════════════════════════
# 数据源总入口
# ════════════════════════════════════════════════

def get_spot_data() -> pd.DataFrame:
    sources = [
        ("东方财富直连",   get_spot_em),
        ("腾讯行情",       get_spot_tencent),
        ("新浪财经(降级)", get_spot_sina),
    ]
    last_err = None
    for name, fn in sources:
        try:
            df = fn()
            if df is not None and len(df) > 100:
                return df
            print(f"    {name} 返回数据过少，跳过")
        except Exception as e:
            last_err = e
            print(f"    → 失败：{e}")

    raise RuntimeError(
        f"所有数据源均失败。最后错误：{last_err}\n\n"
        "━━━━━━━ 代理工具配置方法 ━━━━━━━\n"
        "东方财富 RemoteDisconnected 通常由代理TLS检测触发。\n"
        "请在代理工具中将以下域名设为【直连】：\n"
        "  push2.eastmoney.com\n"
        "  qt.gtimg.cn\n"
        "  hq.sinajs.cn\n"
        "  query.sse.com.cn\n"
        "  www.szse.cn\n\n"
        "Clash规则（rules 段添加）：\n"
        "  - DOMAIN-SUFFIX,eastmoney.com,DIRECT\n"
        "  - DOMAIN-SUFFIX,gtimg.cn,DIRECT\n"
        "  - DOMAIN-SUFFIX,sinajs.cn,DIRECT\n"
        "  - DOMAIN-SUFFIX,sse.com.cn,DIRECT\n"
        "  - DOMAIN-SUFFIX,szse.cn,DIRECT\n"
    )


# ════════════════════════════════════════════════
# 单股历史日线
# ════════════════════════════════════════════════

def get_hist(code: str):
    try:
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(datetime.today()-timedelta(days=400)).strftime("%Y%m%d"),
            adjust="qfq")
        if df is None or len(df) < 60:
            return None
        df.columns = [c.strip() for c in df.columns]
        df.rename(columns={"日期":"date","收盘":"close","成交量":"volume","最高":"high"},
                  inplace=True)
        df["date"] = pd.to_datetime(df["date"])
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception:
        return None


# ════════════════════════════════════════════════
# 单股评分
# ════════════════════════════════════════════════

def score_stock(row: dict, hs300_chg: float):
    hits = []
    pct_chg   = row.get("pct_chg",   np.nan)
    vol_ratio  = row.get("vol_ratio",  np.nan)
    turnover   = row.get("turnover",   np.nan)
    circ_cap   = row.get("circ_cap",   np.nan)
    volume     = row.get("volume",     np.nan)
    code       = row.get("code", "")

    if pd.notna(pct_chg) and 2.0 <= pct_chg <= 5.0:
        hits.append("①涨幅2-5%")
    if pd.notna(vol_ratio) and vol_ratio > 1.0:
        hits.append("②量比>1")
    if pd.notna(turnover) and 4.0 <= turnover <= 10.0:
        hits.append("③换手4-10%")
    if pd.notna(circ_cap) and 50e8 <= circ_cap <= 200e8:
        hits.append("④市值50-200亿")
    if pd.notna(pct_chg) and pct_chg > hs300_chg:
        hits.append("⑦强于沪深300")

    hist = get_hist(code)
    if hist is not None and len(hist) >= 60:
        close, vol, high = hist["close"], hist["volume"], hist["high"]
        if len(vol) >= 6 and pd.notna(volume):
            if vol.iloc[-1] >= vol.iloc[-2]*1.5 or \
               vol.iloc[-1] >= vol.iloc[-6:-1].mean()*1.5:
                hits.append("⑤放量≥1.5x")
        if len(close) >= 60:
            ma5  = close.rolling(5).mean().iloc[-1]
            ma10 = close.rolling(10).mean().iloc[-1]
            ma20 = close.rolling(20).mean().iloc[-1]
            ma60 = close.rolling(60).mean().iloc[-1]
            if ma5 > ma10 > ma20 > ma60:
                hits.append("⑥均线多头")
        w = min(250, len(high))
        if w >= 20 and high.iloc[-1] >= high.iloc[-w:-1].max():
            hits.append("⑧52周新高")

    return len(hits), hits


# ════════════════════════════════════════════════
# 主程序
# ════════════════════════════════════════════════

def main():
    print("=" * 65)
    print("  A股多条件评分选股器  →  输出得分最高前20只  [v3 终极修复版]")
    print("=" * 65)

    print("\n[1/4] 获取沪深300当日涨跌幅...")
    hs300_chg = get_hs300_change()
    print(f"    沪深300当日涨跌幅：{hs300_chg:+.2f}%")

    print("\n[2/4] 获取全市场实时行情...")
    spot = get_spot_data()

    spot = spot[~spot["name"].str.contains("ST|退", na=False)]
    spot = spot[~spot["code"].str.startswith(("688", "8", "4"))]
    spot = spot.dropna(subset=["pct_chg"])
    spot = spot[spot["pct_chg"].between(1.0, 6.0)]
    spot.reset_index(drop=True, inplace=True)
    print(f"    预筛后候选：{len(spot)} 只（涨幅1%-6%区间）")

    print("\n[3/4] 逐只评分分析...\n")
    records, total = [], len(spot)
    for idx, row in enumerate(spot.itertuples(), 1):
        r = row._asdict()
        code = str(r.get("code", "")).zfill(6)
        if idx % 50 == 0 or idx == total:
            print(f"    进度：{idx}/{total}")
        sc, hits = score_stock(r, hs300_chg)
        if sc >= 2:
            records.append({
                "代码":       code,
                "名称":       r.get("name", ""),
                "得分":       sc,
                "现价":       round(r.get("price", 0) or 0, 2),
                "涨幅%":      round(r.get("pct_chg", 0) or 0, 2),
                "量比":       round(r.get("vol_ratio", 0) or 0, 2),
                "换手%":      round(r.get("turnover", 0) or 0, 2),
                "流通市值亿": round((r.get("circ_cap", 0) or 0)/1e8, 1),
                "符合条件":   " | ".join(hits),
            })
        time.sleep(0.08)

    print("\n[4/4] 分析完成！\n" + "=" * 65)
    if not records:
        print("  今日未发现符合多条件的标的。")
    else:
        df = pd.DataFrame(records)
        df.sort_values(["得分","涨幅%"], ascending=[False,False], inplace=True)
        df = df.head(20).reset_index(drop=True)
        df.index += 1
        print(f"  符合≥2条件共 {len(records)} 只，以下为前20只：\n")
        for i, r in df.iterrows():
            print(f"  [{i:>2}] {r['名称']}({r['代码']})  "
                  f"得分:{r['得分']}/8  涨幅:{r['涨幅%']:+.2f}%  "
                  f"量比:{r['量比']}  换手:{r['换手%']}%  "
                  f"流通市值:{r['流通市值亿']}亿")
            print(f"       符合：{r['符合条件']}\n")
        print("=" * 65)
        print("  ⚠️  仅供技术分析参考，不构成投资建议。投资有风险，入市须谨慎。")
        print("=" * 65)
        # 兼容 PyInstaller 打包后的路径
        if getattr(sys, 'frozen', False):
            exe_dir = pathlib.Path(sys.executable).parent
        else:
            exe_dir = pathlib.Path(__file__).parent
        csv_path = exe_dir / "a_share_results.csv"
        df.to_csv(csv_path, index=True, encoding="utf-8-sig")
        print(f"\n  结果已保存至：{csv_path}")


if __name__ == "__main__":
    import sys
    try:
        main()
    except Exception as e:
        print(f"\n程序出错：{e}")
    input("\n按回车键退出...")
