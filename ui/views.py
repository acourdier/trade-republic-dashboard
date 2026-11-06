import csv
import re
import shutil
import time
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_POST

from broker import jobs, tr
from broker.models import Setting, SyncRun
from portfolio import analytics
from portfolio.models import (
    CASH_KINDS,
    BenchmarkPrice,
    Document,
    Instrument,
    Kind,
    PortfolioSnapshot,
    Transaction,
)
from ui import middleware as gate

ASSET_ORDER = ["Stocks", "ETFs", "Crypto", "Bonds", "Private markets"]
COUNTRIES = {
    "US": "United States", "IE": "Ireland", "LU": "Luxembourg", "DE": "Germany",
    "FR": "France", "NL": "Netherlands", "GB": "United Kingdom", "CH": "Switzerland",
    "JP": "Japan", "CA": "Canada", "DK": "Denmark", "SE": "Sweden", "ES": "Spain",
    "IT": "Italy", "FI": "Finland", "NO": "Norway", "BE": "Belgium", "AT": "Austria",
    "AU": "Australia", "CN": "China", "KY": "Cayman Islands", "XS": "International",
    "XF": "Crypto", "JE": "Jersey", "BM": "Bermuda", "TW": "Taiwan", "KR": "South Korea",
    "PT": "Portugal", "IL": "Israel", "HK": "Hong Kong", "IN": "India", "BR": "Brazil",
}
ACTIVITY_GROUPS = {
    "trades": [Kind.BUY, Kind.SELL],
    "income": [Kind.DIVIDEND, Kind.INTEREST, Kind.BOND_REDEMPTION],
    "transfers": [Kind.DEPOSIT, Kind.WITHDRAWAL, Kind.TAX_REFUND],
    "card": [Kind.CARD],
    "other": [Kind.CANCELLED, Kind.EXPIRED, Kind.REJECTED, Kind.PENDING, Kind.UNKNOWN],
}


def _pts(series):
    return [[d.isoformat(), round(v, 2)] for d, v in series]


def _with_today(series, value, on):
    """Append today's value so the 'value' line reaches the right edge."""
    pts = [(d, v) for d, v in series if d != on]
    return [*pts, (on, value)] if value else pts


# ---------------------------------------------------------------- pages
def overview(request):
    o = analytics.overview()
    on = o["today"]
    chart = {
        "ranges": "#nw-ranges",
        "series": [
            {"label": "Invested capital", "color": "--s1", "fill": True, "stepped": True,
             "points": _pts(o["cost_basis"])},
            {"label": "Account value", "color": "--ink",
             "points": _pts(_with_today(o["net_worth"], o["total"], on))},
        ],
    }
    recent = (Transaction.objects.filter(kind__in=CASH_KINDS).exclude(amount=None)
              .select_related("instrument")[:8])
    cf = o["cash_flow"]
    return render(request, "ui/overview.html", {
        "o": o, "cf": cf, "chart": chart, "recent": recent,
        "has_data": Transaction.objects.exists() or o["snapshot"] is not None,
    })


def positions(request):
    o = analytics.overview()
    sort = request.GET.get("sort", "value")
    keys = {"value": lambda p: -p["value"], "pl": lambda p: -p["pl"],
            "pl_pct": lambda p: -(p["pl_pct"] or -1e9), "name": lambda p: p["name"].lower()}
    rows = sorted(o["positions"], key=keys.get(sort, keys["value"]))
    groups = defaultdict(list)
    for p in rows:
        groups[p["asset_class"]].append(p)
    ordered = []
    for cls in ASSET_ORDER + sorted(set(groups) - set(ASSET_ORDER)):
        if cls in groups:
            g = groups[cls]
            value, cost = sum(p["value"] for p in g), sum(p["cost"] for p in g)
            ordered.append({"label": cls, "rows": g, "value": value, "pl": value - cost,
                            "pl_pct": (value - cost) / cost * 100 if cost else None})
    return render(request, "ui/positions.html", {"o": o, "groups": ordered, "sort": sort})


def position(request, isin):
    inst = get_object_or_404(Instrument, isin=isin)
    d = analytics.position_detail(isin)
    # Net money put into this instrument over time (buys − sells), plus value snapshots.
    running, invested = 0.0, {}
    for t in sorted(d["transactions"], key=lambda t: t.timestamp):
        if t.amount is None or t.kind not in (Kind.BUY, Kind.SELL):
            continue
        running += abs(float(t.amount)) * (1 if t.kind == Kind.BUY else -1)
        invested[timezone.localdate(t.timestamp)] = running
    on = analytics.today()
    series = [{"label": "Net invested", "color": "--s1", "fill": True, "stepped": True,
               "points": _pts(sorted(invested.items()) + ([(on, running)] if invested else []))}]
    values = [(day, v[0]) for day, v in d["history"]]
    if d["current"]:
        values = _with_today(values, d["current"]["value"], on)
    if values:
        series.append({"label": "Value", "color": "--ink", "points": _pts(values)})
    docs = Document.objects.filter(isin=isin)[:50]
    return render(request, "ui/position.html", {
        "inst": inst, "d": d, "chart": {"ranges": "#pos-ranges", "series": series}, "docs": docs,
    })


def activity(request):
    qs = Transaction.objects.select_related("instrument")
    group = request.GET.get("group", "")
    if group in ACTIVITY_GROUPS:
        qs = qs.filter(kind__in=ACTIVITY_GROUPS[group])
    elif not request.GET.get("all"):
        qs = qs.exclude(kind__in=ACTIVITY_GROUPS["other"])
    month = request.GET.get("month", "")
    if re.fullmatch(r"\d{4}-\d{2}", month):
        y, m = map(int, month.split("-"))
        qs = qs.filter(timestamp__year=y, timestamp__month=m)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(subtitle__icontains=q)
                       | Q(instrument__isin__iexact=q) | Q(instrument__name__icontains=q))
    if request.GET.get("format") == "csv":
        return _csv(qs)
    totals = defaultdict(float)
    for kind, amount in qs.exclude(amount=None).values_list("kind", "amount"):
        totals[kind] += float(amount)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    months = sorted({d.strftime("%Y-%m") for d in
                     Transaction.objects.dates("timestamp", "month")}, reverse=True)
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "ui/activity.html", {
        "page": page, "group": group, "month": month, "q": q, "months": months,
        "groups": list(ACTIVITY_GROUPS), "qs_params": params.urlencode(),
        "count": page.paginator.count,
        "totals": sorted(((Kind(k).label, v) for k, v in totals.items()), key=lambda x: -abs(x[1])),
    })


def _csv(qs):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="activity-{timezone.localdate()}.csv"'
    w = csv.writer(resp)
    w.writerow(["date", "kind", "amount_eur", "description", "isin", "instrument", "event_type",
                "status"])
    for t in qs.iterator():
        w.writerow([timezone.localtime(t.timestamp).isoformat(timespec="seconds"),
                    t.get_kind_display(), t.amount if t.amount is not None else "",
                    t.label, t.instrument_id or "",
                    t.instrument.name if t.instrument else "", t.event_type, t.status])
    return resp


def income(request):
    o = analytics.overview()
    inc = o["income"]
    chart = {
        "stacked": True,
        "labels": [m["month"] for m in inc["monthly"]],
        "series": [
            {"label": "Dividends", "color": "--s1", "data": [round(m["dividend"], 2)
                                                             for m in inc["monthly"]]},
            {"label": "Interest", "color": "--s2", "data": [round(m["interest"], 2)
                                                            for m in inc["monthly"]]},
        ],
    }
    by_year = defaultdict(lambda: {"dividend": 0.0, "interest": 0.0})
    for m in inc["monthly"]:
        y = by_year[m["month"][:4]]
        y["dividend"] += m["dividend"]
        y["interest"] += m["interest"]
        y["total"] = y["dividend"] + y["interest"]
    return render(request, "ui/income.html", {
        "o": o, "inc": inc, "chart": chart,
        "by_year": sorted(by_year.items(), reverse=True),
        "payments": inc["payments"][:40],
    })


def performance(request):
    o = analytics.overview()
    cf = o["cash_flow"]
    on = o["today"]
    series = [{"label": "Invested capital", "color": "--ink", "dash": True,
               "points": _pts(o["cost_basis"])}]
    colours = ["--s1", "--s2", "--s3"]
    replays = []
    for i, b in enumerate(o["benchmarks"]):
        series.append({"label": b["label"], "color": colours[i % 3], "points": _pts(b["series"])})
        final = b["series"][-1][1] if b["series"] else None
        replays.append({"label": b["label"], "symbol": b["symbol"], "colour": colours[i % 3],
                        "value": final,
                        "diff": (o["depot_value"] - final) if final is not None else None})
    bench_chart = {"ranges": "#bench-ranges", "series": series}
    monthly = cf["monthly"][-36:]
    flow_chart = {
        "labels": [m["month"] for m in monthly],
        "series": [
            {"label": "Bought", "color": "--s1", "data": [m["buys"] for m in monthly]},
            {"label": "Sold", "color": "--s2", "data": [m["sells"] for m in monthly]},
        ],
    }
    countries = [{**c, "name": COUNTRIES.get(c["label"], c["label"])}
                 for c in o["allocation"]["by_country"]]
    bench_updated = BenchmarkPrice.objects.order_by("-date").values_list("date", flat=True).first()
    return render(request, "ui/performance.html", {
        "o": o, "cf": cf, "bench_chart": bench_chart, "flow_chart": flow_chart,
        "replays": replays, "countries": countries, "bench_updated": bench_updated, "today": on,
    })


def settings_page(request):
    creds = tr.load_credentials()
    error = None
    if request.method == "POST":
        phone = request.POST.get("phone", "").strip().replace(" ", "")
        pin = request.POST.get("pin", "").strip() or (creds.pin if creds else "")
        if not re.fullmatch(r"\+\d{8,15}", phone):
            error = "Phone must be in international format, e.g. +4915112345678."
        elif not (pin.isdigit() and 4 <= len(pin) <= 6):
            error = "The PIN is 4 to 6 digits."
        else:
            if creds and creds.phone != phone:
                tr.forget_session(creds.phone)
                _wipe_account_data()
            elif creds and creds.pin != pin:
                tr.forget_session(phone)
            tr.save_credentials(tr.Credentials(phone, pin))
            return redirect("settings")
    runs = SyncRun.objects.all()[:15]
    return render(request, "ui/settings.html", {
        "creds": creds, "error": error, "runs": runs,
        "masked_phone": (creds.phone[:4] + " ••• " + creds.phone[-3:]) if creds else "",
        "docs_count": Document.objects.count(),
        "docs": Document.objects.all()[:12],
        "documents_dir": settings.DOCUMENTS_DIR,
        "tx_count": Transaction.objects.count(),
        "snap_count": PortfolioSnapshot.objects.count(),
        "db_path": settings.DATABASES["default"]["NAME"],
        "first_tx": Transaction.objects.order_by("timestamp").values_list("timestamp", flat=True)
        .first(),
    })


def _wipe_account_data():
    Transaction.objects.all().delete()
    PortfolioSnapshot.objects.all().delete()
    Instrument.objects.all().delete()
    Document.objects.all().delete()
    SyncRun.objects.all().delete()


@require_POST
def reset(request):
    if request.POST.get("confirm") != "delete":
        return redirect("settings")
    creds = tr.load_credentials()
    if creds:
        tr.forget_session(creds.phone)
    tr.delete_credentials()
    _wipe_account_data()
    BenchmarkPrice.objects.all().delete()
    Setting.objects.all().delete()
    shutil.rmtree(settings.DOCUMENTS_DIR, ignore_errors=True)
    settings.DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
    return redirect("settings")


@require_GET
def document(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    root = settings.DOCUMENTS_DIR.resolve()
    path = (root / doc.path).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise Http404
    return FileResponse(path.open("rb"), content_type="application/pdf",
                        filename=path.name, as_attachment=False)


# ---------------------------------------------------------------- sync API
def _run_json(run: SyncRun) -> dict:
    return {"id": run.pk, "mode": run.mode, "status": run.status, "stage": run.stage,
            "message": run.message, "error_code": run.error_code, "counts": run.counts}


@require_POST
def sync_start(request):
    if tr.load_credentials() is None:
        return JsonResponse({"message": "Add your Trade Republic phone and PIN in Settings first."},
                            status=400)
    mode = request.POST.get("mode", SyncRun.Mode.INCREMENTAL)
    if mode not in SyncRun.Mode.values:
        mode = SyncRun.Mode.INCREMENTAL
    run = jobs.enqueue(mode)
    return JsonResponse(_run_json(run), status=202)


@require_GET
def sync_status(request, pk):
    run = get_object_or_404(SyncRun, pk=pk)
    # A queued run nobody picks up means the worker isn't running.
    if run.status == SyncRun.Status.QUEUED and timezone.now() - run.created_at > timedelta(
            seconds=20):
        run.stage = "Waiting for the worker — is it running (manage.py serve)?"
    return JsonResponse(_run_json(run))


# ---------------------------------------------------------------- password gate
def login(request):
    if not settings.TRDASH_PASSWORD or gate.is_unlocked(request):
        return redirect("overview")
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
        nxt = ""
    error = ""
    if request.method == "POST":
        if gate.check_password(request.POST.get("password", "")):
            request.session.cycle_key()
            request.session[gate.SESSION_KEY] = gate.password_fingerprint()
            return redirect(nxt or "overview")
        time.sleep(1)  # slow down guessing
        error = "Wrong password."
    return render(request, "ui/login.html", {"next": nxt, "error": error},
                  status=401 if error else 200)


@require_POST
def logout(request):
    request.session.flush()
    return redirect("login")


@require_GET
def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
