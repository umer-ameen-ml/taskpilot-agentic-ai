"""
Offline test: poora notebook bina internet / bina API keys ke chalata hai.
LLM ko scripted FakeLLM se, aur HTTP/SMTP ko mocks se replace kiya jata hai.
Is se graph ki wiring, routing, retry/replan, HITL (approve/revise/reject) check hoti hai.
Usage:  python tests/offline_test.py   (from the repo root)
"""
import sys, os, json, re, uuid, builtins, io, contextlib
from datetime import date, timedelta, datetime, timezone
from unittest import mock
import nbformat, requests, smtplib
from langchain_core.messages import AIMessage

NB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "taskpilot.ipynb")

for k in ["DUFFEL_ACCESS_TOKEN","TAVILY_API_KEY","OPENWEATHER_API_KEY","SMTP_USER","SMTP_PASSWORD","GOOGLE_API_KEY","OPENROUTER_API_KEY"]:
    os.environ[k] = "test-" + k.lower()

SC = {}          # scenario flags
SENT = []        # emails "sent"
CALLS = {"currency_v2": 0}


class R:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status
        self.text = json.dumps(data)
    def json(self): return self.data
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def fake_post(url, **kw):
    if "duffel" in url:
        dep = kw["json"]["data"]["slices"][0]["departure_date"]
        def offer(i, amt):
            return {"id": f"off_{i}", "total_amount": amt, "total_currency": "USD",
                    "slices": [{"duration": "PT3H35M", "segments": [{
                        "operating_carrier": {"name": f"Air{i}"}, "operating_carrier_flight_number": f"{i}00",
                        "origin": {"iata_code": "LHE"}, "destination": {"iata_code": "DXB"},
                        "departing_at": f"{dep}T08:00:00", "arriving_at": f"{dep}T10:35:00"}]}]}
        return R({"data": {"offers": [offer(1, "310.00"), offer(2, "199.50"), offer(3, "230.10")]}})
    if "tavily" in url:
        return R({"answer": "Visa on arrival rules vary; apply e-visa.", "results": [
            {"title": "UAE gov", "url": "https://u.ae", "content": "Visa info"}]})
    raise AssertionError(url)


def fake_get(url, **kw):
    if "openweathermap" in url:
        tz = 14400
        base = datetime.now(timezone.utc)
        tdate = date.fromisoformat(os.environ["TRAVEL_DATE_ISO"])
        offset_days = 30 if SC.get("weather_unavailable") else 0
        items = []
        for h in range(8):
            local = datetime(tdate.year, tdate.month, tdate.day, h * 3, 0, tzinfo=timezone.utc) + timedelta(days=offset_days)
            ts = int((local - timedelta(seconds=tz)).timestamp())
            items.append({"dt": ts, "main": {"temp_min": 28 + h, "temp_max": 31 + h, "humidity": 50},
                          "wind": {"speed": 3}, "weather": [{"description": "clear sky"}], "pop": 0.1})
        return R({"city": {"name": "Dubai", "timezone": tz}, "list": items})
    if "frankfurter.dev/v2" in url:
        CALLS["currency_v2"] += 1
        if SC.get("currency_flaky") and CALLS["currency_v2"] == 1:
            raise requests.Timeout("timed out")
        if SC.get("currency_flaky") or SC.get("v2_down"):
            if SC.get("v2_down"):
                raise requests.Timeout("down")
        return R({"rate": 278.55, "date": "2026-10-21"})
    if "frankfurter.dev/v1" in url:
        if SC.get("currency_flaky") and CALLS["currency_v2"] == 1:
            raise requests.Timeout("timed out")
        return R({"date": "2026-10-21", "rates": {"PKR": 278.0}})
    if "open.er-api.com" in url:
        if SC.get("currency_flaky") and CALLS["currency_v2"] == 1:
            raise requests.Timeout("timed out")
        return R({"rates": {"PKR": 277.0}, "time_last_update_utc": "x"})
    raise AssertionError(url)


class FakeSMTP:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def login(self, u, p): assert u and p
    def send_message(self, m): SENT.append((m["To"], m["Subject"], m.get_content()))


# ----------------------------- fake LLM -----------------------------
def text_of(messages):
    return "\n".join(str(m.content) for m in messages)


from collections import Counter
COUNT = Counter()

class FakeStructured:
    def __init__(self, schema, ns): self.schema, self.ns = schema, ns
    def invoke(self, messages):
        n, name, txt = self.ns, self.schema.__name__, text_of(messages)
        COUNT[name] += 1
        if name == "Goal":
            missing = []
            if SC.get("missing_info") and "Additional details provided" not in txt:
                missing = ["Passport country of the travellers"]
            return n["Goal"](objective="Plan Dubai trip", context=["2 adults"], requirements=["flights"],
                             constraints=[], missing_info=missing)
        if name == "Plan":
            T = n["Task"]
            tasks = [
                T(task_id="T1", description="flights", tool_name="search_duffel_flights", dependencies=[], expected_output="offers"),
                T(task_id="T2", description="weather", tool_name="get_weather_forecast", dependencies=[], expected_output="weather"),
                T(task_id="T3", description="visa", tool_name="search_visa_requirements", dependencies=[], expected_output="visa"),
                T(task_id="T4", description="convert", tool_name="convert_currency", dependencies=["T1"], expected_output="pkr"),
                T(task_id="T5", description="total", tool_name="calculate_trip_expenses", dependencies=["T4"], expected_output="total"),
                T(task_id="T6", description="email", tool_name="send_email", dependencies=["T1","T2","T3","T4","T5"], expected_output="sent"),
            ]
            if SC.get("omit_email"):
                tasks = tasks[:5]
            return n["Plan"](tasks=tasks)
        if name == "TripFacts":
            if SC.get("facts_fail"):
                raise RuntimeError("facts llm down")
            return n["TripFacts"](verdict="go_with_conditions",
                verdict_reasons=["Direct flight is cheap.", "Weather is clear."],
                best_flight_note="The cheapest direct flight is the best pick.",
                visa_requirements=["Valid passport (6 months)", "Return ticket"],
                visa_fee_and_time="AED 200 for 30 days; about 48 hours.",
                tips=["Carry sunscreen.", "Keep passport copies."])
        if name == "NextAction":
            m = re.search(r"Ready tasks:\n(\[.*\])", txt, re.S)
            ready = json.loads(m.group(1))
            return n["NextAction"](task_id=ready[-1]["task_id"], reason="fake pick")
        if name == "ReplanDecision":
            seq = SC.setdefault("replan_seq", [])
            kind = seq.pop(0) if seq else "skip"
            if kind == "replace":
                T = n["Task"]
                return n["ReplanDecision"](action="replace", reason="alt weather", replacement_tasks=[
                    T(task_id="T2b", description="weather alt", tool_name="get_weather_forecast", dependencies=[], expected_output="weather")])
            return n["ReplanDecision"](action="skip", reason="not essential")
        raise AssertionError(name)


class FakeToolModel:
    def invoke(self, messages):
        COUNT['ToolAgent'] += 1
        txt = text_of(messages)
        task = json.loads(re.search(r"CURRENT TASK:\n(\{.*?\n\})\n\nCOMPLETED", txt, re.S).group(1))
        tool, tdate = task["tool_name"], os.environ["TRAVEL_DATE_ISO"]
        if tool == "send_email" and SC.get("no_email_call"):
            return AIMessage(content="I am not able to call tools right now.")
        args = {
            "search_duffel_flights": {"origin": "LHE", "destination": "DXB", "departure_date": tdate, "adults": 2},
            "get_weather_forecast": {"city": "Dubai", "target_date": tdate},
            "search_visa_requirements": {"passport_country": "Pakistan"},
            "convert_currency": {"amount": 399.0, "from_currency": "USD", "to_currency": "PKR"},
            "calculate_trip_expenses": {"flight_cost_pkr": 111154.5},
            "send_email": {"recipient_id": "C001", "subject": "Dubai trip plan",
                           "body": ("REVISED short plan " if "HUMAN FEEDBACK" in txt else "Full plan ") + "USD/PKR"},
        }[tool]
        return AIMessage(content="", tool_calls=[{"name": tool, "args": args, "id": "call_" + uuid.uuid4().hex[:8], "type": "tool_call"}])


class FakeLLM:
    def __init__(self, ns): self.ns = ns
    def with_structured_output(self, schema, method=None): return FakeStructured(schema, self.ns)
    def bind_tools(self, tools): return FakeToolModel()
    def invoke(self, messages):
        return AIMessage(content="FINAL REPORT (fake). " + ("EMAIL SENT" if "\"action_executed\": true" in text_of(messages) else "EMAIL NOT SENT"))


# ----------------------------- runner -----------------------------
def run_notebook(inputs, scenario):
    SC.clear(); SC.update(scenario); SENT.clear(); CALLS["currency_v2"] = 0
    nb = nbformat.read(NB, 4)
    import types
    mod = types.ModuleType("__main__")      # asli __main__ jaisa: Kaggle notebook mein bhi classes yahin hoti hain
    saved_main = sys.modules.get("__main__")
    sys.modules["__main__"] = mod
    ns = mod.__dict__
    inputs = list(inputs)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        return inputs.pop(0)

    out = io.StringIO()
    with mock.patch.object(requests, "get", fake_get), mock.patch.object(requests, "post", fake_post), \
         mock.patch.object(smtplib, "SMTP_SSL", FakeSMTP), mock.patch.object(builtins, "input", fake_input), \
         contextlib.redirect_stdout(out):
        for i, c in enumerate(nb.cells):
            if c.cell_type != "code":
                continue
            src = c.source
            if "%pip install" in src or "!pip install" in src or "import kagglehub" in src:
                continue
            exec(compile(src, f"<cell {i}>", "exec"), ns)
            if "ChatGoogleGenerativeAI(" in src or "ChatOpenAI(" in src:
                ns["model"] = FakeLLM(ns)
            if "USER_QUERY = f" in src:
                os.environ["TRAVEL_DATE_ISO"] = ns["TRAVEL_DATE_ISO"]
    ns["_mod"] = mod
    assert not inputs, f"unused inputs: {inputs}"
    return ns, out.getvalue(), prompts


def final_values(ns):
    sys.modules["__main__"] = ns["_mod"]
    return ns["graph"].get_state(ns["config"]).values


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        check.failed += 1
check.failed = 0


def calls_total(): return sum(COUNT.values())

print("== S1: happy path, approve ==")
COUNT.clear()
ns, out, _ = run_notebook(["yes"], {})
v = final_values(ns); rep = v["final_answer"]
print("  LLM CALLS:", dict(COUNT), "TOTAL =", calls_total())
check(len(SENT) == 1 and SENT[0][0] == "manager@example.com", "email sent once to manager")
check(set(v["completed_tasks"]) == {"T1","T2","T3","T4","T5","T6"}, "all 6 tasks completed")
check(v["current_state"].get("approval_status") == "approved", "approval status recorded as approved")
for marker in ["# ✈️ Dubai Trip Plan", "## ⚠️ Recommendation: Go, with conditions", "## 🛫 Flights (cheapest first)", "🏆",
               "| Metric | Value |", "🌡️ Temperature", "## 🛂 Visa", "## 💱 Currency & Budget", "🧮 Estimated total",
               "## 📧 Email to Manager", "✅ **Sent to Umer (Manager)**", "## 🧭 Tips", "## 📚 Sources", "[UAE gov](https://u.ae)"]:
    check(marker in rep, f"report has: {marker}")
check("AED" not in rep.replace("AED 200", ""), "report shows no AED amounts of its own")
check("199.50" in rep and "PKR" in rep, "cheapest flight + PKR shown")
check("does not match any flight" in rep, "budget/flight mismatch is flagged (fake LLM passed an arbitrary amount)")
print("----- REPORT PREVIEW -----"); print(rep); print("--------------------------")

print("== S2: planner OMITS the email task (the reported bug) ==")
ns, out, prompts = run_notebook(["yes"], {"omit_email": True})
v = final_values(ns)
ids = [t.task_id for t in v["plan"].tasks]
check(any(t.tool_name == "send_email" for t in v["plan"].tasks), f"email task auto-added to plan: {ids}")
check(sum("Approve?" in p for p in prompts) == 1, "human approval was asked")
check(len(SENT) == 1, "email sent after approval")

print("== S3: LLM gives NO tool call for send_email ==")
ns, out, prompts = run_notebook(["yes"], {"no_email_call": True})
v = final_values(ns)
check(sum("Approve?" in p for p in prompts) == 1 and len(SENT) == 1, "fallback draft was shown, approved and sent")
check("FLIGHTS (cheapest first)" in SENT[0][2] and "ESTIMATED BUDGET" in SENT[0][2], "fallback email has real trip details")

print("== S4: facts-LLM fails -> report still built ==")
ns, out, _ = run_notebook(["yes"], {"facts_fail": True})
rep = final_values(ns)["final_answer"]
check("## 🛫 Flights" in rep and "## 📧 Email to Manager" in rep and "Sent to Umer" in rep, "default report built without LLM facts")

print("== S5: reject without feedback ==")
ns, out, _ = run_notebook(["no", ""], {})
v = final_values(ns); rep = v["final_answer"]
check(len(SENT) == 0 and "🚫 **Not sent.**" in rep, "nothing sent, report says not sent")
check(v["current_state"]["approval_status"] == "rejected", "approval status = rejected")

print("== S6: reject with feedback -> revise -> approve ==")
ns, out, prompts = run_notebook(["no", "make it shorter", "yes"], {})
check(len(SENT) == 1 and "REVISED" in SENT[0][2] and sum("Approve?" in p for p in prompts) == 2, "revised draft approved and sent")

print("== S7: weather unavailable + no LLM for replan (skip) ==")
ns, out, _ = run_notebook(["yes"], {"weather_unavailable": True, "replan_seq": ["skip"]})
v = final_values(ns); rep = v["final_answer"]
check("❌ Weather forecast is not available" in rep and "## ⚠️ Notes" in rep, "report explains missing weather in Notes")
check(len(SENT) == 1, "email still sent with partial data")

print("== S8: missing info -> user answers -> continues ==")
ns, out, _ = run_notebook(["Pakistani passports", "yes"], {"missing_info": True})
v = final_values(ns)
check(v["clarification_rounds"] == 1 and len(SENT) == 1, "clarification asked once, workflow continued")

print("== S9: graph verify cell + imports order ==")
cells_src = [c.source for c in nbformat.read(NB, 4).cells]
iv = next(i for i, c in enumerate(cells_src) if "draw_mermaid" in c)
ic = next(i for i, c in enumerate(cells_src) if "builder.compile" in c)
check(iv > ic, "Verify-graph cell now runs AFTER the graph is compiled")

print("\nRESULT:", "ALL CHECKS PASSED" if check.failed == 0 else f"{check.failed} CHECK(S) FAILED")
sys.exit(1 if check.failed else 0)
