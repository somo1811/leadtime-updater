#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Amazon 出品者出荷リードタイム一括更新 Webサービス (自社専用)

必要な環境変数:
  APP_PASSWORD       画面にログインするためのパスワード(8文字以上)
  SP_CLIENT_ID       LWAクライアントID
  SP_CLIENT_SECRET   LWAクライアントシークレット
  SP_REFRESH_TOKEN   リフレッシュトークン
  SP_SELLER_ID       出品者ID(出品者トークン)
任意:
  SP_MARKETPLACE_ID  既定 A1VC38T7YXB528 (日本)
  PORT               既定 8080
  DEMO_MODE=1        Amazonに接続せず、ダミーデータで画面の動作だけ確認する

Python 3.8以上。追加ライブラリ不要。
"""
import csv
import gzip
import hmac
import io
import json
import os
import random
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ENDPOINT = "https://sellingpartnerapi-fe.amazon.com"   # 極東リージョン(日本)
TOKEN_URL = "https://api.amazon.com/auth/o2/token"
REPORT_TYPE = "GET_MERCHANT_LISTINGS_ALL_DATA"
SKU_HEADERS = ["seller-sku", "出品者SKU", "sku"]
CHANNEL_HEADERS = ["fulfillment-channel", "フルフィルメント・チャンネル",
                   "フルフィルメントチャンネル", "フルフィルメント・チャネル"]
INTERVAL = 0.25          # 1件ごとの待ち時間(秒)。API上限は毎秒5件
SESSION_HOURS = 12
DEMO = os.environ.get("DEMO_MODE") == "1"


# ───────────────────────── SP-API ─────────────────────────
class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class Cancelled(Exception):
    pass


def http(method, url, headers=None, body=None, retries=6):
    data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                raw = res.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise ApiError(e.code, text)
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise


class SpApi:
    def __init__(self):
        env = os.environ
        self.client_id = env["SP_CLIENT_ID"]
        self.client_secret = env["SP_CLIENT_SECRET"]
        self.refresh_token = env["SP_REFRESH_TOKEN"]
        self.seller_id = env["SP_SELLER_ID"]
        self.marketplace = env.get("SP_MARKETPLACE_ID") or "A1VC38T7YXB528"
        self._token, self._token_exp = None, 0
        self._lock = threading.Lock()

    def token(self):
        with self._lock:
            if self._token and time.time() < self._token_exp - 60:
                return self._token
            form = urllib.parse.urlencode({
                "grant_type": "refresh_token", "refresh_token": self.refresh_token,
                "client_id": self.client_id, "client_secret": self.client_secret}).encode()
            res = http("POST", TOKEN_URL,
                       {"Content-Type": "application/x-www-form-urlencoded"}, form)
            self._token = res["access_token"]
            self._token_exp = time.time() + int(res.get("expires_in", 3600))
            return self._token

    def call(self, method, path, query=None, body=None):
        url = ENDPOINT + path + ("?" + urllib.parse.urlencode(query) if query else "")
        return http(method, url, {"x-amz-access-token": self.token(),
                                  "Content-Type": "application/json",
                                  "User-Agent": "LeadTimeUpdaterWeb/1.0 (Language=Python)"}, body)

    def fetch_mfn_skus(self, cancel):
        rid = self.call("POST", "/reports/2021-06-30/reports", body={
            "reportType": REPORT_TYPE, "marketplaceIds": [self.marketplace]})["reportId"]
        for _ in range(120):
            if cancel.is_set():
                raise Cancelled()
            rep = self.call("GET", f"/reports/2021-06-30/reports/{rid}")
            status = rep.get("processingStatus")
            if status == "DONE":
                break
            if status in ("CANCELLED", "FATAL"):
                raise RuntimeError(f"出品レポートの作成に失敗しました({status})。"
                                   "時間をおいて再実行してください。")
            cancel.wait(10)
        else:
            raise RuntimeError("出品レポートの作成がタイムアウトしました。")
        doc = self.call("GET", f"/reports/2021-06-30/documents/{rep['reportDocumentId']}")
        with urllib.request.urlopen(doc["url"], timeout=120) as res:
            raw = res.read()
        if doc.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        return parse_listing_report(raw)

    def patch_lead_time(self, sku, days, preview=False):
        query = {"marketplaceIds": self.marketplace, "issueLocale": "ja_JP"}
        body = {"productType": "PRODUCT", "patches": [{
            "op": "merge",                       # 在庫数など他の項目は変更しない
            "path": "/attributes/fulfillment_availability",
            "value": [{"fulfillment_channel_code": "DEFAULT",
                       "lead_time_to_ship_max_days": days}]}]}
        path = f"/listings/2021-08-01/items/{self.seller_id}/{urllib.parse.quote(sku, safe='')}"
        return self.call("PATCH", path, query, body)


    def check_item(self, sku):
        """変更せず、SKUが存在するかと現在の在庫数・リードタイムを読み取る。"""
        path = f"/listings/2021-08-01/items/{self.seller_id}/{urllib.parse.quote(sku, safe='')}"
        item = self.call("GET", path, {"marketplaceIds": self.marketplace,
                                       "includedData": "summaries,attributes"})
        fa = item.get("attributes", {}).get("fulfillment_availability", [])
        if not fa:
            return {"status": "VALID", "issues": [], "info": "現在: 設定情報なし(標準のリードタイム)"}
        parts = []
        for x in fa:
            lt = x.get("lead_time_to_ship_max_days")
            parts.append(f"在庫 {x.get('quantity', '-')} / リードタイム "
                         + (f"{lt}日" if lt is not None else "未設定(標準)"))
        return {"status": "VALID", "issues": [], "info": "現在: " + "、".join(parts)}


class DemoApi:
    """DEMO_MODE=1 のときに使うダミー。Amazonには一切接続しない。"""
    def fetch_mfn_skus(self, cancel):
        cancel.wait(2)
        return [f"DEMO-SKU-{n:04d}" for n in range(1, 61)]

    def check_item(self, sku):
        if sku.endswith("7"):
            raise ApiError(404, "デモ用: SKUが見つかりません")
        return {"status": "VALID", "issues": [], "info": "現在: 在庫 5 / リードタイム 2日"}

    def patch_lead_time(self, sku, days, preview):
        if sku.endswith("7"):
            return {"status": "INVALID", "issues": [
                {"severity": "ERROR", "message": "デモ用のエラーです"}]}
        return {"status": "VALID" if preview else "ACCEPTED", "issues": []}


def parse_listing_report(raw):
    text = None
    for enc in ("utf-8-sig", "cp932"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("cp932", "replace")
    rows = list(csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE))
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    lower = [h.lower() for h in header]

    def find(cands):
        return next((lower.index(c.lower()) for c in cands if c.lower() in lower), None)

    i_sku, i_ch = find(SKU_HEADERS), find(CHANNEL_HEADERS)
    if i_sku is None or i_ch is None:
        raise RuntimeError("出品レポートの列名を認識できませんでした。見出し: " + " | ".join(header))
    skus, seen = [], set()
    for r in rows[1:]:
        if len(r) <= max(i_sku, i_ch):
            continue
        sku, ch = r[i_sku].strip(), r[i_ch].strip().upper()
        if sku and not ch.startswith("AMAZON") and sku not in seen:   # FBAを除外
            seen.add(sku)
            skus.append(sku)
    return skus


# ───────────────────────── ジョブ管理(同時に1つだけ) ─────────────────────────
LOCK = threading.Lock()
CANCEL = threading.Event()
JOB = {"phase": "idle"}      # idle / report / confirm / updating / done / error / cancelled
SKUS, RESULTS = [], []
API = None


def set_job(**kw):
    with LOCK:
        JOB.update(kw)


def explain(e):
    if isinstance(e, ApiError):
        if e.status in (400, 401) and "invalid_" in e.body:
            return "Amazonの認証に失敗しました。環境変数の認証情報(クライアントID・シークレット・リフレッシュトークン)を確認してください。"
        if e.status == 403:
            return "Amazonにアクセスを拒否されました。アプリのロール(商品の出品／在庫と注文の追跡)と出品者IDを確認してください。"
        return f"Amazon APIエラー: {e}"
    return str(e) or e.__class__.__name__


def prepare(test_sku):
    try:
        skus = [test_sku] if test_sku else API.fetch_mfn_skus(CANCEL)
        if CANCEL.is_set():
            raise Cancelled()
        if not skus:
            raise RuntimeError("出品者出荷のSKUが見つかりませんでした。")
        with LOCK:
            SKUS[:] = skus
            JOB.update(phase="confirm", total=len(skus))
    except Cancelled:
        set_job(phase="cancelled", message="中止しました。Amazonへの変更は行っていません。")
    except Exception as e:
        set_job(phase="error", message=explain(e))


def update():
    days, dry = JOB["days"], JOB["dry_run"]
    ok = ng = 0
    failures, samples = [], []
    try:
        for n, sku in enumerate(list(SKUS), 1):
            if CANCEL.is_set():
                raise Cancelled()
            try:
                res = API.check_item(sku) if dry else API.patch_lead_time(sku, days)
                status = res.get("status", "?")
                issues = " / ".join(f"[{i.get('severity')}] {i.get('message')}"
                                    for i in res.get("issues", []))
            except ApiError as e:
                if e.status in (401, 403) and n == 1:
                    raise
                status, issues = f"HTTP{e.status}", e.body[:300]
            good = status in ("ACCEPTED", "VALID")
            ok, ng = ok + good, ng + (not good)
            if dry and good:
                issues = res.get("info", "")
                if len(samples) < 5:
                    samples.append({"sku": sku, "info": issues})
            RESULTS.append((sku, days, status, issues))
            if not good and len(failures) < 200:
                failures.append({"sku": sku, "status": status, "issues": issues[:300]})
            set_job(done=n, ok=ok, ng=ng, failures=failures, samples=samples)
            CANCEL.wait(INTERVAL)
        set_job(phase="done", finished=time.time())
    except Cancelled:
        set_job(phase="cancelled", message=f"中止しました。{ok}件は処理済みです。")
    except Exception as e:
        set_job(phase="error", message=explain(e))


def start(days, dry_run, test_sku):
    with LOCK:
        if JOB["phase"] in ("report", "confirm", "updating"):
            return False
        JOB.clear()
        JOB.update(phase="report", days=days, dry_run=dry_run, test_sku=test_sku,
                   total=0, done=0, ok=0, ng=0, failures=[], samples=[], message="")
        SKUS.clear()
        RESULTS.clear()
        CANCEL.clear()
    threading.Thread(target=prepare, args=(test_sku,), daemon=True).start()
    return True


def confirm():
    with LOCK:
        if JOB["phase"] != "confirm":
            return False
        JOB["phase"] = "updating"
    threading.Thread(target=update, daemon=True).start()
    return True


def cancel():
    with LOCK:
        if JOB["phase"] == "confirm":
            JOB.update(phase="cancelled", message="中止しました。Amazonへの変更は行っていません。")
        elif JOB["phase"] in ("report", "updating"):
            CANCEL.set()


# ───────────────────────── ログイン ─────────────────────────
PASSWORD = os.environ.get("APP_PASSWORD", "")
SESSIONS = {}            # token -> 有効期限
LOGIN_FAILS = []


def logged_in(handler):
    for part in (handler.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "sid":
            with LOCK:
                exp = SESSIONS.get(v)
            return bool(exp and exp > time.time())
    return False


# ───────────────────────── HTTP ─────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "LeadTime"
    sys_version = ""

    def log_message(self, fmt, *args):
        if "/api/status" not in str(args[0] if args else ""):
            sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send(self, code, body=b"", ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def cookie(self, value, max_age):
        host = (self.headers.get("Host") or "").split(":")[0]
        secure = "" if host in ("localhost", "127.0.0.1") else " Secure;"
        return {"Set-Cookie": f"sid={value}; Path=/; HttpOnly;{secure} SameSite=Strict; Max-Age={max_age}"}

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if 0 < n <= 10000 else b""

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self.send(200, "ok", "text/plain")
        if path == "/":
            return self.send(200, PAGE if logged_in(self) else LOGIN.replace("{{error}}", ""))
        if not logged_in(self):
            return self.json({"error": "login"}, 401)
        if path == "/api/status":
            with LOCK:
                return self.json(dict(JOB, demo=DEMO))
        if path == "/api/result.csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["sku", "days", "status", "issues"])
            w.writerows(list(RESULTS))
            return self.send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                             {"Content-Disposition": 'attachment; filename="leadtime_result.csv"'})
        self.send(404, "Not Found", "text/plain")

    def do_POST(self):
        path = self.path.split("?")[0]
        raw = self.body()
        if path == "/login":
            now = time.time()
            with LOCK:
                LOGIN_FAILS[:] = [t for t in LOGIN_FAILS if now - t < 900]
                locked = len(LOGIN_FAILS) >= 10
            if locked:
                return self.send(429, LOGIN.replace(
                    "{{error}}", "試行回数が多すぎます。15分ほど待ってからやり直してください。"))
            pw = urllib.parse.parse_qs(raw.decode("utf-8", "replace")).get("password", [""])[0]
            if hmac.compare_digest(pw.encode(), PASSWORD.encode()):
                sid = secrets.token_urlsafe(32)
                with LOCK:
                    SESSIONS[sid] = now + SESSION_HOURS * 3600
                return self.send(303, extra=dict(self.cookie(sid, SESSION_HOURS * 3600), Location="/"))
            with LOCK:
                LOGIN_FAILS.append(now)
            time.sleep(1)
            return self.send(401, LOGIN.replace("{{error}}", "パスワードが違います。"))

        # 以降はログイン必須。別サイトからの送信を拒否する
        if not logged_in(self) or self.headers.get("X-Requested-With") != "leadtime":
            return self.json({"error": "login"}, 401)
        if path == "/logout":
            with LOCK:
                SESSIONS.clear()
            return self.send(200, "{}", "application/json", self.cookie("", 0))
        if path == "/api/start":
            try:
                d = json.loads(raw or b"{}")
                days = int(d.get("days"))
            except (ValueError, TypeError):
                return self.json({"error": "日数を数字で入力してください。"}, 400)
            if not 1 <= days <= 30:
                return self.json({"error": "日数は1〜30で指定してください。"}, 400)
            sku = str(d.get("test_sku") or "").strip()[:100]
            if not start(days, bool(d.get("dry_run")), sku):
                return self.json({"error": "別の処理が実行中です。"}, 409)
            return self.json({"ok": True})
        if path == "/api/confirm":
            return self.json({"ok": True}) if confirm() else self.json(
                {"error": "確認待ちの処理がありません。"}, 409)
        if path == "/api/cancel":
            cancel()
            return self.json({"ok": True})
        self.send(404, "Not Found", "text/plain")


# ───────────────────────── 画面 ─────────────────────────
STYLE = """
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"Hiragino Sans","Yu Gothic UI",Meiryo,sans-serif;
 background:#f3f4f1;color:#1d2420;line-height:1.7;font-size:15px}
main{max-width:640px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:21px;margin:0 0 4px;letter-spacing:.02em}
.sub{color:#5d6861;margin:0 0 28px;font-size:13.5px}
.card{background:#fff;border:1px solid #dfe3dc;border-radius:10px;padding:24px;margin-bottom:16px}
label{display:block;font-weight:600;font-size:13.5px;margin-bottom:6px}
.hint{font-weight:400;color:#5d6861;font-size:12.5px;margin:4px 0 0}
input[type=number],input[type=text],input[type=password]{width:100%;padding:10px 12px;font-size:16px;
 border:1px solid #b9c1b8;border-radius:7px;background:#fff;color:inherit;font-family:inherit}
input:focus-visible,button:focus-visible{outline:2px solid #1f6b4a;outline-offset:2px}
.days{display:flex;align-items:center;gap:10px}
.days input{width:110px;font-size:22px;font-weight:600;text-align:center}
.field{margin-bottom:20px}
.check{display:flex;gap:9px;align-items:flex-start;font-weight:400;font-size:14px}
.check input{margin-top:5px;width:16px;height:16px;accent-color:#1f6b4a}
button{font:inherit;font-weight:600;padding:11px 20px;border-radius:7px;border:1px solid transparent;cursor:pointer}
.primary{background:#1f6b4a;color:#fff}
.primary:hover{background:#18573c}
.ghost{background:#fff;border-color:#b9c1b8;color:#1d2420}
.ghost:hover{background:#f3f4f1}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}
.big{font-size:17px;font-weight:600;margin:0 0 6px}
.bar{height:10px;background:#e6e9e3;border-radius:5px;overflow:hidden;margin:14px 0 8px}
.bar div{height:100%;background:#1f6b4a;width:0;transition:width .4s}
.nums{font-variant-numeric:tabular-nums;color:#5d6861;font-size:13.5px}
.err{color:#a3261b}
.note{background:#fbf3dc;border:1px solid #e9d596;border-radius:7px;padding:10px 14px;font-size:13.5px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:12.5px;margin-top:12px}
th,td{text-align:left;padding:6px 8px;border-top:1px solid #e6e9e3;vertical-align:top;word-break:break-all}
th{color:#5d6861;font-weight:600}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.link{background:none;border:none;color:#5d6861;padding:4px;font-weight:400;font-size:13px;text-decoration:underline}
a{color:#1f6b4a}
[hidden]{display:none!important}
"""

LOGIN = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>リードタイム一括更新</title><style>""" + STYLE + """</style></head><body><main>
<h1>リードタイム一括更新</h1><p class="sub">ログインしてください</p>
<form class="card" method="post" action="/login">
<div class="field"><label for="pw">パスワード</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required></div>
<p class="err" role="alert">{{error}}</p>
<button class="primary" type="submit">ログイン</button></form></main></body></html>"""

PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>リードタイム一括更新</title><style>""" + STYLE + """</style></head><body><main>
<div class="top"><div><h1>リードタイム一括更新</h1>
<p class="sub">出品者出荷の全商品のリードタイムを、同じ日数にまとめて変更します。</p></div>
<button class="link" id="logout">ログアウト</button></div>
<div class="note" id="demo" hidden>デモモードで動作中です。Amazonには接続していません。</div>

<form class="card" id="form">
 <div class="field"><label for="days">リードタイム</label>
  <div class="days"><input id="days" type="number" min="1" max="30" value="3" required inputmode="numeric"><span>日</span></div>
  <p class="hint">1〜30日。在庫数や価格は変更しません。</p></div>
 <div class="field"><label for="sku">1商品だけで試す(任意)</label>
  <input id="sku" type="text" placeholder="SKUを入力" autocomplete="off">
  <p class="hint">入力すると、そのSKUだけを更新します。空欄なら全商品が対象です。</p></div>
 <label class="check"><input id="dry" type="checkbox"><span>確認のみ行う(現在の設定を読み取るだけで、変更しません)</span></label>
 <div class="row"><button class="primary" id="go" type="submit">対象の商品を確認する</button></div>
 <p class="err" id="formerr" role="alert"></p>
</form>

<div class="card" id="panel" hidden aria-live="polite">
 <p class="big" id="title"></p><p id="msg" class="nums"></p>
 <div class="bar" id="bar" hidden><div id="fill"></div></div>
 <p class="nums" id="nums"></p>
 <div class="row">
  <button class="primary" id="confirm" hidden></button>
  <button class="ghost" id="cancel" hidden>中止する</button>
  <a id="csv" href="/api/result.csv" hidden>結果をCSVでダウンロード</a>
 </div>
 <div id="samples" class="nums" hidden style="margin-top:12px;word-break:break-all"></div>
 <div id="fails" hidden><table><thead><tr><th>SKU</th><th>結果</th><th>内容</th></tr></thead><tbody id="rows"></tbody></table></div>
</div>
</main><script>
const $=id=>document.getElementById(id);
const api=(path,body)=>fetch(path,{method:body===undefined?'GET':'POST',
  headers:{'Content-Type':'application/json','X-Requested-With':'leadtime'},
  body:body===undefined?undefined:JSON.stringify(body)}).then(async r=>{
    if(r.status===401){location.reload();throw new Error('login')}
    const j=await r.json();if(!r.ok)throw new Error(j.error||'エラーが発生しました');return j});
let timer=null;
const active=p=>['report','confirm','updating'].includes(p);
function render(s){
  $('demo').hidden=!s.demo;
  const p=s.phase, busy=active(p);
  $('go').disabled=busy; $('days').disabled=busy; $('sku').disabled=busy; $('dry').disabled=busy;
  $('panel').hidden=p==='idle';
  if(p==='idle')return;
  const what=s.dry_run?'確認':'更新';
  const t={report:'対象の商品を調べています…',
    confirm:`出品者出荷 ${s.total} 件を ${s.days} 日に${what}します`,
    updating:`${what}しています…`,
    done:`${what}が完了しました`,error:'エラーで停止しました',cancelled:'中止しました'}[p];
  $('title').textContent=t; $('title').className='big'+(p==='error'?' err':'');
  let m=s.message||'';
  if(p==='report')m='Amazonから出品レポートを取得しています。数分かかることがあります。';
  if(p==='confirm')m=s.dry_run?'各商品の現在の設定を読み取るだけで、Amazonの設定は変わりません。':'実行するとAmazonの設定が変わります。';
  if(p==='done')m=s.dry_run?'確認のみのため、Amazonの設定は変わっていません。':'Amazon側への反映には数分〜15分ほどかかります。';
  $('msg').textContent=m;
  const prog=['updating','done','cancelled','error'].includes(p)&&s.total>0&&s.done>0||p==='updating';
  $('bar').hidden=!prog;
  $('fill').style.width=(s.total?Math.round(s.done/s.total*100):0)+'%';
  $('nums').textContent=prog?`${s.done} / ${s.total} 件  ・  成功 ${s.ok}  ・  失敗 ${s.ng}`:'';
  $('confirm').hidden=p!=='confirm';
  $('confirm').textContent=s.dry_run?`${s.total} 件を確認する`:`${s.total} 件を ${s.days} 日に更新する`;
  $('cancel').hidden=!busy;
  $('csv').hidden=!(s.done>0&&!busy);
  const sm=s.samples||[]; $('samples').hidden=!sm.length;
  const sl=$('samples'); sl.textContent='';
  for(const x of sm){const d=document.createElement('div');d.textContent=x.sku+'  —  '+x.info;sl.appendChild(d)}
  const f=s.failures||[]; $('fails').hidden=!f.length;
  const rows=$('rows'); rows.textContent='';
  for(const x of f){const tr=document.createElement('tr');
    for(const v of [x.sku,x.status,x.issues]){const td=document.createElement('td');td.textContent=v;tr.appendChild(td)}
    rows.appendChild(tr)}
  if(busy&&!timer)timer=setInterval(poll,1500);
  if(!busy&&timer){clearInterval(timer);timer=null}
}
const poll=()=>api('/api/status').then(render).catch(()=>{});
$('form').addEventListener('submit',e=>{e.preventDefault();$('formerr').textContent='';
  api('/api/start',{days:$('days').value,test_sku:$('sku').value,dry_run:$('dry').checked})
   .then(poll).catch(err=>{$('formerr').textContent=err.message})});
$('confirm').addEventListener('click',()=>api('/api/confirm',{}).then(poll).catch(err=>{$('msg').textContent=err.message}));
$('cancel').addEventListener('click',()=>api('/api/cancel',{}).then(poll));
$('logout').addEventListener('click',()=>api('/logout',{}).finally(()=>location.reload()));
poll();
</script></body></html>"""


def main():
    global API
    if len(PASSWORD) < 8:
        sys.exit("環境変数 APP_PASSWORD に8文字以上のパスワードを設定してください。")
    if DEMO:
        API = DemoApi()
    else:
        missing = [k for k in ("SP_CLIENT_ID", "SP_CLIENT_SECRET", "SP_REFRESH_TOKEN", "SP_SELLER_ID")
                   if not os.environ.get(k)]
        if missing:
            sys.exit("次の環境変数が未設定です: " + ", ".join(missing))
        API = SpApi()
    port = int(os.environ.get("PORT", "8080"))
    print(f"起動しました: ポート {port}" + (" (デモモード)" if DEMO else ""), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
