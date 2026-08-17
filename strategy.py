#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
وكيل استراتيجية الاستثمار — Investment Strategy Agent
وكيل متخصص في وضع استراتيجية استثمار استرشادية بناءً على مخرجات
وكيل محلل السوق الذكي (يعمل على المنفذ 8084 افتراضيًا).

- يجلب تحليل السوق من وكيل المحلل (توقعات الشركات + المعنويات).
- يقيّم الحالة العامة للسوق ومستوى المخاطرة.
- يقترح توزيعًا استثماريًا: نسبة استثمار/سيولة، وأوزان مقترحة لكل
  شركة بناءً على قوة الإشارة والثقة.
- يصنّف كل شركة: شراء / تراكم / احتفاظ / مراقبة / تقليل / تجنب،
  مع تبرير مأخوذ من الأخبار.
- يولّد ملخص استراتيجية نصيًا بالعربية + قائمة مراقبة.

تنبيه: كل المخرجات استرشادية ولا تعتبر توصية استثمارية.
مبني بالكامل بدون مكتبات خارجية (Python فقط).
"""
import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
PUBLIC_DIR = BASE / "public"
DATA_DIR = BASE / "data"
STRATEGY_FILE = DATA_DIR / "strategy.json"
SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_PORT = 8085
DEFAULT_ANALYST_URL = os.environ.get("ANALYST_URL", "http://127.0.0.1:8084")
STRATEGY_INTERVAL = int(os.environ.get("STRATEGY_INTERVAL", "10"))  # دقائق
DEFAULT_CAPITAL = 100000.0  # رأس المال الافتراضي (ر.س) — يُعدَّل من الواجهة

_lock = threading.Lock()
_strategy_lock = threading.Lock()
_analyst_status = {"ok": None, "last": None, "error": None}
_last_strategy = None
_strategy_running = False
_capital = DEFAULT_CAPITAL


# ------------------------- وقت -------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------- تخزين -------------------------
def load_cache() -> dict:
    if STRATEGY_FILE.exists():
        try:
            return json.loads(STRATEGY_FILE.read_text("utf-8"))
        except Exception:
            pass
    return None


def save_cache(strategy: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    tmp = STRATEGY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(strategy, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STRATEGY_FILE)


# ------------------------- رأس المال -------------------------
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def get_capital() -> float:
    global _capital
    return _capital


def set_capital(value) -> dict:
    """يضبط رأس المال ويحفظه. يعيد القيمة بعد التحقق."""
    global _capital
    try:
        val = float(value)
    except (TypeError, ValueError):
        return {"ok": False, "error": "رأس المال يجب أن يكون رقمًا"}
    if val <= 0:
        return {"ok": False, "error": "رأس المال يجب أن يكون أكبر من صفر"}
    _capital = val
    DATA_DIR.mkdir(exist_ok=True)
    settings = load_settings()
    settings["capital"] = val
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(SETTINGS_FILE)
    return {"ok": True, "capital": val}


# ------------------------- الاتصال بمحلل السوق -------------------------
def _get(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "InvestmentStrategy/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_analysis() -> dict:
    return _get(DEFAULT_ANALYST_URL + "/api/analysis")


def analyst_status() -> dict:
    try:
        st = _get(DEFAULT_ANALYST_URL + "/api/status", timeout=8.0)
        return {"ok": True, "name": st.get("name"), "port": st.get("port"), "error": None}
    except Exception as ex:
        return {"ok": False, "name": None, "port": None, "error": str(ex)}


# ------------------------- محرك الاستراتيجية -------------------------
def _action_for(comp: dict) -> tuple:
    """يعيد (إجراء، فئة لون) لكل شركة بناءً على الإشارة والثقة."""
    signal = comp.get("signal")
    conf = comp.get("confidence", 0)
    if signal == "up":
        return ("شراء", "buy") if conf >= 60 else ("تراكم", "accumulate")
    if signal == "down":
        return ("تجنب", "avoid") if conf >= 60 else ("تقليل", "reduce")
    return ("احتفاظ", "hold")


def _position_class(conf: int) -> str:
    if conf >= 75:
        return "كبيرة"
    if conf >= 50:
        return "متوسطة"
    return "صغيرة"


def money(value: float) -> float:
    return round(max(0.0, value), 2)


def build_strategy(a: dict, capital: float = None) -> dict:
    """يبني الاستراتيجية من تحليل محلل السوق.

    capital: رأس المال المُدخل (ر.س) — يُحسب على أساسه التوزيع بالمبالغ.
    """
    if capital is None:
        capital = get_capital()
    companies = a.get("companies") or []
    sectors = a.get("sectors") or []
    sentiment = int(a.get("market_sentiment") or 0)
    up = int(a.get("up") or 0)
    down = int(a.get("down") or 0)
    flat = int(a.get("flat") or 0)
    total = len(companies)
    up_ratio = (up / total) if total else 0.0

    # ---------- تقييم السوق ----------
    if up_ratio >= 0.6 and sentiment >= 10:
        regime = "صاعد"
    elif up_ratio <= 0.4 and sentiment <= -10:
        regime = "هابط"
    elif abs(sentiment) < 15:
        regime = "محايد"
    else:
        regime = "متقلب"

    strong_total = sum(c.get("strong", 0) for c in companies)
    risk = abs(sentiment) + strong_total * 4 + (max(0, total - 5)) * 3
    risk_level = "منخفض" if risk < 30 else "متوسط" if risk < 60 else "مرتفع"

    # ---------- التوزيع: استثمار / سيولة ----------
    invested = int(max(20, min(90, round(40 + up_ratio * 30 + sentiment / 100.0 * 20))))
    cash = 100 - invested

    # ---------- الفرص ----------
    opportunities = []
    for c in companies:
        if c.get("signal") != "up":
            continue
        weight = max(0.0, float(c.get("avg_score", 0)) / 100.0) * float(c.get("confidence", 0))
        if weight > 0:
            opportunities.append({
                "company": c["company"],
                "name": c["name"],
                "sector": c["sector"],
                "score": c.get("avg_score", 0),
                "confidence": c.get("confidence", 0),
                "weight": weight,
            })
    total_w = sum(o["weight"] for o in opportunities) or 1.0

    allocation = []
    for o in opportunities:
        pct = round(o["weight"] / total_w * invested, 1)
        allocation.append({
            "name": o["name"],
            "company": o["company"],
            "sector": o["sector"],
            "weight_pct": pct,
            "amount": money(pct / 100.0 * capital),
            "confidence": o["confidence"],
            "position": _position_class(o["confidence"]),
        })
    allocation.sort(key=lambda x: -x["weight_pct"])

    # ---------- توصيات الشركات ----------
    recommendations = []
    for c in companies:
        action, action_class = _action_for(c)
        alloc = next((x for x in allocation if x["company"] == c["company"]), None)
        top_reason = (c.get("reasons") or [{}])[0]
        recommendations.append({
            "company": c["company"],
            "name": c["name"],
            "sector": c["sector"],
            "direction": c.get("direction"),
            "signal": c.get("signal"),
            "score": c.get("avg_score"),
            "confidence": c.get("confidence"),
            "action": action,
            "action_class": action_class,
            "weight_pct": alloc["weight_pct"] if alloc else 0.0,
            "amount": money(alloc["amount"]) if alloc else 0.0,
            "position": alloc["position"] if alloc else "-",
            "news_count": c.get("news_count"),
            "pos": c.get("pos"),
            "neg": c.get("neg"),
            "reason_title": top_reason.get("title"),
            "reason_source": top_reason.get("source"),
            "reason_score": top_reason.get("score"),
        })

    order = {"buy": 0, "accumulate": 1, "hold": 2, "reduce": 3, "avoid": 4}
    recommendations.sort(key=lambda r: (order[r["action_class"]], -r["confidence"]))

    # ---------- توزيع القطاعات ----------
    sector_exposure = {}
    for x in allocation:
        s = sector_exposure.setdefault(x["sector"], {"weight_pct": 0.0, "amount": 0.0})
        s["weight_pct"] = round(s["weight_pct"] + x["weight_pct"], 1)
        s["amount"] = money(s["amount"] + x["amount"])
    sector_exposure = [{"sector": k, "weight_pct": v["weight_pct"], "amount": v["amount"]}
                       for k, v in sorted(sector_exposure.items(), key=lambda kv: -kv[1]["weight_pct"])]

    # ---------- قائمة المراقبة ----------
    watchlist = [r for r in recommendations if r["action_class"] in ("reduce", "avoid")]
    watchlist.sort(key=lambda r: r["confidence"], reverse=True)

    # ---------- أفضل الاختيارات ----------
    top_picks = [r for r in recommendations if r["action_class"] in ("buy", "accumulate")][:3]
    top_avoid = watchlist[:3]

    invested_amount = money(invested / 100.0 * capital)
    cash_amount = money(cash / 100.0 * capital)

    # ---------- الملخص النصي ----------
    summary = _summarize(regime, risk_level, sentiment, up, down, flat, total,
                         invested, cash, capital, invested_amount, cash_amount,
                         top_picks, top_avoid)

    return {
        "generated_at": now_iso(),
        "analyst_generated_at": a.get("generated_at"),
        "analyst_news_count": a.get("news_count"),
        "capital": capital,
        "market": {
            "regime": regime,
            "risk_level": risk_level,
            "sentiment": sentiment,
            "up": up,
            "down": down,
            "flat": flat,
            "companies_count": total,
        },
        "allocation": {
            "invested_pct": invested,
            "cash_pct": cash,
            "invested_amount": invested_amount,
            "cash_amount": cash_amount,
        },
        "companies": recommendations,
        "portfolio": allocation,
        "sector_exposure": sector_exposure,
        "watchlist": watchlist,
        "summary": summary,
    }


def _summarize(regime, risk_level, sentiment, up, down, flat, total,
               invested, cash, capital, invested_amount, cash_amount,
               top_picks, top_avoid) -> list:
    lines = []
    fmt = lambda v: f"{v:,.0f}"
    # نظرة السوق
    dist = f"من أصل {total} شركة محللة: {up} متوقع لها ارتفاع، {down} هبوط، {flat} استقرار."
    lines.append(
        f"يُظهر تحليل الأخبار الحالي حالة سوق {regime} "
        f"مع معنويات {'إيجابية' if sentiment > 10 else 'سلبية' if sentiment < -10 else 'محايدة'} "
        f"بنسبة {sentiment:+d}%. {dist}"
    )
    # التوزيع
    lines.append(
        f"بناءً على رأس المال المُدخل {fmt(capital)} ر.س، يُقترح توزيع استراتيجي: "
        f"استثمار {invested}% ({fmt(invested_amount)} ر.س) "
        f"وإبقاء {cash}% ({fmt(cash_amount)} ر.س) سيولة للمرونة والفرص، "
        f"مع مستوى مخاطرة {risk_level}."
    )
    # الاختيارات
    if top_picks:
        names = "، ".join(f"{p['name']} ({p['weight_pct']}% — {fmt(p['amount'])} ر.س)" for p in top_picks)
        lines.append(
            f"أفضل الفرص المُرتقبة: {names}. تتميز هذه الشركات بإشارات صعودية "
            f"مدعومة بأخبار إيجابية بأوزان ثقة مرتفعة."
        )
    else:
        lines.append("لا توجد حالياً فرص شراء بإشارة واضحة — يُفضَّل التركيز على المراقبة والسيولة.")
    if top_avoid:
        names = "، ".join(f"{p['name']}" for p in top_avoid)
        lines.append(
            f"شركات يُنصح بتجنبها أو تقليل التعرض لها حالياً: {names}، "
            f"لأن أخبارها تحمل إشارات سلبية."
        )
    lines.append(
        "تذكير: هذا التحليل استرشادي مبني على الأخبار فقط ولا يشمل البيانات المالية "
        "الأساسية، ويجب مراجعته دوريًا وتوزيع المخاطر عبر قطاعات متعددة."
    )
    return lines


# ------------------------- تشغيل الاستراتيجية -------------------------
def run_strategy(capital: float = None) -> dict:
    global _last_strategy, _analyst_status, _capital
    if not _strategy_lock.acquire(blocking=False):
        return {"ok": False, "message": "عملية بناء استراتيجية جارية بالفعل"}
    try:
        if capital is not None:
            _capital = capital
        st = analyst_status()
        _analyst_status["ok"] = st["ok"]
        _analyst_status["last"] = now_iso()
        _analyst_status["error"] = st["error"]
        if not st["ok"]:
            return {"ok": False, "error": "وكيل محلل السوق غير متصل", "status": _analyst_status}
        analysis = fetch_analysis()
        strategy = build_strategy(analysis, capital=_capital)
        strategy["analyst"] = st
        _last_strategy = strategy
        save_cache(strategy)
        return {"ok": True, "strategy": strategy}
    except Exception as ex:
        _analyst_status["error"] = str(ex)
        return {"ok": False, "error": str(ex), "status": _analyst_status}
    finally:
        _strategy_lock.release()


def strategy_loop() -> None:
    while True:
        try:
            run_strategy()
        except Exception:
            pass
        time.sleep(max(1, STRATEGY_INTERVAL) * 60)


# ------------------------- HTTP -------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "InvestmentStrategy/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _path(self) -> list:
        return urlparse(self.path).path.strip("/").split("/")

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def do_GET(self):
        parts = self._path()
        if not parts or parts == [""]:
            self._send_file("index.html")
            return
        if parts[0] == "api":
            self._api("GET", parts)
            return
        name = os.path.basename(parts[-1])
        if len(parts) == 1 and "/" not in name:
            self._send_file(name)
        else:
            self._send(404, {"error": "غير موجود"})

    def do_POST(self):
        self._body = self._read_body()
        parts = self._path()
        if parts and parts[0] == "api":
            self._api("POST", parts)
        else:
            self._send(404, {"error": "غير موجود"})

    def _send_file(self, name):
        path = (PUBLIC_DIR / name).resolve()
        if not path.is_relative_to(PUBLIC_DIR.resolve()) or not path.is_file():
            self._send(404, {"error": "غير موجود"})
            return
        ctype = ("text/html; charset=utf-8" if path.suffix == ".html" else
                 "text/css; charset=utf-8")
        self._send(200, path.read_bytes(), ctype)

    def _api(self, method, parts):
        try:
            self._route(method, parts)
        except Exception as e:
            self._send(500, {"error": f"خطأ داخلي: {e}"})

    def _route(self, method, parts):
        # GET /api/status
        if method == "GET" and parts == ["api", "status"]:
            self._send(200, {
                "ok": True, "name": "وكيل استراتيجية الاستثمار",
                "port": _port, "version": "1.1",
                "analyst_url": DEFAULT_ANALYST_URL,
                "analyst_connected": _analyst_status.get("ok"),
                "capital": get_capital(),
                "last_strategy": (_last_strategy or {}).get("generated_at"),
                "interval_minutes": STRATEGY_INTERVAL,
            })
            return

        # POST /api/strategy — إعادة بناء استراتيجية فورية
        # يدعم جسم اختياري: {"capital": 100000} لضبط رأس المال ثم التوليد
        if method == "POST" and parts == ["api", "strategy"]:
            capital = self._body.get("capital")
            if capital is not None:
                cap_res = set_capital(capital)
                if not cap_res.get("ok"):
                    self._send(400, cap_res)
                    return
                capital = cap_res["capital"]
            result = run_strategy(capital=capital)
            if result.get("ok"):
                self._send(200, result)
            else:
                self._send(503, result)
            return

        # POST /api/capital — ضبط رأس المال وإعادة بناء الاستراتيجية فورًا
        if method == "POST" and parts == ["api", "capital"]:
            capital = self._body.get("capital")
            if capital is None:
                self._send(400, {"ok": False, "error": "أرسل capital في الجسم"})
                return
            cap_res = set_capital(capital)
            if not cap_res.get("ok"):
                self._send(400, cap_res)
                return
            result = run_strategy(capital=cap_res["capital"])
            if result.get("ok"):
                self._send(200, result)
            else:
                self._send(503, result)
            return

        # GET /api/strategy — آخر استراتيجية
        if method == "GET" and parts == ["api", "strategy"]:
            data = _last_strategy or load_cache()
            if data is None:
                data = run_strategy().get("strategy")
            if data is None:
                self._send(503, {"ok": False, "error": "لا توجد استراتيجية بعد — اضغط زر التوليد"})
                return
            data = dict(data)
            data["analyst_connected"] = _analyst_status.get("ok")
            self._send(200, data)
            return

        self._send(404, {"error": "المسار غير موجود"})


def main():
    global _port, _capital
    _port = int(os.environ.get("PORT", DEFAULT_PORT))
    settings = load_settings()
    _capital = float(settings.get("capital") or DEFAULT_CAPITAL)
    threading.Thread(target=strategy_loop, daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", _port), Handler)
    print("=" * 60)
    print("  وكيل استراتيجية الاستثمار — Investment Strategy")
    print(f"  يستمع على http://0.0.0.0:{_port}")
    print(f"  مصدر التحليل: {DEFAULT_ANALYST_URL}")
    print(f"  استراتيجية تلقائية كل {STRATEGY_INTERVAL} دقيقة")
    print("=" * 60)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
