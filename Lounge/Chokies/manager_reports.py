"""Chokies POS - manager reports.

Read-only pages for managers/admins, wired in manager_urls.py:
    reports/            index (grouped list of reports + today / month figures)
    reports/<slug>/     one report, filtered by ?from=YYYY-MM-DD&to=YYYY-MM-DD, add &export=csv to download

Access is the same as the rest of the back office (manager_required: signed in, role MANAGER / ADMIN or superuser;
anyone else gets a 403). Nothing here writes to the database and nothing needs a migration.

Every report is a function  fn(start, end, GET) -> (tiles, sections, selects)  registered in REPORTS at the bottom.
One generic template (report.html) renders any of them, and the same data feeds the CSV export, so adding a report
means adding one function and one line in REPORTS.

Sections: helpers - sales - items - staff - shifts - stock - profit - purchases - exceptions - z reports - views
"""
import csv
from collections import defaultdict
from datetime import date, timedelta
from decimal import InvalidOperation

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.http import Http404, HttpResponse
from django.utils import timezone

from .manager_views import DIRECT_PREFIX, PS, _fmt, _items_with_stock, _money, _page, manager_required
from .models import (AuditLog, Bill, GoodsReceiptLine, MenuItem, MovementType, Order, OrderItem, Payment,
                     PurchaseOrder, Shift, ShiftAssignment, StockMovement, User, ZReport)
from .views import D, q

ZERO = D(0)
QTY = D("0.0001")        # quantities are shown to 4 places at most
NUMERIC = {"m", "n", "i", "p"}
ROW_CAP = 200          # detail lists show at most this many rows (the totals above them always cover everything)


# =============================================================================
# Helpers
# =============================================================================

def _d(value):
    """Decimal from anything (Z-report JSON holds amounts as strings)."""
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def _period(request):
    """(start, end) dates from ?from / ?to. Defaults to the 1st of this month .. today."""
    today = timezone.localdate()

    def parse(v, default):
        try:
            return date.fromisoformat(v)
        except (TypeError, ValueError):
            return default
    start, end = parse(request.GET.get("from"), today.replace(day=1)), parse(request.GET.get("to"), today)
    return (end, start) if end < start else (start, end)


def _presets():
    t = timezone.localdate()
    first = t.replace(day=1)
    last_end = first - timedelta(days=1)
    return [("Today", t, t), ("Yesterday", t - timedelta(days=1), t - timedelta(days=1)),
            ("Last 7 days", t - timedelta(days=6), t), ("This month", first, t),
            ("Last month", last_end.replace(day=1), last_end)]


def _show(v, kind):
    """Value -> text for the page. Kinds: t text, w wrapping text, m money, n quantity, i integer, p percent, dt, d."""
    if v is None or v == "":
        return ""
    if kind in ("d", "dt") and isinstance(v, str):          # a label such as "Total" in a footer row
        return v
    if kind == "m":
        return f"{q(v):,.2f}"
    if kind == "n":
        return _fmt(D(v).quantize(QTY))
    if kind == "i":
        return f"{int(v):,}"
    if kind == "p":
        return f"{D(v):.1f}%"
    if kind == "dt":
        return timezone.localtime(v).strftime("%d %b %Y %H:%M")
    if kind == "d":
        return v.strftime("%d %b %Y")
    return str(v)


def _raw(v, kind):
    """Value -> text for the CSV (plain numbers, ISO dates, and no spreadsheet formulas from typed text)."""
    if v is None or v == "":
        return ""
    if kind in ("d", "dt") and isinstance(v, str):
        return v
    if kind == "m":
        return f"{q(v):.2f}"
    if kind == "n":
        return _fmt(D(v).quantize(QTY))
    if kind == "p":
        return f"{D(v):.1f}"
    if kind == "dt":
        return timezone.localtime(v).strftime("%Y-%m-%d %H:%M")
    if kind == "d":
        return v.isoformat()
    s = str(v)
    return "'" + s if kind in ("t", "w") and s[:1] in "=+-@" else s


def _tile(label, value, kind="m", cls=""):
    return {"label": label, "value": _show(value, kind), "raw": _raw(value, kind), "cls": cls}


def _section(title, cols, rows, foot=None, note=""):
    """cols = [(label, kind)], rows = [[value, ...]], foot = optional totals row (None for a blank cell)."""
    kinds = [k for _, k in cols]

    def cells(row):
        return [{"t": _show(v, k), "c": "n" if k in NUMERIC else ("wrap" if k == "w" else "")}
                for v, k in zip(row, kinds)]
    return {"title": title, "note": note, "cols": [{"label": l, "c": "n" if k in NUMERIC else ""} for l, k in cols],
            "rows": [cells(r) for r in rows], "foot": cells(foot) if foot else None,
            "head": [l for l, _ in cols], "csv": [[_raw(v, k) for v, k in zip(r, kinds)] for r in rows],
            "csv_foot": [_raw(v, k) for v, k in zip(foot, kinds)] if foot else None}


def _names(ids):
    return {u.pk: (u.get_full_name() or u.username) for u in User.objects.filter(pk__in=[i for i in ids if i])}


def _who(user):
    return (user.get_full_name() or user.username) if user else ""


def _label(choices, value):
    return dict(choices).get(value, value)


def _in_range(field, start, end):
    return {f"{field}__date__gte": start, f"{field}__date__lte": end}


def _paid(start, end):
    return Bill.objects.filter(status=Bill.Status.PAID, **_in_range("paid_at", start, end))


def _bill_lines(bills):
    for lines in bills.values_list("lines", flat=True):
        yield from (lines or [])


def _share(part, whole):
    return part / whole * 100 if whole else ZERO


# =============================================================================
# Sales
# =============================================================================

def sales_report(start, end, g):
    bills = _paid(start, end)
    a = bills.aggregate(n=Count("id"), net=Sum("subtotal"), tax=Sum("tax"), svc=Sum("service_charge"),
                        disc=Sum("discount_amount"), total=Sum("total"))
    n, total = a["n"] or 0, a["total"] or ZERO
    tiles = [_tile("Paid bills", n, "i"), _tile("Net sales", a["net"] or ZERO), _tile("Tax", a["tax"] or ZERO),
             _tile("Service charge", a["svc"] or ZERO), _tile("Discounts given", a["disc"] or ZERO),
             _tile("Total takings", total), _tile("Average bill", total / n if n else ZERO)]

    days = list(bills.annotate(d=TruncDate("paid_at")).values("d")
                .annotate(n=Count("id"), net=Sum("subtotal"), tax=Sum("tax"), total=Sum("total")).order_by("d"))
    by_day = _section("Sales by day", [("Date", "d"), ("Bills", "i"), ("Net", "m"), ("Tax", "m"), ("Total", "m")],
                      [[r["d"], r["n"], r["net"], r["tax"], r["total"]] for r in days],
                      foot=["Total", n, a["net"] or ZERO, a["tax"] or ZERO, total])

    cats = defaultdict(lambda: [0, ZERO])
    for ln in _bill_lines(bills):
        c = cats[ln.get("category") or "Uncategorised"]
        c[0] += int(ln.get("quantity") or 0)
        c[1] += _d(ln.get("net"))
    cat_net = sum((c[1] for c in cats.values()), ZERO)
    by_cat = _section("Net sales by menu category", [("Category", "t"), ("Qty sold", "i"), ("Net", "m"), ("Share", "p")],
                      [[k, v[0], v[1], _share(v[1], cat_net)] for k, v in sorted(cats.items(), key=lambda kv: -kv[1][1])],
                      foot=["Total", sum(c[0] for c in cats.values()), cat_net, 100 if cat_net else None],
                      note="Net of tax, with any bill discount already spread over the lines.")

    pays = list(Payment.objects.filter(**_in_range("created_at", start, end)).values("method")
                .annotate(n=Count("id"), t=Sum("amount")).order_by("method"))
    pay_total = sum((r["t"] or ZERO for r in pays), ZERO)
    by_method = _section("Takings by payment method", [("Method", "t"), ("Payments", "i"), ("Amount", "m"), ("Share", "p")],
                         [[_label(Payment.Method.choices, r["method"]), r["n"], r["t"], _share(r["t"] or ZERO, pay_total)]
                          for r in pays],
                         foot=["Total", sum(r["n"] for r in pays), pay_total, 100 if pay_total else None],
                         note="Money received in the period, net of refunds (refunds count against the day they were paid out).")

    types = bills.values("order__order_type").annotate(n=Count("id"), t=Sum("total")).order_by("-t")
    by_type = _section("Sales by order type", [("Type", "t"), ("Bills", "i"), ("Total", "m"), ("Share", "p")],
                       [[_label(Order.Type.choices, r["order__order_type"]), r["n"], r["t"], _share(r["t"] or ZERO, total)]
                        for r in types])
    return tiles, [by_day, by_cat, by_method, by_type], []


# =============================================================================
# Items sold
# =============================================================================

def items_report(start, end, g):
    sort = g.get("sort") if g.get("sort") in ("revenue", "qty") else "revenue"
    bills = _paid(start, end)
    sold = defaultdict(lambda: [0, ZERO])
    for ln in _bill_lines(bills):
        s = sold[(ln.get("name", "?"), ln.get("category") or "")]
        s[0] += int(ln.get("quantity") or 0)
        s[1] += _d(ln.get("net"))
    total_net = sum((s[1] for s in sold.values()), ZERO)
    total_qty = sum(s[0] for s in sold.values())
    order = (lambda kv: (-kv[1][0], -kv[1][1])) if sort == "qty" else (lambda kv: (-kv[1][1], -kv[1][0]))
    ranked = sorted(sold.items(), key=order)
    rows = [[n + 1, k[0], k[1], v[0], v[1], _share(v[1], total_net)] for n, (k, v) in enumerate(ranked[:ROW_CAP])]
    tiles = [_tile("Different items sold", len(sold), "i"), _tile("Units sold", total_qty, "i"),
             _tile("Net sales", total_net)]
    top = _section("Items sold", [("#", "i"), ("Item", "t"), ("Category", "t"), ("Qty", "i"), ("Net", "m"), ("Share", "p")],
                   rows, foot=["", "Total", "", total_qty, total_net, 100 if total_net else None],
                   note=f"Top {ROW_CAP} shown." if len(ranked) > ROW_CAP else "")
    sold_names = {k[0] for k in sold}
    idle = (MenuItem.objects.filter(is_available=True).exclude(name__in=sold_names).select_related("category")
            .order_by("category__name", "name")[:ROW_CAP])
    slow = _section("Menu items with no sales in this period",
                    [("Item", "t"), ("Category", "t"), ("Price", "m")],
                    [[m.name, m.category.name, m.price] for m in idle],
                    note="Available items only. Matched by the name printed on the bill.")
    selects = [{"name": "sort", "label": "Rank by", "value": sort,
                "options": [("revenue", "Net sales"), ("qty", "Quantity")]}]
    return tiles, [top, slow], selects


# =============================================================================
# Staff (waiters and cashiers)
# =============================================================================

def staff_report(start, end, g):
    bills = _paid(start, end)
    w = list(bills.values("order__waiter").annotate(n=Count("id"), total=Sum("total"), disc=Sum("discount_amount"))
             .order_by("-total"))
    voided = {r["order__waiter"]: r["qty"] for r in OrderItem.objects.filter(
        status=OrderItem.Status.VOID, **_in_range("created_at", start, end))
        .values("order__waiter").annotate(qty=Sum("quantity")).order_by()}
    waiter_names = _names([r["order__waiter"] for r in w] + list(voided))
    wrows = [[waiter_names.get(r["order__waiter"], "?"), r["n"], r["total"], r["total"] / r["n"] if r["n"] else ZERO,
              r["disc"] or ZERO, voided.get(r["order__waiter"], 0)] for r in w]
    for uid, qty in voided.items():                        # waiters who only had voids still show up
        if uid not in {r["order__waiter"] for r in w}:
            wrows.append([waiter_names.get(uid, "?"), 0, ZERO, ZERO, ZERO, qty])
    waiters = _section("Waiters", [("Waiter", "t"), ("Bills paid", "i"), ("Sales", "m"), ("Average bill", "m"),
                                   ("Discounts", "m"), ("Items voided", "i")], wrows,
                       foot=["Total", sum(r["n"] for r in w), sum((r["total"] for r in w), ZERO), None,
                             sum((r["disc"] or ZERO for r in w), ZERO), sum(voided.values())],
                       note="Sales are credited to the waiter who opened the order.")

    methods = [Payment.Method.CASH, Payment.Method.CARD, Payment.Method.MOBILE_MONEY, Payment.Method.VOUCHER]
    pivot = defaultdict(lambda: defaultdict(lambda: ZERO))
    for r in (Payment.objects.filter(**_in_range("created_at", start, end))
              .values("drawer__user", "method").annotate(t=Sum("amount")).order_by()):
        pivot[r["drawer__user"]][r["method"]] += r["t"] or ZERO
    var = {r["user"]: r for r in ShiftAssignment.objects.filter(
        role=ShiftAssignment.Role.CASHIER, counted_cash__isnull=False, **_in_range("ended_at", start, end))
        .values("user").annotate(n=Count("id"), v=Sum("variance")).order_by()}
    cashier_names = _names(list(pivot) + list(var))
    crows = []
    for uid in sorted(set(pivot) | set(var), key=lambda u: cashier_names.get(u, "")):
        by = pivot.get(uid, {})
        crows.append([cashier_names.get(uid, "?")] + [by.get(m, ZERO) for m in methods]
                     + [sum(by.values(), ZERO), var.get(uid, {}).get("n", 0), var.get(uid, {}).get("v") or ZERO])
    cashiers = _section("Cashiers", [("Cashier", "t")] + [(m.label, "m") for m in methods]
                        + [("Total", "m"), ("Drawers counted", "i"), ("Cash variance", "m")], crows,
                        note="Takings are net of refunds. A negative variance means the drawer was short.")

    tiles = [_tile("Waiters with sales", len(w), "i"), _tile("Cashiers taking payments", len(pivot), "i"),
             _tile("Total sales", sum((r["total"] for r in w), ZERO))]
    return tiles, [waiters, cashiers], []


# =============================================================================
# Shifts and cash drawers
# =============================================================================

def shifts_report(start, end, g):
    paid = Q(orders__bills__status=Bill.Status.PAID)
    shifts = list(Shift.objects.filter(**_in_range("opened_at", start, end))
                  .annotate(sales=Sum("orders__bills__total", filter=paid),
                            bill_count=Count("orders__bills", filter=paid, distinct=True))
                  .order_by("-opened_at")[:ROW_CAP])
    per = {r["shift"]: r for r in ShiftAssignment.objects.filter(shift__in=shifts).values("shift")
           .annotate(staff=Count("id"), v=Sum("variance")).order_by()}
    rows = [[str(s), s.opened_at, s.closed_at, s.get_status_display(), per.get(s.pk, {}).get("staff", 0),
             s.bill_count, s.sales or ZERO, per.get(s.pk, {}).get("v") or ZERO] for s in shifts]
    table = _section("Shifts", [("Shift", "t"), ("Opened", "dt"), ("Closed", "dt"), ("Status", "t"), ("Staff", "i"),
                                ("Bills paid", "i"), ("Sales", "m"), ("Cash variance", "m")], rows,
                     foot=["Total", None, None, None, None, sum(r[5] for r in rows), sum((r[6] for r in rows), ZERO),
                           sum((r[7] for r in rows), ZERO)])
    off = (ShiftAssignment.objects.filter(counted_cash__isnull=False, **_in_range("ended_at", start, end))
           .exclude(variance=0).select_related("shift", "user").order_by("-ended_at")[:ROW_CAP])
    drawers = _section("Drawers that did not balance",
                       [("Shift", "t"), ("Cashier", "t"), ("Terminal", "t"), ("Closed", "dt"), ("Expected", "m"),
                        ("Counted", "m"), ("Variance", "m")],
                       [[str(a.shift), _who(a.user), a.terminal, a.ended_at, a.expected_cash, a.counted_cash, a.variance]
                        for a in off])
    short = sum((a.variance for a in off if a.variance < 0), ZERO)
    over = sum((a.variance for a in off if a.variance > 0), ZERO)
    tiles = [_tile("Shifts", len(shifts), "i"), _tile("Drawers off", len(off), "i", "warn" if off else ""),
             _tile("Total short", short, cls="bad" if short else ""), _tile("Total over", over)]
    return tiles, [table, drawers], []


# =============================================================================
# Stock
# =============================================================================

def stock_report(start, end, g):
    items = list(_items_with_stock().filter(is_active=True))
    value = {i.pk: i.on_hand_total * i.avg_cost for i in items}
    low = [i for i in items if i.reorder_level > 0 and i.on_hand_total <= i.reorder_level]
    out = [i for i in items if i.on_hand_total <= 0]
    tiles = [_tile("Stock value (now)", sum(value.values(), ZERO)), _tile("Active items", len(items), "i"),
             _tile("At or below reorder level", len(low), "i", "warn" if low else ""),
             _tile("Out of stock", len(out), "i", "bad" if out else "")]

    cats = defaultdict(lambda: [0, ZERO])
    for i in items:
        c = cats[i.category or "Uncategorised"]
        c[0] += 1
        c[1] += value[i.pk]
    by_cat = _section("Stock value by category (as of now)", [("Category", "t"), ("Items", "i"), ("Value", "m")],
                      [[k, v[0], v[1]] for k, v in sorted(cats.items())],
                      foot=["Total", len(items), sum(value.values(), ZERO)],
                      note="Quantity on hand across all locations x weighted average cost. The full item-by-item list, "
                           "with a CSV export, is on the Stock balances page.")

    reorder = _section("Items to reorder",
                       [("Item", "t"), ("SKU", "t"), ("Unit", "t"), ("On hand", "n"), ("Reorder level", "n"),
                        ("Par level", "n"), ("Suggested order", "n")],
                       [[i.name, i.sku, i.stock_unit, i.on_hand_total, i.reorder_level, i.par_level,
                         max(i.par_level - i.on_hand_total, ZERO) if i.par_level else None]
                        for i in sorted(low, key=lambda i: i.on_hand_total - i.reorder_level)],
                       note="Suggested order is up to the par level, in stock units (blank when no par level is set).")

    moves = StockMovement.objects.filter(**_in_range("created_at", start, end))
    mv = moves.values("movement_type").annotate(n=Count("id"), v=Sum(_money(F("quantity") * F("unit_cost")))).order_by("movement_type")
    by_type = _section("Stock movements in the period", [("Type", "t"), ("Movements", "i"), ("Value", "m")],
                       [[_label(MovementType.choices, r["movement_type"]), r["n"], r["v"] or ZERO] for r in mv],
                       note="Value is quantity x cost at the time; stock leaving shows as negative.")

    loss = (moves.filter(movement_type__in=[MovementType.WASTE, MovementType.ADJUSTMENT, MovementType.RETURN])
            .select_related("item", "location", "user").order_by("-created_at")[:ROW_CAP])
    losses = _section("Waste, stock-take adjustments and supplier returns",
                      [("When", "dt"), ("Type", "t"), ("Item", "t"), ("Location", "t"), ("Qty", "n"), ("Value", "m"),
                       ("By", "t"), ("Reason", "w")],
                      [[m.created_at, _label(MovementType.choices, m.movement_type), m.item.name, m.location.name,
                        m.quantity, m.quantity * m.unit_cost, _who(m.user), m.reason] for m in loss])
    return tiles, [by_cat, reorder, by_type, losses], []


# =============================================================================
# Cost of sales and profit
# =============================================================================

def profit_report(start, end, g):
    net = _paid(start, end).aggregate(t=Sum("subtotal"))["t"] or ZERO
    sale_moves = StockMovement.objects.filter(movement_type__in=[MovementType.SALE, MovementType.SALE_REVERSAL],
                                              **_in_range("created_at", start, end))
    cost_expr = _money(F("quantity") * F("unit_cost"))
    cost = -(sale_moves.aggregate(t=Sum(cost_expr))["t"] or ZERO)       # sales are negative movements; voids reverse them
    gross = net - cost
    margin = _share(gross, net)
    tiles = [_tile("Net sales", net), _tile("Cost of sales", cost), _tile("Gross profit", gross, cls="ok" if gross >= 0 else "bad"),
             _tile("Gross margin", margin if net else None, "p"), _tile("Food & drink cost", _share(cost, net) if net else None, "p")]
    cats = list(sale_moves.values("item__category").annotate(v=Sum(cost_expr)).order_by("v"))
    by_cat = _section("Cost of sales by stock category", [("Category", "t"), ("Cost", "m"), ("Share", "p")],
                      [[r["item__category"] or "Uncategorised", -(r["v"] or ZERO), _share(-(r["v"] or ZERO), cost)] for r in cats],
                      foot=["Total", cost, 100 if cost else None])
    top = list(sale_moves.values("item__name", "item__stock_unit")
               .annotate(qty=Sum("quantity"), v=Sum(cost_expr)).order_by("v")[:25])
    by_item = _section("Biggest cost-of-sales items", [("Item", "t"), ("Unit", "t"), ("Qty used", "n"), ("Cost", "m")],
                       [[r["item__name"], r["item__stock_unit"], -(r["qty"] or ZERO), -(r["v"] or ZERO)] for r in top])
    by_cat["note"] = ("Cost comes from recipe deductions, valued at the average cost when the sale happened. Menu items "
                      "without a recipe have no cost here, so a missing recipe makes the margin look better than it is "
                      "(see the Recipes page for per-dish margins).")
    return tiles, [by_cat, by_item], []


# =============================================================================
# Purchasing
# =============================================================================

def purchases_report(start, end, g):
    lines = GoodsReceiptLine.objects.filter(**_in_range("receipt__created_at", start, end))
    val = _money(F("qty") * F("unit_cost"))
    total = lines.aggregate(t=Sum(val))["t"] or ZERO
    direct = lines.filter(receipt__po__number__startswith=DIRECT_PREFIX).aggregate(t=Sum(val))["t"] or ZERO
    receipts = lines.values("receipt").distinct().count()
    tiles = [_tile("Received value", total), _tile("Direct purchases", direct), _tile("Via LPOs", total - direct),
             _tile("Deliveries", receipts, "i")]

    sup = list(lines.values("receipt__po__supplier__name").annotate(n=Count("receipt", distinct=True), v=Sum(val)).order_by("-v"))
    by_sup = _section("Purchases by supplier", [("Supplier", "t"), ("Deliveries", "i"), ("Value", "m"), ("Share", "p")],
                      [[r["receipt__po__supplier__name"], r["n"], r["v"] or ZERO, _share(r["v"] or ZERO, total)] for r in sup],
                      foot=["Total", receipts, total, 100 if total else None])
    top = list(lines.values("po_line__item__name", "po_line__item__purchase_unit")
               .annotate(got=Sum("qty"), v=Sum(val)).order_by("-v")[:30])
    by_item = _section("Biggest purchases by item", [("Item", "t"), ("Unit", "t"), ("Qty received", "n"), ("Value", "m")],
                       [[r["po_line__item__name"], r["po_line__item__purchase_unit"], r["got"], r["v"] or ZERO] for r in top])
    open_pos = (PurchaseOrder.objects.filter(status__in=[PS.SENT, PS.PARTIAL]).exclude(number__startswith=DIRECT_PREFIX)
                .select_related("supplier").order_by("created_at"))
    waiting = _section("LPOs still waiting for goods (all dates)", [("LPO", "t"), ("Supplier", "t"), ("Status", "t"), ("Raised", "dt")],
                       [[p.number, p.supplier.name, p.get_status_display(), p.created_at] for p in open_pos])
    return tiles, [by_sup, by_item, waiting], []


# =============================================================================
# Exceptions: voids, cancelled bills, discounts, refunds
# =============================================================================

def exceptions_report(start, end, g):
    voids = list(AuditLog.objects.filter(action="item_voided", **_in_range("created_at", start, end))
                 .select_related("user").order_by("-created_at"))
    void_value = sum((_d(v.details.get("amount")) for v in voids), ZERO)
    void_qty = sum(int(_d(v.details.get("qty"))) for v in voids)
    order_no = dict(Order.objects.filter(pk__in=[v.ref_id for v in voids if v.ref_id.isdigit()]).values_list("pk", "number"))
    cancelled = list(Bill.objects.filter(status=Bill.Status.CANCELLED, **_in_range("cancelled_at", start, end))
                     .select_related("cancelled_by", "order").order_by("-cancelled_at"))
    disc = list(Bill.objects.filter(discount_amount__gt=0, **_in_range("created_at", start, end))
                .exclude(status=Bill.Status.CANCELLED).select_related("discount_approved_by").order_by("-created_at"))
    refunds = list(Payment.objects.filter(kind=Payment.Kind.REFUND, **_in_range("created_at", start, end))
                   .select_related("bill", "user").order_by("-created_at"))
    disc_total = sum((b.discount_amount for b in disc), ZERO)
    refund_total = -sum((p.amount for p in refunds), ZERO)
    tiles = [_tile("Items voided", void_qty, "i", "warn" if voids else ""), _tile("Voided value", void_value),
             _tile("Bills cancelled", len(cancelled), "i", "warn" if cancelled else ""),
             _tile("Discounts given", disc_total), _tile("Refunds paid", refund_total, cls="bad" if refunds else "")]

    by_user = defaultdict(lambda: [0, ZERO])
    for v in voids:
        u = by_user[_who(v.user) or "Unknown"]
        u[0] += int(_d(v.details.get("qty")))
        u[1] += _d(v.details.get("amount"))
    by_approver = _section("Voids by approver", [("Approved by", "t"), ("Items", "i"), ("Value", "m")],
                           [[k, v[0], v[1]] for k, v in sorted(by_user.items(), key=lambda kv: -kv[1][1])],
                           foot=["Total", void_qty, void_value])
    void_list = _section("Voided items", [("When", "dt"), ("Order", "t"), ("Item", "t"), ("Qty", "i"), ("Value", "m"),
                                          ("Approved by", "t"), ("Reason", "w")],
                         [[v.created_at, order_no.get(int(v.ref_id)) if v.ref_id.isdigit() else "", v.details.get("item", ""),
                           int(_d(v.details.get("qty"))), _d(v.details.get("amount")), _who(v.user),
                           v.details.get("reason", "")] for v in voids[:ROW_CAP]])
    canc = _section("Cancelled bills", [("When", "dt"), ("Bill", "t"), ("Order", "t"), ("Total", "m"), ("Cancelled by", "t"),
                                        ("Reason", "w")],
                    [[b.cancelled_at, b.number, b.order.number, b.total, _who(b.cancelled_by), b.cancel_reason]
                     for b in cancelled[:ROW_CAP]])
    dlist = _section("Discounts", [("When", "dt"), ("Bill", "t"), ("Status", "t"), ("Discount", "m"), ("Bill total", "m"),
                                   ("Approved by", "t"), ("Reason", "w")],
                     [[b.created_at, b.number, b.get_status_display(), b.discount_amount, b.total,
                       _who(b.discount_approved_by), b.discount_reason] for b in disc[:ROW_CAP]],
                     foot=["Total", None, None, disc_total, None, None, None])
    rlist = _section("Refunds", [("When", "dt"), ("Bill", "t"), ("Method", "t"), ("Amount", "m"), ("By", "t"), ("Reason", "w")],
                     [[p.created_at, p.bill.number, p.get_method_display(), -p.amount, _who(p.user), p.reason]
                      for p in refunds[:ROW_CAP]], foot=["Total", None, None, refund_total, None, None])
    return tiles, [by_approver, void_list, canc, dlist, rlist], []


# =============================================================================
# End-of-day (Z) reports
# =============================================================================

def zreports_report(start, end, g):
    zs = list(ZReport.objects.filter(**_in_range("period_end", start, end)).select_related("closed_by").order_by("-period_end"))
    rows = []
    for z in zs:
        s, v, sh = z.data.get("sales", {}), z.data.get("voids", {}), z.data.get("shifts", {})
        rows.append([z.number, z.period_start, z.period_end, _who(z.closed_by), int(_d(s.get("bills"))),
                     _d(s.get("net")), _d(s.get("discounts")), _d(s.get("gross_total")), int(_d(v.get("count"))),
                     _d(sh.get("cash_variance"))])
    col = lambda n: sum((r[n] for r in rows), ZERO)    # noqa: E731
    table = _section("Z reports", [("Z no.", "t"), ("From", "dt"), ("To", "dt"), ("Closed by", "t"), ("Bills", "i"),
                                   ("Net sales", "m"), ("Discounts", "m"), ("Total takings", "m"), ("Voids", "i"),
                                   ("Cash variance", "m")], rows,
                     foot=["Total", None, None, None, col(4), col(5), col(6), col(7), col(8), col(9)],
                     note="Z reports are snapshots taken when the day was closed; they never change afterwards.")
    tiles = [_tile("Days closed", len(zs), "i"), _tile("Total takings", col(7)), _tile("Cash variance", col(9))]
    return tiles, [table], []


# =============================================================================
# Registry + views
# =============================================================================

REPORTS = [
    ("Sales", [
        ("sales", "Sales summary", "Takings by day, menu category, payment method and order type.", sales_report),
        ("items", "Items sold", "Best sellers by quantity or revenue, and menu items that did not sell.", items_report),
        ("profit", "Cost of sales & profit", "Net sales against the cost of the ingredients used.", profit_report),
    ]),
    ("People & shifts", [
        ("staff", "Waiters & cashiers", "Sales per waiter; takings and cash variance per cashier.", staff_report),
        ("shifts", "Shifts & cash drawers", "Every shift with its sales, plus drawers that did not balance.", shifts_report),
    ]),
    ("Stock & purchasing", [
        ("stock", "Stock", "Stock value, items to reorder, movements, waste and stock-take adjustments.", stock_report),
        ("purchases", "Purchases", "What was received, from which supplier, and LPOs still outstanding.", purchases_report),
    ]),
    ("Control", [
        ("exceptions", "Voids, discounts & refunds", "Who voided, cancelled, discounted or refunded what, and why.", exceptions_report),
        ("zreports", "End-of-day (Z) reports", "The closed days and their key figures.", zreports_report),
    ]),
]
BY_SLUG = {slug: {"slug": slug, "title": title, "blurb": blurb, "fn": fn, "group": group}
           for group, entries in REPORTS for slug, title, blurb, fn in entries}


def _csv(spec, start, end, tiles, sections):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{spec["slug"]}-{start}-to-{end}.csv"'
    resp.write("\ufeff")                                        # so Excel reads UTF-8 correctly
    w = csv.writer(resp)
    w.writerow([spec["title"], f"{start} to {end}"])
    for t in tiles:
        w.writerow([t["label"], t["raw"]])
    for s in sections:
        w.writerow([])
        w.writerow([s["title"]])
        w.writerow(s["head"])
        w.writerows(s["csv"])
        if s["csv_foot"] and s["csv"]:
            w.writerow(s["csv_foot"])
    return resp


@manager_required
def index(request):
    today = timezone.localdate()
    month = today.replace(day=1)
    day_sales = _paid(today, today).aggregate(n=Count("id"), t=Sum("total"))
    month_sales = _paid(month, today).aggregate(n=Count("id"), t=Sum("total"))
    tiles = [_tile("Sales today", day_sales["t"] or ZERO), _tile("Bills paid today", day_sales["n"] or 0, "i"),
             _tile("Sales this month", month_sales["t"] or ZERO), _tile("Bills paid this month", month_sales["n"] or 0, "i")]
    groups = [(g, [BY_SLUG[e[0]] for e in entries]) for g, entries in REPORTS]
    return _page(request, "reports.html", {"tiles": tiles, "groups": groups, "active": "reports"})


@manager_required
def report(request, slug):
    spec = BY_SLUG.get(slug)
    if spec is None:
        raise Http404("No such report")
    start, end = _period(request)
    tiles, sections, selects = spec["fn"](start, end, request.GET)
    if request.GET.get("export") == "csv":
        return _csv(spec, start, end, tiles, sections)
    params = request.GET.copy()
    params.pop("export", None)
    return _page(request, "report.html", {
        "report": spec, "start": start, "end": end, "tiles": tiles, "sections": sections, "selects": selects,
        "presets": _presets(), "qs": params.urlencode(), "active": f"report_{slug}"})