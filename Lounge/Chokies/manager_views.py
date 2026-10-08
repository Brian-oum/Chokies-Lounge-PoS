"""Chokies POS - manager back office: purchasing, inventory, voids and discounts.

Server-rendered pages (one template each, under templates/Chokies/manager/) that drive the services
already in views.py (receive_goods, start_stock_take, void_item, cancel_bill, ...) plus the few the back
office still needed: direct purchases, LPO cancel / close, issue + write-off, and bill discounts.

Nothing here needs a migration. A *direct purchase* is a purchase order numbered DP-xxxxxx that is created
and received in one step, so it flows through the same ledger, average cost and goods-receipt records as an
LPO (a Local Purchase Order is a PurchaseOrder numbered PO-xxxxxx).

Sections: access - helpers - services - dashboard - purchases - LPOs - receive - items - balances -
          stock takes - issue - recipes - voids - discounts
"""
import csv
from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import DecimalField, ExpressionWrapper, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .models import (AuditLog, Bill, Category, DocumentSequence, DomainError, GoodsReceipt, GoodsReceiptLine, InvalidState,
                     Location, MenuItem, MovementType, Order, OrderItem, PurchaseOrder, PurchaseOrderLine, RecipeLine,
                     Station, StockBalance, StockItem, StockMovement, StockTake, StockTakeLine, Supplier, audit)
from .views import (POS_HOME_URL, D, _num, _require_manager, approve_stock_take, cancel_bill, compute_bill_amounts,
                    create_purchase_order, q, receive_goods, record_count, record_movement, send_purchase_order,
                    start_stock_take, void_item)

LOGIN_URL = getattr(settings, "POS_LOGIN_URL", "/pin-login/")
DIRECT_PREFIX = "DP-"          # purchase orders created by "New purchase" (no LPO)
PS = PurchaseOrder.Status


# =============================================================================
# Access
# =============================================================================

def manager_required(view):
    """Session login (the PIN login page sets it). Anonymous -> sign-in page, non-managers -> 403."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect(f"{LOGIN_URL}?{urlencode({'next': request.get_full_path()})}")
        if not user.is_manager:
            raise PermissionDenied("Managers only")
        return view(request, *args, **kwargs)
    return wrapped


# =============================================================================
# Helpers
# =============================================================================

NAV_KEY = {"purchase_detail": "purchases", "purchase_form": "purchase_new", "lpo_form": "lpo_new", "lpo_detail": "lpos",
           "item_form": "items", "item_detail": "items", "stock_take": "stock_takes",
           "recipe_edit": "recipes"}   # template -> highlighted menu entry


def _page(request, template, ctx=None, status=200):
    key = template.removesuffix(".html")
    return render(request, f"Chokies/manager/{template}",
                  {"pos_home": POS_HOME_URL, "active": NAV_KEY.get(key, key), **(ctx or {})}, status=status)


def _try(request, fn):
    """Run fn() atomically. Returns (result, ok). A domain error becomes a flash message and rolls back everything."""
    try:
        with transaction.atomic():
            return fn(), True
    except DomainError as e:
        messages.error(request, str(e))
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    except ValueError as e:
        messages.error(request, str(e))
    return None, False


def _back(request, default):
    nxt = request.POST.get("next", "")
    ok = nxt and url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure())
    return redirect(nxt if ok else default)


def _fmt(d):
    """5.0000 -> '5', 2.5000 -> '2.5'."""
    s = f"{D(d):f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _rows(post, *names):
    """Parallel form columns (item[], qty[], cost[]) -> list of dicts. Rows with no item and no qty are dropped."""
    cols = [post.getlist(n) for n in names]
    out = []
    for vals in zip(*cols):
        vals = [v.strip() for v in vals]
        if not vals[0] and not vals[1]:
            continue
        out.append(dict(zip(names, vals)))
    return out


def _lines(post, cost=True):
    """Parse item/qty(/cost) columns -> [(StockItem, qty, cost)]. Raises InvalidState with a clear message."""
    rows = _rows(post, "item", "qty", "cost") if cost else _rows(post, "item", "qty")
    if not rows:
        raise InvalidState("Add at least one line")
    try:
        items = StockItem.objects.in_bulk([int(r["item"]) for r in rows])
    except (TypeError, ValueError):
        raise InvalidState("Pick an item on every line")
    out = []
    for n, r in enumerate(rows, 1):
        item = items.get(int(r["item"]))
        if item is None:
            raise InvalidState(f"Line {n}: unknown item")
        qty = _num(r["qty"], f"Line {n} quantity")
        if qty <= 0:
            raise InvalidState(f"Line {n}: quantity must be more than zero")
        unit_cost = None
        if cost:
            unit_cost = _num(r["cost"], f"Line {n} cost")
            if unit_cost < 0:
                raise InvalidState(f"Line {n}: cost cannot be negative")
        out.append((item, qty, unit_cost))
    return out


def _supplier_from(post):
    """An existing supplier (pk) or a new one typed in (matched by name so we never duplicate)."""
    name = post.get("new_supplier", "").strip()
    if name:
        s = Supplier.objects.filter(name__iexact=name).first()
        return s or Supplier.objects.create(name=name, phone=post.get("new_supplier_phone", "").strip())
    try:
        return Supplier.objects.get(pk=int(post.get("supplier", "")))
    except (TypeError, ValueError, Supplier.DoesNotExist):
        raise InvalidState("Pick a supplier or type a new one")


def _location(pk, what="location"):
    try:
        return Location.objects.get(pk=int(pk))
    except (TypeError, ValueError, Location.DoesNotExist):
        raise InvalidState(f"Pick a {what}")


def _default_location(locations):
    locations = list(locations)
    return next((l for l in locations if "store" in l.code.lower() or "store" in l.name.lower()),
                locations[0] if locations else None)


def _paginate(request, qs, size=50):
    return Paginator(qs, size).get_page(request.GET.get("page"))


def _qs_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _date_filter(qs, request, field):
    f, t = request.GET.get("from"), request.GET.get("to")
    if f:
        qs = qs.filter(**{f"{field}__date__gte": f})
    if t:
        qs = qs.filter(**{f"{field}__date__lte": t})
    return qs


def _money(expr):
    return ExpressionWrapper(expr, output_field=DecimalField(max_digits=24, decimal_places=4))


def _items_with_stock():
    zero = Value(D(0), output_field=DecimalField(max_digits=18, decimal_places=6))
    return StockItem.objects.annotate(
        on_hand_total=Coalesce(Sum("balances__quantity"), zero, output_field=DecimalField(max_digits=18, decimal_places=6)))


def _item_options():
    """Active items for the line pickers, with what the JS needs to label units and suggest a cost."""
    rows = []
    for i in StockItem.objects.filter(is_active=True).order_by("name"):
        rows.append({"id": i.pk, "label": f"{i.name} ({i.sku})", "punit": i.purchase_unit, "sunit": i.stock_unit,
                     "factor": _fmt(i.stock_units_per_purchase_unit),
                     "cost": f"{q(i.avg_cost * i.stock_units_per_purchase_unit):f}"})
    return rows


def _balance_map():
    """{item_id: {location_id: qty}} for the availability hints on the issue page."""
    out = {}
    for item_id, loc_id, qty in StockBalance.objects.values_list("item_id", "location_id", "quantity"):
        out.setdefault(str(item_id), {})[str(loc_id)] = _fmt(qty)
    return out


# =============================================================================
# Services the back office needs on top of views.py
# =============================================================================

@transaction.atomic
def create_lpo(*, supplier, user, lines, notes="", send=False):
    """lines: [(item, qty in purchase units, cost per purchase unit)]."""
    _require_manager(user, "Creating an LPO")
    po = create_purchase_order(supplier=supplier, user=user, lines=lines, notes=notes)
    if send:
        send_purchase_order(po)
    audit(user, "lpo_created", po, supplier=supplier.name, sent=send, lines=len(lines))
    return po


@transaction.atomic
def direct_purchase(*, supplier, location, user, lines, invoice_no="", notes=""):
    """Buy and receive in one step, with no LPO. Same ledger, average cost and goods receipt as an LPO delivery."""
    _require_manager(user, "Recording a purchase")
    po = PurchaseOrder.objects.create(
        number=DocumentSequence.next("direct-purchase", DIRECT_PREFIX), supplier=supplier, status=PS.SENT,
        created_by=user, notes=notes or "Direct purchase")
    made = [PurchaseOrderLine.objects.create(po=po, item=i, qty_ordered=qty, unit_cost=cost) for i, qty, cost in lines]
    receive_goods(po=po, location=location, user=user, invoice_no=invoice_no,
                  lines=[{"po_line": l, "qty": l.qty_ordered, "unit_cost": l.unit_cost} for l in made])
    audit(user, "direct_purchase", po, supplier=supplier.name, invoice=invoice_no, lines=len(made))
    return GoodsReceipt.objects.filter(po=po).order_by("-id").first()


@transaction.atomic
def cancel_lpo(*, po, user, reason):
    _require_manager(user, "Cancelling an LPO")
    if not reason:
        raise InvalidState("A reason is required")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status not in (PS.DRAFT, PS.SENT) or po.receipts.exists():
        raise InvalidState("Only an LPO with nothing received can be cancelled. Use 'Close' for a part-delivered one.")
    po.status = PS.CANCELLED
    po.notes = f"{po.notes}\nCancelled: {reason}".strip()
    po.save(update_fields=["status", "notes", "updated_at"])
    audit(user, "lpo_cancelled", po, reason=reason)
    return po


@transaction.atomic
def close_lpo(*, po, user, reason):
    """Supplier will not deliver the rest of a part-delivered LPO."""
    _require_manager(user, "Closing an LPO")
    if not reason:
        raise InvalidState("A reason is required")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status != PS.PARTIAL:
        raise InvalidState("Only a part-delivered LPO can be closed")
    po.status = PS.RECEIVED
    po.notes = f"{po.notes}\nClosed short: {reason}".strip()
    po.save(update_fields=["status", "notes", "updated_at"])
    audit(user, "lpo_closed_short", po, reason=reason)
    return po


@transaction.atomic
def receive_lpo(*, po, location, user, rows, invoice_no=""):
    """rows: [(po_line_id, qty, cost or None)] in purchase units. You cannot receive more than is outstanding."""
    _require_manager(user, "Receiving stock")
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    lines = {l.pk: l for l in po.lines.select_related("item").select_for_update()}
    todo = []
    for pk, qty, cost in rows:
        line = lines.get(pk)
        if line is None:
            raise InvalidState("A line does not belong to this LPO")
        if qty <= 0:
            continue
        left = line.qty_ordered - line.qty_received
        if qty > left:
            raise InvalidState(f"{line.item.name}: only {_fmt(left)} {line.item.purchase_unit} still to receive")
        todo.append({"po_line": line, "qty": qty, "unit_cost": cost if cost is not None else line.unit_cost})
    if not todo:
        raise InvalidState("Enter a quantity for at least one line")
    receive_goods(po=po, location=location, user=user, lines=todo, invoice_no=invoice_no)
    return GoodsReceipt.objects.filter(po=po).order_by("-id").first()


def _require_available(location, lines, what="issue"):
    """Block moving more than a location actually holds, whatever ALLOW_NEGATIVE_STOCK says.
    (That setting is for POS sales; the back office must never issue stock that is not there.)"""
    need = {}
    for item, qty in lines:
        need[item] = need.get(item, D(0)) + qty
    short = [f"{item.name} (have {_fmt(item.on_hand(location))}, need {_fmt(qty)})"
             for item, qty in need.items() if qty > item.on_hand(location)]
    if short:
        raise InvalidState(f"Not enough stock in {location.name} to {what}: " + "; ".join(short))


@transaction.atomic
def issue_stock(*, from_location, to_location, lines, user, note=""):
    """Move stock from the store to a department (kitchen, bar...). One ISS- reference for the whole issue.
    lines: [(item, qty in stock units)]. Average cost is unchanged by a transfer."""
    _require_manager(user, "Issuing stock")
    if from_location == to_location:
        raise InvalidState("Choose two different locations")
    _require_available(from_location, lines)
    ref = DocumentSequence.next("issue", "ISS-")
    why = note or f"Issue to {to_location.name}"
    for item, qty in lines:
        out = record_movement(item=item, location=from_location, quantity=-qty, movement_type=MovementType.TRANSFER,
                              user=user, ref_type="issue", ref_id=ref, reason=why)
        record_movement(item=item, location=to_location, quantity=qty, movement_type=MovementType.TRANSFER,
                        unit_cost=out.unit_cost, user=user, ref_type="issue", ref_id=ref, reason=why)
    audit(user, "stock_issued", None, number=ref, source=from_location.name, to=to_location.name, lines=len(lines))
    return ref


@transaction.atomic
def write_off_stock(*, location, lines, reason, user):
    """Breakage, spoilage, staff use. Stock leaves the location and the ledger records it as WASTE."""
    _require_manager(user, "Writing off stock")
    if not reason:
        raise InvalidState("A write-off needs a reason")
    _require_available(location, lines, "write off")
    ref = DocumentSequence.next("writeoff", "WO-")
    for item, qty in lines:
        record_movement(item=item, location=location, quantity=-qty, movement_type=MovementType.WASTE,
                        user=user, ref_type="writeoff", ref_id=ref, reason=reason)
    audit(user, "stock_written_off", None, number=ref, location=location.name, reason=reason, lines=len(lines))
    return ref


def _reprice_bill(bill, order, user, *, discount, reason, items_note=""):
    """Recompute an unpaid bill with a discount (0 = none) and save it. Guards the amounts already paid."""
    a = compute_bill_amounts(order, discount_amount=discount)
    if a["total"] <= bill.amount_paid:
        raise InvalidState("That would bring the bill down to what has already been paid. "
                           "Use a smaller discount, or cancel the bill.")
    before = bill.total
    bill.lines, bill.subtotal, bill.tax = a["lines"], a["subtotal"], a["tax"]
    bill.discount_amount, bill.service_charge, bill.total = a["discount"], a["service_charge"], a["total"]
    bill.discount_reason = (f"{reason} [{items_note}]" if items_note else reason)[:200] if a["discount"] else ""
    bill.discount_approved_by = user if a["discount"] else None
    bill.save()
    order.version += 1
    order.save(update_fields=["version", "updated_at"])
    return before


@transaction.atomic
def apply_bill_discount(*, bill, user, reason, percent=None, amount=None, line_indexes=None):
    """Set the discount on an unpaid bill (replacing any earlier one; discounts never stack).
    line_indexes: apply to only those bill lines (a discount on specific products), otherwise the whole bill.
    The money amount is spread over the lines by the normal bill maths, so VAT stays right."""
    _require_manager(user, "Giving a discount")
    if not reason:
        raise InvalidState("A discount needs a reason")
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status != Bill.Status.OPEN:
        raise InvalidState(f"Bill {bill.number} is {bill.get_status_display().lower()}; only unpaid bills can be discounted")
    order = Order.objects.select_for_update().get(pk=bill.order_id)
    chosen = [bill.lines[i] for i in line_indexes] if line_indexes else bill.lines
    basis = sum((D(l["line_total"]) for l in chosen), D(0))
    if percent is not None:
        if not (0 < percent <= 100):
            raise InvalidState("A percentage must be between 0 and 100")
        disc = q(basis * percent / 100)
    elif amount is not None:
        disc = q(amount)
    else:
        raise InvalidState("Enter a percentage or an amount")
    if disc <= 0 or disc > basis:
        raise InvalidState(f"The discount must be more than 0 and at most {basis:,.2f} (the {'selected items' if line_indexes else 'bill'})")
    note = ", ".join(l["name"] for l in chosen)[:80] if line_indexes else ""
    before = _reprice_bill(bill, order, user, discount=disc, reason=reason, items_note=note)
    audit(user, "discount_applied", bill, amount=str(disc), reason=reason, scope=note or "whole bill",
          total_before=str(before), total_after=str(bill.total))
    return bill


@transaction.atomic
def remove_bill_discount(*, bill, user, reason):
    _require_manager(user, "Removing a discount")
    if not reason:
        raise InvalidState("A reason is required")
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status != Bill.Status.OPEN:
        raise InvalidState("Only unpaid bills can be changed")
    if not bill.discount_amount:
        raise InvalidState("This bill has no discount")
    order = Order.objects.select_for_update().get(pk=bill.order_id)
    gone = bill.discount_amount
    before = _reprice_bill(bill, order, user, discount=D(0), reason="")
    audit(user, "discount_removed", bill, amount=str(gone), reason=reason, total_before=str(before),
          total_after=str(bill.total))
    return bill


# =============================================================================
# Dashboard
# =============================================================================

@manager_required
def dashboard(request):
    today = timezone.localdate()
    month_start = today.replace(day=1)
    stock = _items_with_stock().filter(is_active=True)
    low = stock.filter(reorder_level__gt=0, on_hand_total__lte=F("reorder_level"))
    spent = GoodsReceiptLine.objects.filter(receipt__created_at__date__gte=month_start).aggregate(
        t=Sum(_money(F("qty") * F("unit_cost"))))["t"] or D(0)
    logs = AuditLog.objects.filter(created_at__date=today)
    ctx = {
        "low_count": low.count(), "low_items": low.order_by("on_hand_total")[:8],
        "draft_lpos": PurchaseOrder.objects.filter(status=PS.DRAFT).exclude(number__startswith=DIRECT_PREFIX).count(),
        "awaiting": PurchaseOrder.objects.filter(status__in=[PS.SENT, PS.PARTIAL]).exclude(
            number__startswith=DIRECT_PREFIX).count(),
        "takes_open": StockTake.objects.filter(status=StockTake.Status.DRAFT).count(),
        "voids_today": logs.filter(action="item_voided").count(),
        "discounts_today": logs.filter(action__in=["discount_given", "discount_applied"]).count(),
        "open_bills": Bill.objects.filter(status=Bill.Status.OPEN).count(),
        "spent_month": spent, "month": month_start,
    }
    return _page(request, "dashboard.html", ctx)


# =============================================================================
# Purchases (everything received: direct purchases and LPO deliveries)
# =============================================================================

@manager_required
def purchases(request):
    g = request.GET
    base = GoodsReceipt.objects.all()
    if g.get("q"):
        s = g["q"].strip()
        base = base.filter(Q(number__icontains=s) | Q(invoice_no__icontains=s) | Q(po__number__icontains=s)
                           | Q(po__supplier__name__icontains=s))
    if g.get("supplier"):
        base = base.filter(po__supplier_id=g["supplier"])
    if g.get("kind") == "direct":
        base = base.filter(po__number__startswith=DIRECT_PREFIX)
    elif g.get("kind") == "lpo":
        base = base.exclude(po__number__startswith=DIRECT_PREFIX)
    base = _date_filter(base, request, "created_at")
    total = GoodsReceiptLine.objects.filter(receipt__in=base).aggregate(t=Sum(_money(F("qty") * F("unit_cost"))))["t"] or D(0)
    rows = base.select_related("po__supplier", "location", "received_by").annotate(
        value=Sum(_money(F("lines__qty") * F("lines__unit_cost")))).order_by("-created_at", "-id")
    page = _paginate(request, rows)
    for r in page:
        r.direct = r.po.number.startswith(DIRECT_PREFIX)
    return _page(request, "purchases.html", {"page": page, "total": total, "suppliers": Supplier.objects.order_by("name"),
                                              "f": g, "qs": _qs_without_page(request)})


@manager_required
def purchase_detail(request, pk):
    r = get_object_or_404(GoodsReceipt.objects.select_related("po__supplier", "location", "received_by"), pk=pk)
    lines = []
    for l in r.lines.select_related("po_line__item"):
        item = l.po_line.item
        lines.append({"item": item, "qty": l.qty, "unit": item.purchase_unit, "cost": l.unit_cost,
                      "value": l.qty * l.unit_cost, "stock_qty": l.qty * item.stock_units_per_purchase_unit})
    return _page(request, "purchase_detail.html", {"r": r, "lines": lines, "direct": r.po.number.startswith(DIRECT_PREFIX),
                                                    "total": sum((x["value"] for x in lines), D(0))})


@manager_required
def purchase_new(request):
    """Direct purchase: bought and received in one step (market run, cash purchase, no LPO)."""
    locations = Location.objects.order_by("name")
    ctx = {"suppliers": Supplier.objects.order_by("name"), "locations": locations, "items": _item_options(),
           "rows": [], "sel_location": _default_location(locations)}
    if request.method == "POST":
        p = request.POST
        def run():
            return direct_purchase(supplier=_supplier_from(p), location=_location(p.get("location")), user=request.user,
                                   lines=_lines(p), invoice_no=p.get("invoice_no", "").strip(), notes=p.get("notes", "").strip())
        receipt, ok = _try(request, run)
        if ok:
            messages.success(request, f"Purchase {receipt.po.number} received into {receipt.location.name} ({receipt.number}).")
            return redirect("manager:purchase_detail", pk=receipt.pk)
        ctx.update(rows=_rows(p, "item", "qty", "cost"), post=p, sel_location=Location.objects.filter(pk=p.get("location") or 0).first())
        return _page(request, "purchase_form.html", ctx, status=422)
    return _page(request, "purchase_form.html", ctx)


# =============================================================================
# LPOs
# =============================================================================

def _lpo_base():
    return PurchaseOrder.objects.exclude(number__startswith=DIRECT_PREFIX)


@manager_required
def lpos(request):
    g = request.GET
    base = _lpo_base()
    if g.get("status"):
        base = base.filter(status=g["status"])
    if g.get("supplier"):
        base = base.filter(supplier_id=g["supplier"])
    if g.get("q"):
        base = base.filter(Q(number__icontains=g["q"].strip()) | Q(supplier__name__icontains=g["q"].strip()))
    base = _date_filter(base, request, "created_at")
    rows = base.select_related("supplier", "created_by").annotate(
        value=Sum(_money(F("lines__qty_ordered") * F("lines__unit_cost"))),
        ordered=Sum("lines__qty_ordered"), received=Sum("lines__qty_received")).order_by("-created_at", "-id")
    page = _paginate(request, rows)
    for po in page:
        po.pct = int(100 * (po.received or 0) / po.ordered) if po.ordered else 0
    return _page(request, "lpos.html", {"page": page, "suppliers": Supplier.objects.order_by("name"),
                                         "statuses": PurchaseOrder.Status.choices, "f": g, "qs": _qs_without_page(request)})


@manager_required
def lpo_new(request):
    ctx = {"suppliers": Supplier.objects.order_by("name"), "items": _item_options(), "rows": []}
    if request.method == "POST":
        p = request.POST
        po, ok = _try(request, lambda: create_lpo(supplier=_supplier_from(p), user=request.user, lines=_lines(p),
                                                  notes=p.get("notes", "").strip(), send=p.get("then") == "send"))
        if ok:
            messages.success(request, f"{po.number} created" + (" and marked as sent." if po.status == PS.SENT else " as a draft."))
            return redirect("manager:lpo_detail", pk=po.pk)
        ctx.update(rows=_rows(p, "item", "qty", "cost"), post=p)
        return _page(request, "lpo_form.html", ctx, status=422)
    return _page(request, "lpo_form.html", ctx)


@manager_required
def lpo_detail(request, pk):
    po = get_object_or_404(_lpo_base().select_related("supplier", "created_by"), pk=pk)
    lines = []
    for l in po.lines.select_related("item"):
        lines.append({"line": l, "left": max(l.qty_ordered - l.qty_received, D(0)), "value": l.qty_ordered * l.unit_cost})
    receipts = po.receipts.select_related("location", "received_by").annotate(
        value=Sum(_money(F("lines__qty") * F("lines__unit_cost")))).order_by("-id")
    return _page(request, "lpo_detail.html", {"po": po, "lines": lines, "receipts": receipts,
                                               "total": sum((x["value"] for x in lines), D(0)),
                                               "can_receive": po.status in (PS.SENT, PS.PARTIAL)})


@manager_required
@require_POST
def lpo_action(request, pk, action):
    po = get_object_or_404(_lpo_base(), pk=pk)
    reason = request.POST.get("reason", "").strip()
    if action == "send":
        def run():
            send_purchase_order(po)
            audit(request.user, "lpo_sent", po)
        done = f"{po.number} marked as sent."
    elif action == "cancel":
        run, done = (lambda: cancel_lpo(po=po, user=request.user, reason=reason)), f"{po.number} cancelled."
    elif action == "close":
        run, done = (lambda: close_lpo(po=po, user=request.user, reason=reason)), f"{po.number} closed."
    else:
        raise PermissionDenied("Unknown action")
    _, ok = _try(request, run)
    if ok:
        messages.success(request, done)
    return _back(request, reverse("manager:lpos"))


# =============================================================================
# Receive stock against an LPO
# =============================================================================

@manager_required
def receive(request):
    """No ?po -> the LPOs waiting for delivery. ?po=<id> -> the receiving form for that LPO."""
    waiting = _lpo_base().filter(status__in=[PS.SENT, PS.PARTIAL]).select_related("supplier").order_by("created_at")
    po = _lpo_base().filter(pk=request.GET.get("po") or request.POST.get("po") or 0).select_related("supplier").first()
    if po is None:
        return _page(request, "receive.html", {"waiting": waiting})
    locations = Location.objects.order_by("name")
    lines = []
    for l in po.lines.select_related("item"):
        left = max(l.qty_ordered - l.qty_received, D(0))
        lines.append({"line": l, "left": left, "qty": _fmt(left), "cost": ""})   # default: the whole outstanding quantity
    ctx = {"waiting": waiting, "po": po, "lines": lines, "locations": locations,
           "sel_location": _default_location(locations), "can_receive": po.status in (PS.SENT, PS.PARTIAL)}
    if request.method == "POST":
        p = request.POST
        def run():
            rows = []
            for x in lines:
                raw = p.get(f"qty_{x['line'].pk}", "").strip()
                if not raw:
                    continue
                cost = p.get(f"cost_{x['line'].pk}", "").strip()
                rows.append((x["line"].pk, _num(raw, f"{x['line'].item.name} quantity"),
                             _num(cost, "cost") if cost else None))
            return receive_lpo(po=po, location=_location(p.get("location")), user=request.user, rows=rows,
                               invoice_no=p.get("invoice_no", "").strip())
        receipt, ok = _try(request, run)
        if ok:
            messages.success(request, f"Received into {receipt.location.name} as {receipt.number}.")
            return redirect("manager:purchase_detail", pk=receipt.pk)
        for x in lines:   # show back what was typed
            x["qty"], x["cost"] = p.get(f"qty_{x['line'].pk}", ""), p.get(f"cost_{x['line'].pk}", "")
        ctx.update(post=p, sel_location=Location.objects.filter(pk=p.get("location") or 0).first())
        return _page(request, "receive.html", ctx, status=422)
    return _page(request, "receive.html", ctx)


# =============================================================================
# Items
# =============================================================================

class StockItemForm(forms.ModelForm):
    class Meta:
        model = StockItem
        fields = ["sku", "name", "category", "stock_unit", "purchase_unit", "stock_units_per_purchase_unit",
                  "recipe_unit", "recipe_units_per_stock_unit", "reorder_level", "par_level", "is_active"]
        labels = {"stock_unit": "Stock unit (what you count)", "purchase_unit": "Purchase unit (what you buy)",
                  "stock_units_per_purchase_unit": "Stock units in one purchase unit",
                  "recipe_unit": "Recipe unit (what recipes use)", "recipe_units_per_stock_unit": "Recipe units in one stock unit",
                  "reorder_level": "Reorder level", "par_level": "Par level (stock to refill up to)", "is_active": "Active"}

    def clean(self):
        d = super().clean()
        for f in ("stock_units_per_purchase_unit", "recipe_units_per_stock_unit"):
            if d.get(f) is not None and d[f] <= 0:
                self.add_error(f, "Must be more than zero.")
        for f in ("reorder_level", "par_level"):
            if d.get(f) is not None and d[f] < 0:
                self.add_error(f, "Cannot be negative.")
        return d


@manager_required
def items(request):
    g = request.GET
    qs = _items_with_stock()
    if g.get("show") != "all":
        qs = qs.filter(is_active=True)
    if g.get("q"):
        qs = qs.filter(Q(name__icontains=g["q"].strip()) | Q(sku__icontains=g["q"].strip()))
    if g.get("category"):
        qs = qs.filter(category=g["category"])
    if g.get("low"):
        qs = qs.filter(reorder_level__gt=0, on_hand_total__lte=F("reorder_level"))
    page = _paginate(request, qs.order_by("name"))
    for i in page:
        i.value = i.on_hand_total * i.avg_cost
        i.low = i.reorder_level > 0 and i.on_hand_total <= i.reorder_level
    cats = StockItem.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
    return _page(request, "items.html", {"page": page, "categories": cats, "f": g, "qs": _qs_without_page(request)})


def _item_form_page(request, form, item=None, status=200):
    cats = StockItem.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
    return _page(request, "item_form.html", {"form": form, "item": item, "categories": cats}, status=status)


@manager_required
def item_new(request):
    form = StockItemForm(request.POST or None, initial={"is_active": True})
    if request.method == "POST" and form.is_valid():
        item = form.save()
        audit(request.user, "item_created", item, sku=item.sku)
        messages.success(request, f"{item.name} added. Bring in its opening stock with a stock take, a purchase or a receipt.")
        return redirect("manager:item_detail", pk=item.pk)
    return _item_form_page(request, form, status=422 if request.method == "POST" else 200)


@manager_required
def item_edit(request, pk):
    item = get_object_or_404(StockItem, pk=pk)
    form = StockItemForm(request.POST or None, instance=item)
    if request.method == "POST" and form.is_valid():
        changed = form.changed_data
        form.save()
        audit(request.user, "item_edited", item, fields=changed)
        messages.success(request, f"{item.name} updated.")
        return redirect("manager:item_detail", pk=item.pk)
    return _item_form_page(request, form, item, status=422 if request.method == "POST" else 200)


@manager_required
@require_POST
def item_toggle(request, pk):
    item = get_object_or_404(StockItem, pk=pk)
    item.is_active = not item.is_active
    item.save(update_fields=["is_active", "updated_at"])
    audit(request.user, "item_activated" if item.is_active else "item_deactivated", item)
    messages.success(request, f"{item.name} is now {'active' if item.is_active else 'inactive'}.")
    return _back(request, reverse("manager:items"))


@manager_required
def item_detail(request, pk):
    item = get_object_or_404(StockItem, pk=pk)
    bal = dict(StockBalance.objects.filter(item=item).values_list("location_id", "quantity"))
    per_loc = [{"loc": l, "qty": bal.get(l.pk, D(0))} for l in Location.objects.order_by("name")]
    total = sum((x["qty"] for x in per_loc), D(0))
    moves = StockMovement.objects.filter(item=item).select_related("location", "user").order_by("-id")[:100]
    recipes = item.recipe_lines.select_related("menu_item")
    return _page(request, "item_detail.html", {"item": item, "per_loc": per_loc, "total": total, "value": total * item.avg_cost,
                                                "moves": moves, "recipes": recipes,
                                                "low": item.reorder_level > 0 and total <= item.reorder_level})


# =============================================================================
# Stock balances
# =============================================================================

@manager_required
def balances(request):
    g = request.GET
    locations = list(Location.objects.order_by("name"))
    shown = [l for l in locations if str(l.pk) == g.get("location")] or locations
    qs = StockItem.objects.filter(is_active=True)
    if g.get("q"):
        qs = qs.filter(Q(name__icontains=g["q"].strip()) | Q(sku__icontains=g["q"].strip()))
    if g.get("category"):
        qs = qs.filter(category=g["category"])
    qty = {}
    for item_id, loc_id, n in StockBalance.objects.filter(location__in=shown).values_list("item_id", "location_id", "quantity"):
        qty.setdefault(item_id, {})[loc_id] = n
    rows, grand = [], D(0)
    for i in qs.order_by("category", "name"):
        cells = [qty.get(i.pk, {}).get(l.pk, D(0)) for l in shown]
        total = sum(cells, D(0))
        low = i.reorder_level > 0 and total <= i.reorder_level
        if g.get("low") and not low:
            continue
        if g.get("hide_zero") and total == 0:
            continue
        value = total * i.avg_cost
        grand += value
        rows.append({"item": i, "cells": cells, "total": total, "value": value, "low": low, "out": total <= 0})
    if g.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="stock-balances-{timezone.localdate()}.csv"'
        w = csv.writer(resp)
        w.writerow(["SKU", "Item", "Category", "Unit"] + [l.name for l in shown] + ["Total", "Avg cost", "Value", "Reorder level"])
        for r in rows:
            i = r["item"]
            w.writerow([i.sku, i.name, i.category, i.stock_unit] + [_fmt(c) for c in r["cells"]]
                       + [_fmt(r["total"]), f"{i.avg_cost:.2f}", f"{r['value']:.2f}", _fmt(i.reorder_level)])
        return resp
    cats = StockItem.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
    return _page(request, "balances.html", {"rows": rows, "shown": shown, "locations": locations, "categories": cats,
                                             "grand": grand, "f": g, "qs": _qs_without_page(request)})


# =============================================================================
# Stock takes
# =============================================================================

@manager_required
def stock_takes(request):
    locations = Location.objects.order_by("name")
    if request.method == "POST":
        p = request.POST
        def run():
            items_qs = StockItem.objects.filter(is_active=True)
            if p.get("category"):
                items_qs = items_qs.filter(category=p["category"])
            if not items_qs.exists():
                raise InvalidState("There are no active items to count")
            return start_stock_take(location=_location(p.get("location")), user=request.user, items=items_qs,
                                    is_opening=p.get("kind") == "opening", blind=bool(p.get("blind")))
        take, ok = _try(request, run)
        if ok:
            messages.success(request, f"{take.number} started. Enter the counts, then approve to apply the variances.")
            return redirect("manager:stock_take", pk=take.pk)
    page = _paginate(request, StockTake.objects.select_related("location", "created_by", "approved_by").order_by("-created_at"), 25)
    cats = StockItem.objects.exclude(category="").values_list("category", flat=True).distinct().order_by("category")
    return _page(request, "stock_takes.html", {"page": page, "locations": locations, "categories": cats,
                                                "qs": _qs_without_page(request)})


@manager_required
def stock_take_detail(request, pk):
    take = get_object_or_404(StockTake.objects.select_related("location", "created_by", "approved_by"), pk=pk)
    if request.method == "POST":
        p = request.POST
        if take.status != StockTake.Status.DRAFT:
            messages.error(request, "This stock take is already approved.")
            return redirect("manager:stock_take", pk=pk)

        def save_counts():
            for line in take.lines.select_related("item"):
                raw, cost = p.get(f"count_{line.pk}", "").strip(), p.get(f"cost_{line.pk}", "").strip()
                if raw == "":
                    if line.counted is not None:
                        line.counted = None
                        line.save(update_fields=["counted"])
                    continue
                n = _num(raw, f"{line.item.name} count")
                if n < 0:
                    raise InvalidState(f"{line.item.name}: a count cannot be negative")
                c = _num(cost, f"{line.item.name} cost") if cost else None
                if c is not None and c < 0:
                    raise InvalidState(f"{line.item.name}: cost cannot be negative")
                record_count(take=take, item=line.item, counted=n, unit_cost=c)

        if p.get("action") == "approve":
            def run():
                save_counts()
                return approve_stock_take(take=take, approver=request.user)
            _, ok = _try(request, run)
            messages.success(request, f"{take.number} approved. Variances are now in the stock ledger.") if ok else None
        else:
            _, ok = _try(request, save_counts)
            messages.success(request, "Counts saved.") if ok else None
        return redirect("manager:stock_take", pk=pk)

    lines = list(take.lines.select_related("item").order_by("item__category", "item__name"))
    for l in lines:
        # Cost per stock unit and value of what was counted (same costing as net_value below).
        l.cost = l.unit_cost if take.is_opening and l.unit_cost is not None else l.item.avg_cost
        l.value = None if l.counted is None else l.counted * l.cost
    counted = [l for l in lines if l.counted is not None]
    varied = [l for l in counted if l.variance != 0]
    show_expected = not (take.blind and take.status == StockTake.Status.DRAFT)
    shrink = sum(((l.variance * (l.unit_cost if take.is_opening and l.unit_cost is not None else l.item.avg_cost))
                  for l in varied), D(0))
    return _page(request, "stock_take.html", {
        "take": take, "lines": lines, "counted": len(counted), "uncounted": len(lines) - len(counted),
        "varied": varied, "show_expected": show_expected, "net_value": shrink, "draft": take.status == StockTake.Status.DRAFT})


# =============================================================================
# Issue stock (store -> kitchen / bar, or write off)
# =============================================================================

@manager_required
def issue(request):
    locations = list(Location.objects.order_by("name"))
    store = _default_location(locations)
    ctx = {"locations": locations, "items": _item_options(), "balances": _balance_map(), "rows": [],
           "sel_from": store, "mode": "issue"}
    status = 200
    if request.method == "POST":
        p = request.POST
        mode = p.get("mode", "issue")

        def run():
            src = _location(p.get("from_location"), "location to issue from")
            lines = [(i, n) for i, n, _ in _lines(p, cost=False)]
            if mode == "writeoff":
                return write_off_stock(location=src, lines=lines, reason=p.get("reason", "").strip(), user=request.user), src, None
            dest = _location(p.get("to_location"), "location to issue to")
            return issue_stock(from_location=src, to_location=dest, lines=lines, user=request.user,
                               note=p.get("reason", "").strip()), src, dest
        res, ok = _try(request, run)
        if ok:
            ref, src, dest = res
            messages.success(request, f"{ref}: " + (f"issued from {src.name} to {dest.name}." if dest else f"written off from {src.name}."))
            return redirect("manager:issue")
        status = 422
        ctx.update(rows=_rows(p, "item", "qty"), post=p, mode=mode,
                   sel_from=Location.objects.filter(pk=p.get("from_location") or 0).first() or store)
    moves = list(StockMovement.objects.filter(ref_type__in=["issue", "writeoff"], quantity__lt=0)
                 .select_related("item", "location", "user").order_by("-id")[:300])
    refs = list(dict.fromkeys(m.ref_id for m in moves))[:20]
    dest = {m.ref_id: m.location for m in StockMovement.objects.filter(ref_type="issue", ref_id__in=refs, quantity__gt=0).select_related("location")}
    history = {}
    for m in moves:
        if m.ref_id not in refs:
            continue
        h = history.setdefault(m.ref_id, {"ref": m.ref_id, "at": m.created_at, "user": m.user, "src": m.location, "reason": m.reason,
                                          "dest": dest.get(m.ref_id), "lines": [], "value": D(0)})
        h["lines"].append(m)
        h["value"] += -m.quantity * m.unit_cost
    ctx["history"] = list(history.values())
    return _page(request, "issue.html", ctx, status=status)


# =============================================================================
# Recipes (what ONE portion of a menu item takes out of stock)
# =============================================================================

QTY_PLACES = D("0.0001")      # RecipeLine.quantity has 4 decimal places
RECIPE_NO_FIGURES = {"cost": None, "net": None, "profit": None, "margin": None, "food_cost": None}


def _margin_target():
    """Dishes whose margin falls under this % are flagged. Override with RECIPE_MARGIN_TARGET in settings."""
    return D(str(getattr(settings, "RECIPE_MARGIN_TARGET", 60)))


def _net_price(dish):
    """Selling price without tax: what the business really earns on one portion."""
    price = D(dish.price)
    return price / (1 + D(dish.tax_rate) / 100) if settings.PRICES_INCLUDE_TAX else price


def _recipe_figures(dish, lines):
    """Cost per portion, net price, profit, margin % and food-cost % from the saved lines (avg cost per stock unit)."""
    cost = sum((l.stock_quantity * l.stock_item.avg_cost for l in lines), D(0))
    net = _net_price(dish)
    profit = net - cost
    one = D("0.1")
    return {"cost": q(cost), "net": q(net), "profit": q(profit),
            "margin": (profit / net * 100).quantize(one) if net > 0 else None,
            "food_cost": (cost / net * 100).quantize(one) if net > 0 else None}


@manager_required
def recipes(request):
    g = request.GET
    qs = MenuItem.objects.select_related("category").prefetch_related("recipe_lines__stock_item")
    if g.get("q"):
        s = g["q"].strip()
        qs = qs.filter(Q(name__icontains=s) | Q(category__name__icontains=s))
    if g.get("category", "").isdigit():
        qs = qs.filter(category_id=int(g["category"]))
    if g.get("station") in Station.values:
        qs = qs.filter(station=g["station"])
    if g.get("missing"):
        qs = qs.filter(Q(recipe_lines__isnull=True) & Q(track_stock=True))
    target = _margin_target()
    rows = []
    for d in qs:
        lines = list(d.recipe_lines.all())
        fig = _recipe_figures(d, lines) if lines else dict(RECIPE_NO_FIGURES)
        rows.append({"dish": d, "n": len(lines), **fig,
                     "tracked_empty": d.track_stock and not lines,
                     "untracked_recipe": bool(lines) and not d.track_stock,
                     "low": fig["margin"] is not None and fig["margin"] < target})
    sort = g.get("sort", "")
    if sort == "margin":        # worst margin first, dishes without a recipe last
        rows.sort(key=lambda r: (r["margin"] is None, r["margin"] if r["margin"] is not None else D(0)))
    elif sort == "cost":        # most expensive portion first
        rows.sort(key=lambda r: (r["cost"] is None, -(r["cost"] or D(0))))
    stats = {"total": MenuItem.objects.count(),
             "with": MenuItem.objects.filter(recipe_lines__isnull=False).distinct().count(),
             "empty": MenuItem.objects.filter(track_stock=True, recipe_lines__isnull=True).count(),
             "low": sum(1 for r in rows if r["low"])}
    return _page(request, "recipes.html", {"page": _paginate(request, rows, 100), "stats": stats, "target": target,
                                            "categories": Category.objects.all(), "stations": Station.choices,
                                            "includes_tax": settings.PRICES_INCLUDE_TAX,
                                            "f": g, "qs": _qs_without_page(request)})


def _parse_recipe(post, keep=()):
    """ingredient/qty columns -> [(StockItem, qty in recipe units)]. No lines at all is allowed (clears the recipe)."""
    rows = _rows(post, "item", "qty")
    try:
        found = StockItem.objects.in_bulk([int(r["item"]) for r in rows])
    except (TypeError, ValueError):
        raise InvalidState("Pick an ingredient on every line")
    out, seen = [], set()
    for n, r in enumerate(rows, 1):
        item = found.get(int(r["item"]))
        if item is None:
            raise InvalidState(f"Line {n}: unknown ingredient")
        if not item.is_active and item.pk not in keep:
            raise InvalidState(f"Line {n}: {item.name} is inactive")
        if item.pk in seen:
            raise InvalidState(f"{item.name} is on two lines; combine them into one")
        seen.add(item.pk)
        raw = _num(r["qty"], f"Line {n} quantity")
        if not raw.is_finite() or raw > D("1000000"):
            raise InvalidState(f"Line {n}: that quantity is not valid")
        qty = raw.quantize(QTY_PLACES)
        if qty <= 0:
            raise InvalidState(f"Line {n}: quantity must be more than zero")
        out.append((item, qty))
    return out


@manager_required
def recipe_edit(request, pk):
    dish = get_object_or_404(MenuItem.objects.select_related("category"), pk=pk)
    current = list(dish.recipe_lines.select_related("stock_item").order_by("id"))
    keep = {l.stock_item_id for l in current}
    g, status, copied = request.GET, 200, None
    track = dish.track_stock
    rows = [{"item": str(l.stock_item_id), "qty": _fmt(l.quantity)} for l in current]

    if request.method == "POST":
        p = request.POST

        def run():
            lines = _parse_recipe(p, keep)
            before = [f"{l.stock_item.sku} {_fmt(l.quantity)}" for l in current]
            dish.recipe_lines.all().delete()
            RecipeLine.objects.bulk_create([RecipeLine(menu_item=dish, stock_item=i, quantity=n) for i, n in lines])
            dish.track_stock = p.get("track_stock") == "on"
            dish.save(update_fields=["track_stock", "updated_at"])
            audit(request.user, "recipe_edited", dish, before=before, after=[f"{i.sku} {_fmt(n)}" for i, n in lines])
            return lines
        lines, ok = _try(request, run)
        if ok:
            saved = list(dish.recipe_lines.select_related("stock_item"))
            if not saved:
                messages.warning(request, f"{dish.name}: recipe cleared. Selling it now deducts nothing from stock.")
            else:
                f = _recipe_figures(dish, saved)
                tail = "" if dish.track_stock else " Stock tracking is OFF, so sales will not deduct these yet."
                messages.success(request, f"{dish.name} saved: cost {f['cost']} per portion"
                                          + (f", margin {f['margin']}%." if f["margin"] is not None else ".") + tail)
            return redirect("manager:recipes")
        status = 422
        rows, track = _rows(p, "item", "qty"), p.get("track_stock") == "on"
    elif g.get("copy", "").isdigit():
        src = MenuItem.objects.filter(pk=int(g["copy"])).exclude(pk=dish.pk).first()
        if src:
            copy_lines = list(src.recipe_lines.select_related("stock_item").order_by("id"))
            rows = [{"item": str(l.stock_item_id), "qty": _fmt(l.quantity)} for l in copy_lines]
            copied = src

    shown = set(keep)
    shown.update(int(r["item"]) for r in rows if str(r.get("item", "")).isdigit())
    stock = StockItem.objects.filter(Q(is_active=True) | Q(pk__in=shown)).order_by("name")
    item_data = [{"id": i.pk, "label": f"{i.name} ({i.sku})", "runit": i.recipe_unit, "sunit": i.stock_unit,
                  "factor": _fmt(i.recipe_units_per_stock_unit), "cost": f"{i.avg_cost:f}"} for i in stock]
    others = (MenuItem.objects.exclude(pk=dish.pk).filter(recipe_lines__isnull=False).distinct()
              .select_related("category").order_by("category", "name"))
    return _page(request, "recipe_edit.html", {"dish": dish, "rows": rows, "item_data": item_data, "track": track,
                                                "net": f"{_net_price(dish):f}", "target": f"{_margin_target():f}",
                                                "includes_tax": settings.PRICES_INCLUDE_TAX, "others": others,
                                                "copied": copied}, status=status)


# =============================================================================
# Voids
# =============================================================================

@manager_required
def voids(request):
    g = request.GET
    live = (Order.objects.filter(status__in=Order.ACTIVE, items__status=OrderItem.Status.SENT).distinct()
            .select_related("table", "waiter").prefetch_related("items").order_by("-created_at"))
    if g.get("q"):
        s = g["q"].strip()
        live = live.filter(Q(number__icontains=s) | Q(table__name__icontains=s) | Q(waiter__username__icontains=s)
                           | Q(items__name__icontains=s)).distinct()
    orders = []
    for o in live[:60]:
        o.sent_items = [i for i in o.items.all() if i.status == OrderItem.Status.SENT]
        orders.append(o)
    billed = (Bill.objects.filter(status=Bill.Status.OPEN).select_related("order__table", "order__waiter")
              .order_by("-created_at")[:40])
    logs = list(AuditLog.objects.filter(action__in=["item_voided", "bill_cancelled"]).select_related("user")[:100])
    order_no = dict(Order.objects.filter(pk__in=[l.ref_id for l in logs if l.ref_type == "Order"]).values_list("pk", "number"))
    bill_no = dict(Bill.objects.filter(pk__in=[l.ref_id for l in logs if l.ref_type == "Bill"]).values_list("pk", "number"))
    for l in logs:
        l.what = order_no.get(int(l.ref_id)) if l.ref_type == "Order" else bill_no.get(int(l.ref_id))
    return _page(request, "voids.html", {"orders": orders, "billed": billed, "logs": logs, "f": g})


@manager_required
@require_POST
def void_item_action(request, item_id):
    item = get_object_or_404(OrderItem.objects.select_related("order"), pk=item_id)
    p = request.POST
    _, ok = _try(request, lambda: void_item(item=item, user=request.user, reason=p.get("reason", "").strip(),
                                            wasted=p.get("wasted") == "1"))
    if ok:
        messages.success(request, f"Voided {item.quantity} x {item.name} on {item.order.number}"
                                  + (" (counted as waste)." if p.get("wasted") == "1" else " (stock returned)."))
    return _back(request, reverse("manager:voids"))


@manager_required
@require_POST
def cancel_bill_action(request, pk):
    bill = get_object_or_404(Bill, pk=pk)
    _, ok = _try(request, lambda: cancel_bill(bill=bill, user=request.user, reason=request.POST.get("reason", "").strip()))
    if ok:
        messages.success(request, f"Bill {bill.number} cancelled. Order {bill.order.number} is open again, so its items can be voided.")
    return _back(request, reverse("manager:voids"))


# =============================================================================
# Discounts
# =============================================================================

@manager_required
def discounts(request):
    g = request.GET
    bills = Bill.objects.filter(status=Bill.Status.OPEN).select_related("order__table", "order__waiter").order_by("-created_at")
    if g.get("q"):
        s = g["q"].strip()
        bills = bills.filter(Q(number__icontains=s) | Q(order__number__icontains=s) | Q(order__table__name__icontains=s))
    bills = list(bills[:60])
    logs = list(AuditLog.objects.filter(action__in=["discount_given", "discount_applied", "discount_removed"])
                .select_related("user")[:100])
    bill_no = dict(Bill.objects.filter(pk__in=[l.ref_id for l in logs]).values_list("pk", "number"))
    for l in logs:
        l.what = bill_no.get(int(l.ref_id))
    return _page(request, "discounts.html", {"bills": bills, "logs": logs, "f": g})


@manager_required
@require_POST
def discount_action(request, pk, action):
    bill = get_object_or_404(Bill, pk=pk)
    p = request.POST
    reason = p.get("reason", "").strip()
    if action == "apply":
        def run():
            pct = _num(p["value"], "percentage") if p.get("kind") == "percent" and p.get("value") else None
            amt = _num(p["value"], "amount") if p.get("kind") == "amount" and p.get("value") else None
            try:
                idx = sorted({int(i) for i in p.getlist("line")}) if p.get("scope") == "lines" else None
            except ValueError:
                raise InvalidState("Pick valid items")
            if p.get("scope") == "lines" and not idx:
                raise InvalidState("Tick at least one item, or discount the whole bill")
            return apply_bill_discount(bill=bill, user=request.user, reason=reason, percent=pct, amount=amt, line_indexes=idx)
        done = f"Discount applied to {bill.number}."
    elif action == "remove":
        run, done = (lambda: remove_bill_discount(bill=bill, user=request.user, reason=reason)), f"Discount removed from {bill.number}."
    else:
        raise PermissionDenied("Unknown action")
    _, ok = _try(request, run)
    if ok:
        messages.success(request, done)
    return _back(request, reverse("manager:discounts"))