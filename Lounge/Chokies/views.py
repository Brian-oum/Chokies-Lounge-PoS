"""Chokies POS - services, PIN authentication, serializers and API views in one module.

Flow
----
 manager/admin ──create_shift──▶ Shift + ShiftAssignments (waiters, cashiers + drawer float)
 staff         ──authenticate_pin▶ bearer token (waiters/cashiers only while on an open shift)
 waiter        ──open_order───▶ order from the MENU or the BAR, table optional, own order number
 waiter        ──add_item / send_order (KOTs, stock deduction) / void
 waiter        ──generate_bill / take_payment on their own orders (one bill per order, into their drawer)
 cashier       ──can also bill/collect any order; keeps the float and the cash in/out
 everyone      ──end_assignment (count the drawer you collected into)   manager ──close_shift / close_day

Sections: helpers - auth - shifts - catalog - orders - billing - end of day -
          serializers - API views - inventory & purchasing services
"""
import base64
import hmac
import json
import logging
import math
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone as dt_tz
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth import login as django_login, logout as django_logout
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Count, Q, Sum
from html import escape as esc
from urllib.parse import urlencode, urlsplit

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template import TemplateDoesNotExist
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from rest_framework import serializers
from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_handler

from .models import (KOT, AuditLog, AuthFailed, Bill, CashMovement, DocumentSequence, DomainError,
                     GoodsReceipt, GoodsReceiptLine, InsufficientStock, InvalidState, ItemUnavailable, Location,
                     ManagerApprovalRequired, MenuItem, Modifier, MovementType, NotOnShift, Order, OrderConflict,
                     OrderItem, Payment, PinLocked, PurchaseOrder, PurchaseOrderLine, Shift, ShiftAssignment,
                     Station, StockBalance, StockItem, StockMovement, StockTake, StockTakeLine, Table, User,
                     ZReport, Category, MealPlan, RoomUnavailable, Room, RoomBooking, RoomBookingLine, audit)

D = Decimal
TWO = D("0.01")
Role = ShiftAssignment.Role

POS_LOGIN_TEMPLATE = getattr(settings, "POS_LOGIN_TEMPLATE", "Chokies/login.html")
log = logging.getLogger(__name__)
POS_HOME_URL = getattr(settings, "POS_HOME_URL", "/")
# =============================================================================
# Helpers
# =============================================================================

def q(x):
    return D(x).quantize(TWO, ROUND_HALF_UP)


def _num(value, field="value"):
    """Parse user input to Decimal, turning garbage into a clean 409 instead of a 500."""
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise InvalidState(f"{field} must be a number")


def _money(value, field="amount"):
    return q(_num(value, field))


def _approver(user, manager_pin, what):
    approver = user if user.is_manager else find_manager_by_pin(manager_pin)
    if approver is None:
        raise ManagerApprovalRequired(f"{what} needs manager approval")
    return approver


def find_manager_by_pin(raw_pin):
    """Manager override: returns the manager whose PIN matches, or None."""
    if not raw_pin:
        return None
    for u in User.objects.filter(is_active=True, role__in=[User.Role.MANAGER, User.Role.ADMIN]):
        if u.check_pin(str(raw_pin)):
            return u
    return None


# =============================================================================
# Authentication: username + PIN -> signed bearer token
# =============================================================================

TOKEN_SALT = "chokies.pos.token"
MAX_PIN_ATTEMPTS = getattr(settings, "POS_MAX_PIN_ATTEMPTS", 5)
PIN_LOCK_MINUTES = getattr(settings, "POS_PIN_LOCK_MINUTES", 5)
TOKEN_MAX_AGE = getattr(settings, "POS_TOKEN_MAX_AGE", 16 * 3600)


def issue_token(user):
    return signing.dumps({"u": user.pk, "v": user.token_version}, salt=TOKEN_SALT)


def active_assignment(user, role=None):
    """The user's place on an open shift (None if they are not working right now)."""
    qs = ShiftAssignment.objects.select_related("shift").filter(
        user=user, ended_at__isnull=True, shift__status=Shift.Status.OPEN)
    if role is not None:
        qs = qs.filter(role=role)
    return qs.first()


def _require_assignment(user, role):
    a = active_assignment(user, role)
    if a is None:
        raise NotOnShift(f"You are not on an open shift as a {role.label.lower()}. Ask a manager to add you.")
    return a


def _require_drawer(user):
    """Billing and payments need a collection drawer: any open-shift assignment (waiter or cashier).
    Waiters collect into their own drawer; cashiers into theirs."""
    a = active_assignment(user)
    if a is None:
        raise NotOnShift("You are not on an open shift. Ask a manager to add you.")
    return a


def _require_cashier(user, what="Printing receipts"):
    """Receipts are the cashier's job: a cashier on an open shift, or a manager. Waiters collect orders, not print."""
    if user.is_manager or active_assignment(user, Role.CASHIER):
        return
    raise ManagerApprovalRequired(f"{what} is done by the cashier. Please send the guest to the cashier.")


def _check_bill_access(user, drawer, doc):
    """Waiters bill and collect only on their own orders / bookings; cashiers and managers on any.
    `doc` is the Order or RoomBooking the bill is for."""
    owner = doc.waiter_id if isinstance(doc, Order) else doc.booked_by_id
    if drawer.role == Role.WAITER and not user.is_manager and owner != user.pk:
        kind = "Order" if isinstance(doc, Order) else "Booking"
        raise ManagerApprovalRequired(f"{kind} {doc.number} belongs to another waiter")


def user_payload(user, assignment=None):
    assignment = assignment or active_assignment(user)
    return {"id": user.pk, "username": user.username, "name": user.get_full_name() or user.username,
            "role": user.role, "is_manager": user.is_manager, "prices_include_tax": settings.PRICES_INCLUDE_TAX,
            "room_vat_percent": float(_room_vat()), "room_levy_percent": float(_room_levy()),
            "shift": None if assignment is None else {
                "id": assignment.shift_id, "name": assignment.shift.name, "role": assignment.role,
                "terminal": assignment.terminal}}


def authenticate_pin(*, username, pin):
    """Check username + PIN. Waiters/cashiers must be on an open shift. Locks after repeated failures."""
    outcome, user = "bad", None
    with transaction.atomic():  # the failure counter must be saved even though we raise afterwards
        user = User.objects.select_for_update().filter(username=username, is_active=True).first()
        if user is None:
            outcome = "bad"
        elif user.pin_is_locked:
            outcome = "locked"
        elif user.check_pin(str(pin)):
            user.failed_pin_attempts, user.pin_locked_until = 0, None
            user.save(update_fields=["failed_pin_attempts", "pin_locked_until"])
            outcome = "ok"
        else:
            user.failed_pin_attempts += 1
            if user.failed_pin_attempts >= MAX_PIN_ATTEMPTS:
                user.failed_pin_attempts = 0
                user.pin_locked_until = timezone.now() + timedelta(minutes=PIN_LOCK_MINUTES)
                audit(user, "pin_locked", user, minutes=PIN_LOCK_MINUTES)
            user.save(update_fields=["failed_pin_attempts", "pin_locked_until"])
            outcome = "bad"
    if outcome == "locked":
        raise PinLocked(f"Too many wrong PINs. Try again in {PIN_LOCK_MINUTES} minutes or ask a manager to unlock.")
    if outcome != "ok":
        raise AuthFailed("Wrong username or PIN")
    assignment = active_assignment(user)
    if user.needs_shift and assignment is None:
        raise NotOnShift("You are not on an open shift. Ask a manager to add you to one.")
    return {"token": issue_token(user), "user": user_payload(user, assignment)}


# --- PIN-only sign-in (the login page): the PIN alone says who you are ---------------------------------------
# With no username, wrong guesses cannot be counted against a person, so they are counted against the device /
# network address instead. Successful sign-ins do not reset the count (otherwise a valid PIN could be used to keep
# guessing). Needs a cache shared by all workers to be exact: the default per-process cache is fine for one worker.
PIN_LOGIN_MAX_FAILS = getattr(settings, "POS_PIN_LOGIN_MAX_FAILS", 10)           # wrong PINs allowed per window ...
PIN_LOGIN_WINDOW_MIN = getattr(settings, "POS_PIN_LOGIN_WINDOW_MINUTES", 10)     # ... of this many minutes
PIN_LOGIN_MAX_LEN = 12
POS_PIN_LENGTH = getattr(settings, "POS_PIN_LENGTH", 0)     # e.g. 4: the login page signs in as soon as 4 digits are tapped; 0 = needs the Login button


def client_id(request):
    """Who is knocking: REMOTE_ADDR, or the first address of settings.POS_CLIENT_IP_HEADER (e.g. 'HTTP_X_FORWARDED_FOR')
    when the app sits behind a proxy you control."""
    header = getattr(settings, "POS_CLIENT_IP_HEADER", "")
    raw = request.META.get(header, "") if header else ""
    return (raw.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")) or "unknown"


def _find_by_pin(pin):
    """The active user who owns this PIN, or None. Uses the indexed fingerprint; people whose PIN predates it are found
    by checking their hash once and then given a fingerprint, so this slow path disappears as staff sign in."""
    fp = User.pin_fingerprint(pin)
    user = User.objects.filter(pin_lookup=fp, is_active=True).first()
    if user is not None:
        return user
    legacy_users = list(User.objects.filter(is_active=True, pin_lookup__isnull=True).exclude(pin_hash=""))
    if legacy_users:       # every one of these costs ~0.5 s of hashing per attempt: this is what makes sign-in slow
        log.warning("%d active user(s) still have an old-style PIN, so this sign-in had to check each of them (slow). "
                    "Run 'python manage.py reset_pins --apply' once to make sign-in instant.", len(legacy_users))
    legacy = [u for u in legacy_users if u.check_pin(pin)]
    if len(legacy) > 1:
        raise AuthFailed("More than one person has this PIN. Ask a manager to give you a new one.")
    if legacy:
        try:
            with transaction.atomic():
                User.objects.filter(pk=legacy[0].pk, pin_lookup__isnull=True).update(pin_lookup=fp)
        except Exception:      # e.g. an inactive user already holds this fingerprint: sign in, fingerprint later
            pass
        return legacy[0]
    return None


def authenticate_by_pin(*, pin, request):
    """Sign in with the PIN alone. Same rules as authenticate_pin afterwards: waiters / cashiers must be on an open
    shift; a manager-locked account stays locked."""
    cid = client_id(request)
    fails_key, lock_key = f"poslogin:fails:{cid}", f"poslogin:lock:{cid}"
    if cache.get(lock_key):
        raise PinLocked(f"Too many wrong PINs from this device. Try again in {PIN_LOCK_MINUTES} minutes.")
    pin = str(pin or "").strip()
    user, refusal = None, AuthFailed("Wrong PIN. Try again.")
    if pin.isdigit() and len(pin) <= PIN_LOGIN_MAX_LEN:
        try:
            user = _find_by_pin(pin)
        except AuthFailed as e:          # a shared PIN is refused, and counted like any other failed guess
            refusal = e
    if user is None:
        cache.add(fails_key, 0, PIN_LOGIN_WINDOW_MIN * 60)
        try:
            fails = cache.incr(fails_key)
        except ValueError:      # the entry expired between add and incr
            cache.set(fails_key, 1, PIN_LOGIN_WINDOW_MIN * 60); fails = 1
        if fails >= PIN_LOGIN_MAX_FAILS:
            cache.set(lock_key, 1, PIN_LOCK_MINUTES * 60); cache.delete(fails_key)
            audit(None, "pin_login_locked", None, client=cid, minutes=PIN_LOCK_MINUTES)
        raise refusal
    if user.pin_is_locked:
        raise PinLocked("This account is locked. Ask a manager to unlock it.")
    assignment = active_assignment(user)
    if user.needs_shift and assignment is None:
        raise NotOnShift("You are not on an open shift. Ask a manager to add you to one.")
    if user.failed_pin_attempts or user.pin_locked_until:      # only write when there is something to clear
        user.failed_pin_attempts, user.pin_locked_until = 0, None
        user.save(update_fields=["failed_pin_attempts", "pin_locked_until"])
    return {"token": issue_token(user), "user": user_payload(user, assignment), "_user": user}   # "_user": for pin_login, not sent out


class PinTokenAuthentication(BaseAuthentication):
    """`Authorization: Bearer <token>`. Waiters/cashiers are re-checked against their shift on
    every request, so closing a shift (or ending someone's assignment) logs them out at once."""

    def authenticate(self, request):
        parts = request.headers.get("Authorization", "").split()
        if not parts or parts[0].lower() != "bearer":
            return None
        if len(parts) != 2:
            raise AuthenticationFailed("Malformed Authorization header")
        try:
            data = signing.loads(parts[1], salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
        except signing.BadSignature:
            raise AuthenticationFailed("Session expired or invalid. Log in again.")
        user = User.objects.filter(pk=data.get("u"), is_active=True).first()
        if user is None or user.token_version != data.get("v"):
            raise AuthenticationFailed("Session is no longer valid. Log in again.")
        if user.needs_shift and active_assignment(user) is None:
            raise AuthenticationFailed("Your shift has ended.")
        return user, parts[1]

    def authenticate_header(self, request):
        return "Bearer"


class ShiftSessionAuthentication(SessionAuthentication):
    """Browser sessions (set by the pin_login page view) get the same shift check as tokens."""

    def authenticate(self, request):
        result = super().authenticate(request)
        if result and result[0].needs_shift and active_assignment(result[0]) is None:
            raise AuthenticationFailed("Your shift has ended.")
        return result


# =============================================================================
# Shifts
# =============================================================================

def _validated(assignment):
    try:
        assignment.full_clean()
    except ValidationError as e:
        raise InvalidState("; ".join(e.messages))
    return assignment


def _require_manager(user, what="This"):
    if not user.is_manager:
        raise ManagerApprovalRequired(f"{what} needs a manager")


@transaction.atomic
def create_shift(*, manager, staff, name="", notes=""):
    """staff: iterable of dicts {user, role, terminal (cashiers), opening_float (cashiers)}."""
    _require_manager(manager, "Creating a shift")
    staff = list(staff)
    if not staff:
        raise InvalidState("A shift needs at least one waiter or cashier")
    shift = Shift.objects.create(name=name, notes=notes, created_by=manager)
    seen_terminals = set()
    for row in staff:
        a = ShiftAssignment(shift=shift, user=row["user"], role=row["role"],
                            terminal=row.get("terminal", ""), opening_float=q(row.get("opening_float", 0)))
        _validated(a).save()  # saved one by one so the next row sees this one (terminal clashes)
        if a.role == Role.CASHIER:
            if a.terminal in seen_terminals:
                raise InvalidState(f"Terminal {a.terminal} is used by two cashiers")
            seen_terminals.add(a.terminal)
    audit(manager, "shift_created", shift,
          staff=[f"{a.user.username}:{a.role}" for a in shift.staff.select_related("user")])
    return shift


@transaction.atomic
def add_staff(*, shift, manager, user, role, terminal="", opening_float=0):
    _require_manager(manager, "Adding staff to a shift")
    shift = Shift.objects.select_for_update().get(pk=shift.pk)
    if shift.status != Shift.Status.OPEN:
        raise InvalidState("Shift is closed")
    a = _validated(ShiftAssignment(shift=shift, user=user, role=role, terminal=terminal,
                                   opening_float=q(opening_float)))
    a.save()
    audit(manager, "shift_staff_added", shift, user=user.username, role=role)
    return a


def shift_summary(drawer):
    """Cash position of one cashier's drawer."""
    pay = drawer.payments.all()
    cash_in = pay.filter(method=Payment.Method.CASH, kind=Payment.Kind.PAYMENT).aggregate(t=Sum("amount"))["t"] or D(0)
    cash_out = -(pay.filter(method=Payment.Method.CASH, kind=Payment.Kind.REFUND).aggregate(t=Sum("amount"))["t"] or D(0))
    cm = drawer.cash_movements
    paid_in = cm.filter(kind="PAID_IN").aggregate(t=Sum("amount"))["t"] or D(0)
    payouts = cm.filter(kind="PAYOUT").aggregate(t=Sum("amount"))["t"] or D(0)
    by_method = {r["method"]: r["t"] for r in pay.values("method").annotate(t=Sum("amount"))}
    return {"opening_float": drawer.opening_float, "cash_sales": cash_in, "cash_refunds": cash_out,
            "paid_in": paid_in, "payouts": payouts, "by_method": by_method,
            "expected_cash": drawer.opening_float + cash_in - cash_out + paid_in - payouts}


def shift_overview(shift):
    staff = []
    for a in shift.staff.select_related("user"):
        row = {"user_id": a.user_id, "username": a.user.username, "name": a.user.get_full_name() or a.user.username,
               "role": a.role, "terminal": a.terminal, "active": a.is_active, "ended_at": a.ended_at}
        row["drawer"] = shift_summary(a)
        row.update(counted_cash=a.counted_cash, variance=a.variance)
        staff.append(row)
    orders = {r["status"]: r["n"] for r in shift.orders.values("status").annotate(n=Count("id"))}
    sales = Bill.objects.filter(order__shift=shift, status=Bill.Status.PAID).aggregate(t=Sum("total"))["t"] or D(0)
    return {"id": shift.pk, "name": shift.name, "status": shift.status, "opened_at": shift.opened_at,
            "closed_at": shift.closed_at, "staff": staff, "orders_by_status": orders, "paid_sales": sales}


def add_cash_movement(*, drawer, kind, amount, reason, user):
    if not drawer.is_active:
        raise InvalidState("This drawer is closed")
    if kind not in CashMovement.Kind.values:
        raise InvalidState(f"Unknown cash movement {kind}")
    if not reason or q(amount) <= 0:
        raise InvalidState("A positive amount and a reason are required")
    return CashMovement.objects.create(drawer=drawer, kind=kind, amount=q(amount), reason=reason, user=user)


def _count_drawer(drawer, counted_cash, by):
    summ = shift_summary(drawer)
    drawer.counted_cash = q(counted_cash)
    drawer.expected_cash = q(summ["expected_cash"])
    drawer.variance = drawer.counted_cash - drawer.expected_cash
    drawer.ended_at = timezone.now()
    drawer.save(update_fields=["counted_cash", "expected_cash", "variance", "ended_at"])
    audit(by, "drawer_closed", drawer, cashier=drawer.user.username, expected=str(drawer.expected_cash),
          counted=str(drawer.counted_cash), variance=str(drawer.variance))


@transaction.atomic
def end_assignment(*, assignment, user, counted_cash=None, force=False):
    """Sign one person off a shift. Cashiers, and waiters who collected any payment, must count their
    drawer. Waiters can't leave with open or unpaid orders unless a manager forces it."""
    a = ShiftAssignment.objects.select_for_update().select_related("shift", "user").get(pk=assignment.pk)
    if a.ended_at:
        raise InvalidState("Already signed off")
    if a.user_id != user.pk and not user.is_manager:
        raise ManagerApprovalRequired("Only the person themselves or a manager can end this")
    if a.role == Role.CASHIER:
        if counted_cash is None:
            raise InvalidState("Count the drawer: counted_cash is required")
        _count_drawer(a, counted_cash, user)
    else:
        n = Order.objects.filter(shift=a.shift, waiter=a.user, status__in=Order.OCCUPYING).count()
        if n and not (force and user.is_manager):
            raise InvalidState(f"{n} order(s) are still open or unpaid. Settle or cancel them, or ask a manager to force it")
        if a.payments.exists():
            if counted_cash is None:
                raise InvalidState("Count the cash you collected: counted_cash is required")
            _count_drawer(a, counted_cash, user)
        else:
            a.ended_at = timezone.now()
            a.save(update_fields=["ended_at"])
            audit(user, "waiter_signed_off", a, waiter=a.user.username, open_orders=n)
    return a


@transaction.atomic
def close_shift(*, shift, user, counts=None, force=False):
    """Manager closes the whole shift. counts = {user_id: counted_cash} for every cashier whose
    drawer is still open. Unsettled orders block the close unless force=True."""
    _require_manager(user, "Closing a shift")
    shift = Shift.objects.select_for_update().get(pk=shift.pk)
    if shift.status != Shift.Status.OPEN:
        raise InvalidState("Shift is already closed")
    counts = {int(k): v for k, v in (counts or {}).items()}
    staff = list(shift.staff.select_for_update().select_related("user").filter(ended_at__isnull=True))
    needs_count = lambda a: a.role == Role.CASHIER or a.payments.exists()
    missing = [a.user.username for a in staff if needs_count(a) and a.user_id not in counts]
    if missing:
        raise InvalidState("Counted cash is required for: " + ", ".join(missing))
    unsettled = shift.orders.filter(status__in=Order.OCCUPYING).count()
    if unsettled and not force:
        raise InvalidState(f"{unsettled} order(s) on this shift are still open or awaiting payment")
    for a in staff:
        if needs_count(a):
            _count_drawer(a, counts[a.user_id], user)
        else:
            a.ended_at = timezone.now()
            a.save(update_fields=["ended_at"])
    shift.status, shift.closed_at, shift.closed_by = Shift.Status.CLOSED, timezone.now(), user
    shift.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    audit(user, "shift_closed", shift, forced=bool(unsettled and force), unsettled_orders=unsettled)
    return shift


# =============================================================================
# Catalog
# =============================================================================

def station_location(station):
    return Location.objects.get(code=settings.STATION_LOCATIONS[station])


def portions_available(menu_item):
    """How many portions the station's stock can still make. None = not tracked."""
    lines = list(menu_item.recipe_lines.select_related("stock_item"))
    if not menu_item.track_stock or not lines:
        return None
    loc = station_location(menu_item.station)
    best = None
    for rl in lines:
        have = rl.stock_item.on_hand(loc)
        n = math.floor(have / rl.stock_quantity) if rl.stock_quantity > 0 else 0
        best = n if best is None else min(best, n)
    return max(best, 0)


SOURCE_STATION = {Order.Source.MENU: Station.KITCHEN, Order.Source.BAR: Station.BAR}


# =============================================================================
# Orders: open -> add items -> send (KOTs + stock deduction) -> void / cancel
# =============================================================================

def _lock_order(order, user, expected_version=None, must_be_active=True):
    order = Order.objects.select_for_update().get(pk=order.pk)
    if not (user.is_manager or order.waiter_id == user.pk):
        raise ManagerApprovalRequired(f"Order {order.number} belongs to another waiter")
    if expected_version is not None and order.version != expected_version:
        raise OrderConflict("This order was changed on another terminal. Reload and try again.")
    if must_be_active and order.status not in Order.ACTIVE:
        raise InvalidState(f"Order {order.number} is {order.get_status_display().lower()} and can't be changed")
    return order


def _touch(order):
    order.version += 1
    order.save(update_fields=["version", "updated_at"])


def release_table(order):
    """Free the table once none of its orders are still in use."""
    if order.table_id and not Order.objects.filter(table_id=order.table_id, status__in=Order.OCCUPYING).exists():
        Table.objects.filter(pk=order.table_id).update(status=Table.Status.FREE)


def next_order_number():
    """Every order, from the menu or the bar, gets the next ORD- number from ONE shared sequence.
    The sequence keeps its old name "order-menu" so numbering carries on from the existing ORD- orders
    (a new name would restart at 1 and collide with them)."""
    return DocumentSequence.next("order-menu", "ORD-")


@transaction.atomic
def open_order(*, waiter, source=Order.Source.MENU, order_type=None, table=None, room_number="", notes=""):
    """A waiter on an open shift starts an order from the menu or from the bar.

    The table is optional and a table may carry several orders; each order has its own
    number and is billed on its own."""
    assignment = _require_assignment(waiter, Role.WAITER)
    if source not in Order.Source.values:
        raise InvalidState(f"Unknown order source {source}")
    if order_type is None and room_number:
        order_type = Order.Type.ROOM_SERVICE
    if order_type is None:
        order_type = (Order.Type.BAR_TAB if source == Order.Source.BAR
                      else Order.Type.DINE_IN if table else Order.Type.TAKEAWAY)
    if order_type == Order.Type.ROOM_SERVICE and not room_number:
        raise InvalidState("Room service orders need a room number")
    if table is not None:
        table = Table.objects.select_for_update().get(pk=table.pk)
        table.status = Table.Status.OCCUPIED
        table.save(update_fields=["status"])
    order = Order.objects.create(number=next_order_number(),
                                 source=source, order_type=order_type, shift=assignment.shift, table=table,
                                 room_number=room_number, notes=notes, waiter=waiter)
    audit(waiter, "order_opened", order, source=source, table=table.name if table else None)
    return order


def _check_portions(menu_item, quantity):
    if settings.STOCK_AVAILABILITY_MODE == "block":
        left = portions_available(menu_item)
        if left is not None and left < quantity:
            raise ItemUnavailable(f"Only {left} x {menu_item.name} left in stock")


@transaction.atomic
def add_item(*, order, menu_item, user, quantity=1, modifiers=(), notes="", course=1, expected_version=None):
    """Add `quantity` of an item. An identical unsent line (same item, modifiers, note, course) is
    topped up instead of creating a duplicate line."""
    order = _lock_order(order, user, expected_version)
    if not menu_item.is_available:
        raise ItemUnavailable(f"{menu_item.name} is sold out (86'd)")
    mods = [{"name": m.name, "price": str(m.price_delta)} for m in modifiers]
    same = next((i for i in order.items.select_for_update().filter(menu_item=menu_item, status=OrderItem.Status.PENDING,
                                                                   notes=notes, course=course)
                 if i.modifiers == mods), None)
    _check_portions(menu_item, quantity + (same.quantity if same else 0))
    if same:
        same.quantity += quantity
        same.save(update_fields=["quantity"])
        item = same
    else:
        item = OrderItem.objects.create(
            order=order, menu_item=menu_item, name=menu_item.name, station=menu_item.station,
            unit_price=menu_item.price, tax_rate=menu_item.tax_rate, modifiers=mods,
            quantity=quantity, notes=notes, course=course)
    _touch(order)
    return item


@transaction.atomic
def add_items(*, order, lines, user, expected_version=None):
    """Add several lines in one go (the waiter's order form): every line goes in, or none if one fails.
    Lines may come from either selling point; each is routed to its own kitchen/bar ticket when sent."""
    if not lines:
        raise InvalidState("Add at least one item")
    order = _lock_order(order, user, expected_version)
    for ln in lines:
        add_item(order=order, menu_item=ln["menu_item"], user=user, quantity=ln["quantity"],
                 modifiers=Modifier.objects.filter(pk__in=ln["modifiers"]), notes=ln["notes"], course=ln["course"])
    return order


@transaction.atomic
def set_item_quantity(*, item, user, quantity, expected_version=None):
    """Change the quantity of an UNSENT line (sent lines must be voided)."""
    order = _lock_order(item.order, user, expected_version)
    item = OrderItem.objects.select_for_update().select_related("menu_item").get(pk=item.pk)
    if item.status != OrderItem.Status.PENDING:
        raise InvalidState("Item was already sent; use void with a manager approval")
    if not 1 <= quantity <= 500:
        raise InvalidState("Quantity must be between 1 and 500")
    _check_portions(item.menu_item, quantity)
    item.quantity = quantity
    item.save(update_fields=["quantity"])
    _touch(order)
    return item


@transaction.atomic
def remove_pending_item(*, item, user, expected_version=None):
    """Unsent items can be removed freely. Sent items must be voided."""
    order = _lock_order(item.order, user, expected_version)
    item = OrderItem.objects.select_for_update().get(pk=item.pk)
    if item.status != OrderItem.Status.PENDING:
        raise InvalidState("Item was already sent; use void with a manager approval")
    audit(user, "pending_item_removed", order, item=item.name, qty=item.quantity)
    item.delete()
    _touch(order)


def _deduct_stock(item, user):
    if not item.menu_item.track_stock:
        return
    loc = station_location(item.station)
    for rl in item.menu_item.recipe_lines.select_related("stock_item"):
        record_movement(item=rl.stock_item, location=loc, quantity=-(rl.stock_quantity * item.quantity),
                        movement_type=MovementType.SALE, user=user,
                        ref_type="order_item", ref_id=item.pk, reason=item.order.number)


@transaction.atomic
def send_order(*, order, user, expected_version=None):
    """Lock pending lines, split into one KOT per station, deduct stock."""
    order = _lock_order(order, user, expected_version)
    pending = list(order.items.select_for_update().filter(status=OrderItem.Status.PENDING)
                   .select_related("menu_item"))
    if not pending:
        raise InvalidState("Nothing new to send")
    by_station = defaultdict(list)
    for it in pending:
        by_station[it.station].append(it)
    kots = []
    for station, items in by_station.items():
        kot = KOT.objects.create(order=order, station=station, created_by=user,
                                 number=DocumentSequence.next("kot", "K-"))
        for it in items:
            it.kot, it.status = kot, OrderItem.Status.SENT
            it.save(update_fields=["kot", "status"])
            _deduct_stock(it, user)
        kots.append(kot)
    order.status = Order.Status.SENT
    order.version += 1
    order.save(update_fields=["status", "version", "updated_at"])
    return kots


@transaction.atomic
def void_item(*, item, user, reason, wasted=False, manager_pin=None, expected_version=None):
    """Void a SENT item. Needs a manager (the user themselves, or a manager PIN).

    wasted=False: the item was never made -> stock is returned.
    wasted=True : it was made and thrown away -> stock stays deducted and the
                  ledger records it as WASTE.
    """
    if not reason:
        raise InvalidState("A void needs a reason")
    approver = user if user.is_manager else find_manager_by_pin(manager_pin)
    if approver is None:
        raise ManagerApprovalRequired("Voiding a sent item needs manager approval")

    order = _lock_order(item.order, user, expected_version)
    item = OrderItem.objects.select_for_update().get(pk=item.pk)
    if item.status != OrderItem.Status.SENT:
        raise InvalidState("Only sent items can be voided")

    if item.menu_item.track_stock:
        sales = StockMovement.objects.filter(ref_type="order_item", ref_id=str(item.pk),
                                             movement_type=MovementType.SALE)
        for m in sales:  # reverse exactly what was deducted, whatever the recipe says today
            record_movement(item=m.item, location=m.location, quantity=-m.quantity,
                            movement_type=MovementType.SALE_REVERSAL, unit_cost=m.unit_cost,
                            user=approver, ref_type="order_item", ref_id=item.pk, reason=f"Void: {reason}")
            if wasted:
                record_movement(item=m.item, location=m.location, quantity=m.quantity,  # m.quantity is negative
                                movement_type=MovementType.WASTE, unit_cost=m.unit_cost, user=approver,
                                ref_type="order_item", ref_id=item.pk, reason=f"Void (wasted): {reason}")

    item.status, item.void_reason, item.voided_by = OrderItem.Status.VOID, reason, approver
    item.save(update_fields=["status", "void_reason", "voided_by"])
    KOT.objects.create(order=order, station=item.station, kind=KOT.Kind.VOID, created_by=approver,
                       number=DocumentSequence.next("kot", "K-"))
    audit(approver, "item_voided", order, item=item.name, qty=item.quantity, amount=str(item.line_total),
          reason=reason, wasted=wasted, requested_by=user.username)
    _touch(order)
    return item


@transaction.atomic
def cancel_order(*, order, user, expected_version=None):
    """Cancel an order that has no live items (never filled, or everything voided).
    This frees the table, which would otherwise stay occupied by an empty order."""
    order = _lock_order(order, user, expected_version)
    if order.items.exclude(status=OrderItem.Status.VOID).exists():
        raise InvalidState("The order still has items. Remove or void them first")
    order.status = Order.Status.CANCELLED
    order.version += 1
    order.save(update_fields=["status", "version", "updated_at"])
    release_table(order)
    audit(user, "order_cancelled", order)
    return order



def _check_order_access(user, order):
    """Same rule as billing: a waiter only touches their own orders; cashiers and managers touch any."""
    if user.is_manager:
        return
    _check_bill_access(user, _require_drawer(user), order)


def _require_changeable(order):
    if order.status == Order.Status.BILLED:
        raise InvalidState(f"Order {order.number} is already billed. Cancel its bill first")
    if order.status not in Order.ACTIVE:
        raise InvalidState(f"Order {order.number} is {order.get_status_display().lower()} and can't be changed")


@transaction.atomic
def merge_orders(*, target, sources, user, expected_version=None):
    """Fold every live line of the `sources` orders into `target`, so one bill covers them all.

    Use it when one party ordered in several rounds, or several guests under different orders want
    to pay together. Sources are marked MERGED (kept for the audit trail, never deleted) and their
    tables are released. Nothing is billed yet: all orders must still be open/in progress."""
    source_ids = {s.pk for s in sources}
    if not source_ids:
        raise InvalidState("Pick at least one order to merge")
    if target.pk in source_ids:
        raise InvalidState("An order can't be merged into itself")
    # Lock in primary-key order so two terminals merging at the same time can't deadlock.
    locked = {o.pk: o for o in Order.objects.select_for_update().filter(pk__in=source_ids | {target.pk}).order_by("pk")}
    if len(locked) != len(source_ids) + 1:
        raise InvalidState("One of those orders no longer exists")
    target = locked[target.pk]
    if expected_version is not None and target.version != expected_version:
        raise OrderConflict("This order was changed on another terminal. Reload and try again.")
    for o in locked.values():
        _check_order_access(user, o)
        _require_changeable(o)

    srcs = [locked[i] for i in sorted(source_ids)]
    moved = OrderItem.objects.filter(order_id__in=source_ids).exclude(status=OrderItem.Status.VOID)
    count = moved.update(order=target)  # voided lines stay behind on the source as history
    for s in srcs:
        s.status, s.merged_into = Order.Status.MERGED, target
        s.version += 1
        s.save(update_fields=["status", "merged_into", "version", "updated_at"])
        release_table(s)
    if target.status == Order.Status.OPEN and target.items.filter(status=OrderItem.Status.SENT).exists():
        target.status = Order.Status.SENT
    target.version += 1
    target.save(update_fields=["status", "version", "updated_at"])
    audit(user, "orders_merged", target, merged=[s.number for s in srcs], lines=count,
          waiters=sorted({s.waiter.username for s in srcs} | {target.waiter.username}))
    return target


@transaction.atomic
def split_order(*, source, item_ids, user, expected_version=None):
    """Move the chosen lines of `source` to a brand-new order (same table and waiter) so that
    guest can be billed separately. Whole lines only: a line that was already sent has stock booked
    against it, so it must stay in one piece."""
    item_ids = {int(i) for i in item_ids}
    if not item_ids:
        raise InvalidState("Pick the items to move to the new order")
    source = Order.objects.select_for_update().get(pk=source.pk)
    _check_order_access(user, source)
    if expected_version is not None and source.version != expected_version:
        raise OrderConflict("This order was changed on another terminal. Reload and try again.")
    _require_changeable(source)
    live = list(source.items.select_for_update().exclude(status=OrderItem.Status.VOID))
    chosen = [i for i in live if i.pk in item_ids]
    if len(chosen) != len(item_ids):
        raise InvalidState("Some of those items are no longer on this order")
    if len(chosen) == len(live):
        raise InvalidState("That is every item on the order. Pick fewer items, or merge orders instead")

    new = Order.objects.create(number=next_order_number(),
                               source=source.source, order_type=source.order_type, shift=source.shift,
                               table=source.table, room_number=source.room_number, waiter=source.waiter)
    OrderItem.objects.filter(pk__in=item_ids).update(order=new)
    if any(i.status == OrderItem.Status.SENT for i in chosen):
        new.status = Order.Status.SENT
        new.save(update_fields=["status", "updated_at"])
    _touch(source)
    audit(user, "order_split", source, new_order=new.number, items=[i.name for i in chosen])
    return new


# =============================================================================
# Billing: one bill per order
# =============================================================================

def compute_bill_amounts(order, discount_amount=None, discount_percent=None):
    """Discount is taken off the menu-price basis (what the guest sees), spread
    proportionally over lines so tax stays correct."""
    items = list(order.items.exclude(status=OrderItem.Status.VOID).select_related("menu_item__category"))
    basis_total = sum((i.line_total for i in items), D(0))
    if discount_percent:
        disc = q(basis_total * D(discount_percent) / 100)
    else:
        disc = q(discount_amount or 0)
    if disc < 0 or disc > basis_total:
        raise InvalidState("Discount must be between 0 and the bill total")
    factor = (basis_total - disc) / basis_total if basis_total else D(1)

    lines, net_sum, tax_sum = [], D(0), D(0)
    for i in items:
        basis, r = i.line_total, i.tax_rate
        if settings.PRICES_INCLUDE_TAX:
            net = basis / (1 + r / 100)
            tax = basis - net
        else:
            net, tax = basis, basis * r / 100
        net, tax = net * factor, tax * factor
        net_sum += net
        tax_sum += tax
        lines.append({"name": i.name, "category": i.menu_item.category.name, "quantity": i.quantity,
                      "unit_price": str(i.unit_total), "line_total": str(q(basis)),
                      "modifiers": [m["name"] for m in i.modifiers], "tax_rate": str(r),
                      "net": str(q(net)), "tax": str(q(tax))})
    tax = q(tax_sum)
    if settings.PRICES_INCLUDE_TAX:
        net = q(basis_total - disc) - tax
    else:
        net = q(net_sum)
    service = q(net * D(settings.SERVICE_CHARGE_PERCENT) / 100)
    return {"lines": lines, "subtotal": net, "tax": tax, "discount": disc, "service_charge": service,
            "total": net + tax + service}


@transaction.atomic
def generate_bill(*, order, user, discount_percent=None, discount_amount=None, discount_reason="", manager_pin=None):
    """Bill ONE order. The waiter bills their own order; a cashier can bill any order. Bill numbers
    come from the cashier's terminal, or the shared WTR series for waiters."""
    drawer = _require_drawer(user)
    order = Order.objects.select_for_update().get(pk=order.pk)
    _check_bill_access(user, drawer, order)
    if order.status == Order.Status.BILLED:
        raise InvalidState("Order is already billed. Cancel the bill first to change the order.")
    if order.status not in Order.ACTIVE:
        raise InvalidState(f"Order {order.number} is {order.get_status_display().lower()}")
    if order.items.filter(status=OrderItem.Status.PENDING).exists():
        raise InvalidState("Send or remove unsent items before billing")
    if not order.items.exclude(status=OrderItem.Status.VOID).exists():
        raise InvalidState("Nothing to bill")

    approver = None
    if discount_percent or discount_amount:
        approver = _approver(user, manager_pin, "A discount")
        if not discount_reason:
            raise InvalidState("A discount needs a reason")

    a = compute_bill_amounts(order, discount_amount, discount_percent)
    terminal = drawer.terminal or "WTR"  # waiters share one bill series; each cashier terminal has its own
    bill = Bill.objects.create(
        number=DocumentSequence.next(f"bill-{terminal}", f"{terminal}-B"),
        order=order, terminal=terminal, lines=a["lines"], subtotal=a["subtotal"], tax=a["tax"],
        discount_amount=a["discount"], discount_reason=discount_reason, discount_approved_by=approver,
        service_charge=a["service_charge"], total=a["total"], created_by=user)
    order.status = Order.Status.BILLED
    order.version += 1
    order.save(update_fields=["status", "version", "updated_at"])
    if approver:
        audit(approver, "discount_given", bill, amount=str(a["discount"]), reason=discount_reason)
    return bill


@transaction.atomic
def cancel_bill(*, bill, user, reason, manager_pin=None):
    """Cancel an unpaid bill (reopens the order). The bill number is kept, never reused."""
    approver = _approver(user, manager_pin, "Cancelling a bill")
    if not reason:
        raise InvalidState("A reason is required")
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    if bill.status != Bill.Status.OPEN:
        raise InvalidState("Only bills awaiting payment can be cancelled")
    if bill.amount_paid != 0:
        raise InvalidState("Refund the payments on this bill before cancelling it")
    bill.status, bill.cancel_reason = Bill.Status.CANCELLED, reason
    bill.cancelled_by, bill.cancelled_at = approver, timezone.now()
    bill.save(update_fields=["status", "cancel_reason", "cancelled_by", "cancelled_at", "updated_at"])
    if bill.order_id:
        order = Order.objects.select_for_update().get(pk=bill.order_id)
        order.status = Order.Status.SENT
        order.version += 1
        order.save(update_fields=["status", "version", "updated_at"])
    audit(approver, "bill_cancelled", bill, reason=reason)
    return bill


@transaction.atomic
def take_payment(*, bill, user, method, amount, reference="", tendered=None, idempotency_key=None):
    """Split payments = call this several times. Pass an idempotency_key so a
    double-tap or network retry returns the original payment instead of paying twice."""
    drawer = _require_drawer(user)
    bill = Bill.objects.select_for_update().get(pk=bill.pk)
    _check_bill_access(user, drawer, bill.document)
    if idempotency_key:
        existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing
    if bill.status != Bill.Status.OPEN:
        raise InvalidState(f"Bill {bill.number} is {bill.get_status_display().lower()}")
    if method not in Payment.Method.values:
        raise InvalidState(f"Unknown payment method {method}")
    amount = q(amount)
    if amount <= 0:
        raise InvalidState("Amount must be positive")
    if amount > bill.balance:
        raise InvalidState(f"Amount exceeds the balance of {bill.balance}")
    if method == Payment.Method.CASH:
        tendered = q(tendered) if tendered is not None else amount
        if tendered < amount:
            raise InvalidState("Cash tendered is less than the amount")
    else:
        tendered = None
        if method in (Payment.Method.CARD, Payment.Method.MOBILE_MONEY) and not reference:
            raise InvalidState("Enter the card slip / transaction reference")

    payment = Payment.objects.create(bill=bill, drawer=drawer, method=method, amount=amount, tendered=tendered,
                                     reference=reference, idempotency_key=idempotency_key, user=user)
    bill.amount_paid += amount
    if bill.balance == 0:
        bill.status, bill.paid_at = Bill.Status.PAID, timezone.now()
        if bill.order_id:   # a room bill has no order to close; the booking just shows as paid
            order = Order.objects.select_for_update().get(pk=bill.order_id)
            order.status = Order.Status.PAID
            order.version += 1
            order.save(update_fields=["status", "version", "updated_at"])
            release_table(order)
    bill.save()
    return payment


@transaction.atomic
def refund_payment(*, payment, user, reason, amount=None, manager_pin=None):
    approver = _approver(user, manager_pin, "A refund")
    if not reason:
        raise InvalidState("A reason is required")
    drawer = _require_drawer(user)  # the refund comes out of the current user's drawer
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.kind != Payment.Kind.PAYMENT:
        raise InvalidState("Can only refund a payment")
    already = -(payment.refunds.aggregate(t=Sum("amount"))["t"] or D(0))
    remaining = payment.amount - already
    amount = q(amount) if amount is not None else remaining
    if amount <= 0 or amount > remaining:
        raise InvalidState(f"Refund must be between 0.01 and {remaining}")

    bill = Bill.objects.select_for_update().get(pk=payment.bill_id)
    refund = Payment.objects.create(bill=bill, drawer=drawer, kind=Payment.Kind.REFUND, method=payment.method,
                                    amount=-amount, reason=reason, refund_of=payment, user=approver,
                                    reference=payment.reference)
    bill.amount_paid -= amount
    if bill.status == Bill.Status.PAID and bill.amount_paid == 0:
        bill.status = Bill.Status.REFUNDED
    bill.save()
    audit(approver, "payment_refunded", bill, amount=str(amount), reason=reason, method=payment.method)
    return refund


# =============================================================================
# End of day (Z report)
# =============================================================================

EPOCH = datetime(1970, 1, 1, tzinfo=dt_tz.utc)


def _period_start():
    last = ZReport.objects.order_by("-period_end").first()
    return last.period_end if last else EPOCH


def day_blockers():
    out = []
    n = Order.objects.filter(status__in=Order.OCCUPYING).count()
    if n:
        out.append(f"{n} order(s) still open or awaiting payment")
    n = Bill.objects.filter(booking__isnull=False, status=Bill.Status.OPEN).count()
    if n:
        out.append(f"{n} room bill(s) awaiting payment")
    n = Shift.objects.filter(status=Shift.Status.OPEN).count()
    if n:
        out.append(f"{n} shift(s) still open")
    return out


def build_z_data(start, end):
    paid = Bill.objects.filter(status=Bill.Status.PAID, paid_at__gte=start, paid_at__lt=end)
    sums = paid.aggregate(net=Sum("subtotal"), tax=Sum("tax"), service=Sum("service_charge"),
                          levy=Sum("levy"), discounts=Sum("discount_amount"), total=Sum("total"))
    by_cat = defaultdict(lambda: D(0))
    for b in paid:
        for ln in b.lines:
            by_cat[ln["category"]] += D(ln["net"])
    pays = Payment.objects.filter(created_at__gte=start, created_at__lt=end)
    by_method = {r["method"]: r["t"] for r in pays.values("method").annotate(t=Sum("amount"))}
    voids = AuditLog.objects.filter(action="item_voided", created_at__gte=start, created_at__lt=end)
    void_value = sum((D(v.details.get("amount", "0")) for v in voids), D(0))
    drawers = ShiftAssignment.objects.filter(counted_cash__isnull=False,
                                             ended_at__gte=start, ended_at__lt=end)
    return {
        "sales": {"bills": paid.count(), "net": sums["net"] or 0, "tax": sums["tax"] or 0,
                  "service_charge": sums["service"] or 0, "levy": sums["levy"] or 0, "discounts": sums["discounts"] or 0,
                  "gross_total": sums["total"] or 0},
        "net_by_category": dict(by_cat),
        "payments_by_method": by_method,  # net of refunds
        "voids": {"count": voids.count(), "value": void_value},
        "cancelled_bills": Bill.objects.filter(status=Bill.Status.CANCELLED, cancelled_at__gte=start,
                                               cancelled_at__lt=end).count(),
        "refunded_bills": Bill.objects.filter(status=Bill.Status.REFUNDED, updated_at__gte=start,
                                              updated_at__lt=end).count(),
        "shifts": {"closed": Shift.objects.filter(status=Shift.Status.CLOSED, closed_at__gte=start,
                                                  closed_at__lt=end).count(),
                   "drawers_counted": drawers.count(),
                   "cash_variance": drawers.aggregate(t=Sum("variance"))["t"] or 0},
    }


def preview_day():
    start, end = _period_start(), timezone.now()
    return {"period_start": start, "period_end": end, "blockers": day_blockers(), **build_z_data(start, end)}


@transaction.atomic
def close_day(*, user):
    if not user.is_manager:
        raise ManagerApprovalRequired("Only a manager can close the day")
    blockers = day_blockers()
    if blockers:
        raise InvalidState("Cannot close the day: " + "; ".join(blockers))
    start, end = _period_start(), timezone.now()
    data = json.loads(json.dumps(build_z_data(start, end), cls=DjangoJSONEncoder))
    report = ZReport.objects.create(number=DocumentSequence.next("zreport", "Z-", 4), period_start=start,
                                    period_end=end, data=data, closed_by=user)
    audit(user, "day_closed", report)
    return report


# =============================================================================
# Serializers
# =============================================================================

def clean(data):  # make Decimals/datetimes JSON-friendly
    return json.loads(json.dumps(data, cls=DjangoJSONEncoder))


class OrderItemSerializer(serializers.ModelSerializer):
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)

    class Meta:
        model = OrderItem
        fields = ["id", "name", "station", "quantity", "unit_price", "modifiers", "notes",
                  "course", "status", "void_reason", "line_total"]


class OrderSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)
    table_name = serializers.CharField(source="table.name", default=None, read_only=True)
    waiter_name = serializers.SerializerMethodField()
    totals = serializers.SerializerMethodField()
    bill_id = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "number", "source", "order_type", "shift", "table", "table_name", "room_number", "notes",
                  "waiter", "waiter_name", "status", "version", "created_at", "items", "totals", "bill_id"]

    def get_waiter_name(self, obj):
        return obj.waiter.get_full_name() or obj.waiter.username

    def get_bill_id(self, obj):
        live = [b for b in obj.bills.all() if b.status in ("OPEN", "PAID")]
        return live[0].id if live else None

    def get_totals(self, obj):
        return {k: f"{v:.2f}" for k, v in obj.totals().items()}


def order_qs():
    return Order.objects.select_related("table", "waiter").prefetch_related("items", "bills")


class PinLoginIn(serializers.Serializer):
    username = serializers.CharField()
    pin = serializers.CharField()


class ShiftStaffIn(serializers.Serializer):
    user = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True))
    role = serializers.ChoiceField(choices=ShiftAssignment.Role.choices)
    terminal = serializers.CharField(required=False, allow_blank=True, default="", max_length=10)
    opening_float = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, default=0, min_value=0)


class ShiftCreateIn(serializers.Serializer):
    name = serializers.CharField(required=False, allow_blank=True, default="", max_length=60)
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=300)
    staff = ShiftStaffIn(many=True, allow_empty=False)


class LineIn(serializers.Serializer):
    menu_item = serializers.PrimaryKeyRelatedField(queryset=MenuItem.objects.all())
    quantity = serializers.IntegerField(min_value=1, max_value=500, default=1)
    modifiers = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
    course = serializers.IntegerField(min_value=1, max_value=10, default=1)


class AddItemIn(LineIn):
    version = serializers.IntegerField(required=False, allow_null=True, default=None)


class OrderCreateIn(serializers.Serializer):
    source = serializers.ChoiceField(choices=Order.Source.choices, default=Order.Source.MENU)  # selling point it starts from
    order_type = serializers.ChoiceField(choices=Order.Type.choices, required=False)
    table = serializers.PrimaryKeyRelatedField(queryset=Table.objects.all(), required=False, allow_null=True)
    room_number = serializers.CharField(required=False, allow_blank=True, default="", max_length=10)
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=300)
    items = LineIn(many=True, required=False, default=list, max_length=100)   # optional: create the order with its lines


class BulkItemsIn(serializers.Serializer):
    items = LineIn(many=True, allow_empty=False, max_length=100)
    version = serializers.IntegerField(required=False, allow_null=True, default=None)


def bill_data(b):
    o, bk = b.order, b.booking
    who = o.waiter if o else bk.booked_by
    return {"id": b.id, "number": b.number, "order": b.order_id, "order_number": o.number if o else None,
            "booking": b.booking_id, "booking_number": bk.number if bk else None,
            "status": b.status, "created_at": b.created_at, "terminal": b.terminal,
            "table": o.table.name if o and o.table_id else None, "waiter": who.get_full_name() or who.username, "lines": b.lines, "subtotal": b.subtotal, "discount": b.discount_amount,
            "service_charge": b.service_charge, "levy": b.levy, "tax": b.tax, "total": b.total, "paid": b.amount_paid,
            "balance": b.balance,
            "payments": [{"id": p.id, "kind": p.kind, "method": p.method, "amount": p.amount,
                          "reference": p.reference, "change_due": p.change_due} for p in b.payments.all()]}


def render_receipt(bill, autoprint=False):
    """80 mm thermal-printer receipt as a standalone HTML page. Used by the POS screen and the admin.
    Optional settings: POS_BUSINESS_NAME, POS_BUSINESS_ADDRESS, POS_BUSINESS_PHONE, POS_BUSINESS_TAX_ID, POS_RECEIPT_FOOTER."""
    g = lambda k, d="": getattr(settings, k, d)
    m = lambda x: f"{D(x):,.2f}"
    order, booking = bill.order, bill.booking
    rows = "".join(
        f"<tr><td>{l['quantity']} x {esc(l['name'])}" + "".join(f"<br>&nbsp;&nbsp;+ {esc(x)}" for x in l["modifiers"])
        + f"</td><td class=r>{m(l['line_total'])}</td></tr>" for l in bill.lines)
    items_total = sum((D(l["line_total"]) for l in bill.lines), D(0))
    t = [("Items total", items_total)]
    if bill.discount_amount:
        t.append((f"Discount{' (' + esc(bill.discount_reason) + ')' if bill.discount_reason else ''}", -bill.discount_amount))
    if bill.service_charge:
        t.append(("Service charge", bill.service_charge))
    if bill.levy:
        t.append(("Catering levy", bill.levy))
    t.append(("VAT" if booking else "VAT included" if settings.PRICES_INCLUDE_TAX else "VAT", bill.tax))
    pay = "".join(f"<tr><td>{p.get_method_display()}{' (' + esc(p.reference) + ')' if p.reference else ''}"
                  f"{' - refund' if p.kind == 'REFUND' else ''}</td><td class=r>{m(p.amount)}</td></tr>"
                  + (f"<tr><td>&nbsp;&nbsp;Change</td><td class=r>{m(p.change_due)}</td></tr>" if p.change_due > 0 else "")
                  for p in bill.payments.all())
    taken = next((p for p in bill.payments.select_related("user", "drawer")
                  if p.kind == "PAYMENT" and p.drawer.role == "CASHIER"), None)
    cashier_row = (f"<tr><td>Cashier</td><td class=r>{esc(taken.user.get_full_name() or taken.user.username)}</td></tr>"
                   if taken else "")
    stamp = {"OPEN": f"AMOUNT DUE {m(bill.balance)}", "PAID": "PAID", "CANCELLED": "CANCELLED - NOT A VALID BILL",
             "REFUNDED": "REFUNDED"}.get(bill.status, "")
    head = "".join(f"<div class=c>{esc(g(k))}</div>" for k in ("POS_BUSINESS_ADDRESS", "POS_BUSINESS_PHONE") if g(k))
    taxid = f"<div class=c>PIN: {esc(g('POS_BUSINESS_TAX_ID'))}</div>" if g("POS_BUSINESS_TAX_ID") else ""
    if order:
        where = order.table.name if order.table_id else ("Bar" if order.source == "BAR" else "No table")
        who = order.waiter.get_full_name() or order.waiter.username
        meta = (f"<tr><td>Order</td><td class=r>{esc(order.number)}</td></tr>\n<tr><td>Table</td><td class=r>{esc(where)}</td></tr>"
                f"<tr><td>Served by</td><td class=r>{esc(who)}</td></tr>")
    else:
        who = booking.booked_by.get_full_name() or booking.booked_by.username
        meta = (f"<tr><td>Booking</td><td class=r>{esc(booking.number)}</td></tr>\n<tr><td>Guest</td><td class=r>{esc(booking.guest_name)}</td></tr>"
                f"<tr><td>Stay</td><td class=r>{booking.check_in:%d %b} - {booking.check_out:%d %b %Y}</td></tr>"
                f"<tr><td>Booked by</td><td class=r>{esc(who)}</td></tr>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Bill {esc(bill.number)}</title><style>
@page{{size:80mm auto;margin:3mm}}body{{font:12px/1.35 'Courier New',monospace;width:74mm;margin:0 auto;color:#000}}
h1{{font-size:17px;text-align:center;margin:0}}.c{{text-align:center}}table{{width:100%;border-collapse:collapse}}td{{vertical-align:top;padding:1px 0}}
td.r{{text-align:right;white-space:nowrap;padding-left:6px}}hr{{border:0;border-top:1px dashed #000;margin:6px 0}}
.tot td{{font-weight:bold;font-size:15px}}.stamp{{text-align:center;border:2px solid #000;font-weight:bold;margin:8px 0;padding:3px}}</style></head><body>
<h1>{esc(g('POS_BUSINESS_NAME', 'Nova PoS'))}</h1>{head}{taxid}<hr>
<table><tr><td>Bill</td><td class=r>{esc(bill.number)}</td></tr>{meta}
{cashier_row}<tr><td>Date</td><td class=r>{timezone.localtime(bill.created_at):%d %b %Y %H:%M}</td></tr></table><hr>
<table>{rows}</table><hr><table>{"".join(f"<tr><td>{a}</td><td class=r>{m(b)}</td></tr>" for a, b in t)}
<tr class=tot><td>TOTAL</td><td class=r>{m(bill.total)}</td></tr></table>
{"<hr><table>" + pay + "</table>" if pay else ""}<div class="stamp">{stamp}</div>
<div class=c>{esc(g('POS_RECEIPT_FOOTER', 'Thank you - karibu tena!'))}</div>
{"<script>window.onload=function(){window.print()}</script>" if autoprint else ""}</body></html>"""


def _version(d):
    v = d.get("version")
    return None if v in (None, "") else int(v)


# =============================================================================
# API views
# =============================================================================

def exception_handler(exc, context):
    """DomainError -> {error, detail} with its status code; everything else -> DRF default.
    POSView uses this directly, so the EXCEPTION_HANDLER setting is no longer needed."""
    if isinstance(exc, DomainError):
        return Response({"error": exc.__class__.__name__, "detail": str(exc)}, status=exc.status_code)
    return drf_handler(exc, context)


class IsManager(BasePermission):
    message = "Managers only"

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_manager)


class POSView(APIView):
    authentication_classes = [PinTokenAuthentication, ShiftSessionAuthentication]
    permission_classes = [IsAuthenticated]

    def get_exception_handler(self):
        # Always use ours, whatever REST_FRAMEWORK["EXCEPTION_HANDLER"] says (or points at).
        return exception_handler


# ---------- login / session ----------

POS_TEMPLATE = getattr(settings, "POS_TEMPLATE", "Chokies/pos.html")


POS_LOGIN_TEMPLATE = getattr(settings, "POS_LOGIN_TEMPLATE", "Chokies/login.html")
POS_HOME_URL = getattr(settings, "POS_HOME_URL", "/")
POS_LOGIN_URL = getattr(settings, "POS_LOGIN_URL", "/pin-login/")        # same setting manager_views.py uses
POS_MANAGER_HOME = getattr(settings, "POS_MANAGER_HOME", "manager:dashboard")   # URL name managers land on

_FALLBACK_LOGIN = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Chokies POS - Sign in</title>
<style>body{font-family:system-ui,sans-serif;background:#111827;color:#f9fafb;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}form{background:#1f2937;padding:2rem;border-radius:12px;
width:min(92vw,320px);display:grid;gap:.9rem}h1{font-size:1.2rem;margin:0}input,button{font:inherit;padding:.75rem;
border-radius:8px;border:1px solid #374151;background:#111827;color:inherit}button{background:#f59e0b;color:#111;
border:0;font-weight:600;cursor:pointer}.err{color:#fca5a5;font-size:.9rem}</style></head><body>
<form method="post"><h1>Chokies POS</h1>%(error)s
<input name="username" placeholder="Username" value="%(username)s" autocomplete="username" autofocus required>
<input name="pin" type="password" inputmode="numeric" placeholder="PIN" autocomplete="current-password" required>
<input type="hidden" name="next" value="%(next)s"><button>Sign in</button></form></body></html>"""


def _wants_json(request):
    return (request.content_type == "application/json" or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in request.headers.get("Accept", "").split(",")[0])


def _session_ok(user):
    """A browser session is usable: signed in and, for waiters / cashiers, still on an open shift."""
    return bool(user.is_authenticated and not (user.needs_shift and active_assignment(user) is None))


def _manager_home():
    try:
        return reverse(POS_MANAGER_HOME)
    except NoReverseMatch:
        return POS_HOME_URL


def landing_url(user):
    """Where someone starts after signing in. Decided by who they are, not by anything they pick on the login page:
    managers / admins -> the back office; waiters and cashiers (already checked to be on an open shift) -> the POS,
    which then shows the waiter or cashier screen from their shift role."""
    return _manager_home() if user.is_manager else POS_HOME_URL


def _safe_next(request, nxt, user):
    """Honour ?next= only if it stays on this site and the person is allowed there: a waiter or cashier is never
    sent to a manager page that would answer 403, and nobody is sent back to the login page."""
    if not nxt or not url_has_allowed_host_and_scheme(nxt, {request.get_host()}, request.is_secure()):
        return None
    path = urlsplit(nxt).path
    if path.startswith(POS_LOGIN_URL):
        return None
    mgr = _manager_home()
    # _manager_home() falls back to POS_HOME_URL ("/") when the manager URL cannot be reversed; every path starts with
    # "/", so only apply this guard when there is a real, separate manager area.
    if mgr != POS_HOME_URL and not user.is_manager and path.startswith(mgr):
        return None
    return nxt


def _login_page(request, error="", username="", status=200):
    name = business_info()["name"]
    first, _, rest = name.partition(" ")        # the logo shows the first word in one colour, the rest in another
    ctx = {"error": error, "username": username, "next": request.GET.get("next") or request.POST.get("next", ""),
           "business_name": name, "brand_first": first, "brand_rest": rest.strip(),
           "login_image": getattr(settings, "POS_LOGIN_IMAGE", ""), "pin_length": POS_PIN_LENGTH}
    try:
        return render(request, POS_LOGIN_TEMPLATE, ctx, status=status)
    except TemplateDoesNotExist:
        log.warning("Login template %r not found, showing the plain built-in form. Put login.html at "
                    "Chokies/templates/Chokies/login.html or set POS_LOGIN_TEMPLATE.", POS_LOGIN_TEMPLATE)
        from django.utils.html import escape
        html = _FALLBACK_LOGIN % {"error": f'<div class="err">{escape(error)}</div>' if error else "",
                                  "username": escape(username), "next": escape(ctx["next"])}
        return HttpResponse(html, status=status)


@csrf_exempt
def pin_login(request):
    """The one sign-in page for waiters, cashiers and managers: you select your PIN, nothing else.

    GET  -> the login page (template POS_LOGIN_TEMPLATE, or a built-in form if it is missing).
    POST -> {pin, next?}. The server finds who owns the PIN, checks their role and, for waiters / cashiers, that they are
            on an open shift (same rules as the API), starts a Django session and works out where they start:
              manager / admin -> back office       waiter / cashier -> POS (screen chosen from their shift role)
            JSON / AJAX post: {token, user, role, redirect}, or {error, detail} with the HTTP status.
            Browser form post: redirect there, or redisplay the form with the error.
            (An optional `username` still selects the older username + PIN check, for scripts and the built-in form.)"""
    if request.method == "GET":
        return _login_page(request)
    if request.method != "POST":
        return HttpResponse(status=405, headers={"Allow": "GET, POST"})
    username = ""
    try:
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST
        username, pin = str(data.get("username", "")).strip(), str(data.get("pin", ""))
        nxt = str(data.get("next") or "")
        result = (authenticate_pin(username=username, pin=pin) if username
                  else authenticate_by_pin(pin=pin, request=request))
    except (ValueError, AttributeError):
        return JsonResponse({"error": "BadRequest", "detail": "Send the pin"}, status=400)
    except DomainError as e:
        if _wants_json(request):
            return JsonResponse({"error": e.__class__.__name__, "detail": str(e)}, status=e.status_code)
        return _login_page(request, error=str(e), username=username, status=e.status_code)
    user = result.pop("_user", None) or User.objects.get(pk=result["user"]["id"])      # PIN-only sign-in already has the user
    django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    shift = result["user"]["shift"]
    result["role"] = "manager" if user.is_manager else (shift["role"] if shift else user.role).lower()
    result["redirect"] = _safe_next(request, nxt, user) or landing_url(user)
    if _wants_json(request):
        return JsonResponse(result)
    return redirect(result["redirect"])


@ensure_csrf_cookie
def pos_page(request):
    """Serves the POS single page. Signing in happens on the login page only: anyone without a valid session
    (never signed in, or their shift has ended) is sent there."""
    user = request.user
    if not _session_ok(user):
        return redirect(POS_LOGIN_URL)
    return render(request, POS_TEMPLATE, {"pos_user": user_payload(user), "login_url": POS_LOGIN_URL})


class PinLoginView(POSView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        d = PinLoginIn(data=request.data)
        d.is_valid(raise_exception=True)
        return Response(authenticate_pin(**d.validated_data))


class LogoutView(POSView):
    def post(self, request):
        u = request.user
        u.token_version += 1  # kills every token this user holds
        u.save(update_fields=["token_version"])
        django_logout(request._request)
        return Response(status=204)


class MeView(POSView):
    def get(self, request):
        return Response(user_payload(request.user))


# ---------- shifts ----------

class ShiftListCreateView(POSView):
    permission_classes = [IsManager]

    def get(self, request):
        qs = Shift.objects.all()
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"].upper())
        return Response([{"id": s.id, "name": s.name, "status": s.status, "opened_at": s.opened_at,
                          "closed_at": s.closed_at} for s in qs[:100]])

    def post(self, request):
        d = ShiftCreateIn(data=request.data)
        d.is_valid(raise_exception=True)
        shift = create_shift(manager=request.user, **d.validated_data)
        return Response(clean(shift_overview(shift)), status=201)


class ShiftDetailView(POSView):
    permission_classes = [IsManager]

    def get(self, request, pk):
        return Response(clean(shift_overview(get_object_or_404(Shift, pk=pk))))


class ShiftStaffAddView(POSView):
    permission_classes = [IsManager]

    def post(self, request, pk):
        d = ShiftStaffIn(data=request.data)
        d.is_valid(raise_exception=True)
        shift = get_object_or_404(Shift, pk=pk)
        add_staff(shift=shift, manager=request.user, **d.validated_data)
        return Response(clean(shift_overview(shift)), status=201)


class ShiftStaffEndView(POSView):
    """Manager signs one person off (cashiers need counted_cash)."""
    permission_classes = [IsManager]

    def post(self, request, pk, user_id):
        a = get_object_or_404(ShiftAssignment, shift_id=pk, user_id=user_id)
        counted = request.data.get("counted_cash")
        end_assignment(assignment=a, user=request.user, force=bool(request.data.get("force")),
                       counted_cash=None if counted in (None, "") else _money(counted, "counted_cash"))
        return Response(clean(shift_overview(a.shift)))


class ShiftCloseView(POSView):
    """Body: {"counts": {"<cashier user id>": "1500.00"}, "force": false}"""
    permission_classes = [IsManager]

    def post(self, request, pk):
        counts = {k: _money(v, "counted cash") for k, v in (request.data.get("counts") or {}).items()}
        shift = close_shift(shift=get_object_or_404(Shift, pk=pk), user=request.user, counts=counts,
                            force=bool(request.data.get("force")))
        return Response(clean(shift_overview(shift)))


class MyShiftView(POSView):
    """The logged-in waiter/cashier's own place on the shift (cashiers also see their drawer)."""

    def get(self, request):
        a = active_assignment(request.user)
        if not a:
            return Response({"shift": None})
        out = {**user_payload(request.user, a)}
        out["drawer"] = shift_summary(a)
        return Response(clean(out))


class MySalesView(POSView):
    """The signed-in user's own paid bills on the shift they are working now, newest first.
    Feeds the waiter's "My sales" modal and the totals on the home screen."""

    def get(self, request):
        a = active_assignment(request.user)
        empty = {"summary": {"count": 0, "total": "0.00", "by_method": {}}, "bills": []}
        if a is None:
            return Response(empty)
        bills = list(Bill.objects.filter(Q(order__waiter=request.user, order__shift_id=a.shift_id)
                                         | Q(booking__booked_by=request.user, booking__shift_id=a.shift_id),
                                         status=Bill.Status.PAID)
                     .select_related("order__table", "booking").prefetch_related("payments", "order__items")
                     .order_by("-paid_at", "-id"))
        rows, by_method, total = [], defaultdict(Decimal), Decimal(0)
        for b in bills:
            o = b.order
            methods = []
            for p in b.payments.all():          # refunds are stored negative, so they net off automatically
                by_method[p.method] += p.amount
                if p.kind == Payment.Kind.PAYMENT and p.method not in methods:
                    methods.append(p.method)
            total += b.total
            rows.append({"id": b.id, "number": b.number, "order": b.order_id, "booking": b.booking_id,
                         "order_number": o.number if o else b.booking.number, "source": o.source if o else "ROOM",
                         "table": o.table.name if o and o.table_id else None, "paid_at": b.paid_at, "total": b.total,
                         "methods": methods,
                         "items": sum(i.quantity for i in o.items.all() if i.status != "VOID") if o else len(b.lines)})
        return Response(clean({"summary": {"count": len(rows), "total": total, "by_method": dict(by_method)}, "bills": rows}))


class MyDrawerCashView(POSView):
    def post(self, request):
        d = request.data
        a = _require_assignment(request.user, Role.CASHIER)
        add_cash_movement(drawer=a, kind=d.get("kind"), amount=_money(d.get("amount")),
                          reason=d.get("reason", ""), user=request.user)
        return Response(clean(shift_summary(a)), status=201)


class MyShiftEndView(POSView):
    """Sign myself off. Cashiers send counted_cash and get expected/variance back."""

    def post(self, request):
        a = active_assignment(request.user)
        if not a:
            return Response({"error": "NotOnShift", "detail": "No open shift"}, status=403)
        counted = request.data.get("counted_cash")
        a = end_assignment(assignment=a, user=request.user,
                           counted_cash=None if counted in (None, "") else _money(counted, "counted_cash"))
        return Response(clean({"expected": a.expected_cash, "counted": a.counted_cash, "variance": a.variance}))


# ---------- menu / tables ----------

class MenuView(POSView):
    """?source=MENU -> kitchen items, ?source=BAR -> bar items, no filter -> everything."""

    def get(self, request):
        source = (request.query_params.get("source") or "").upper()
        station = SOURCE_STATION.get(source) or (request.query_params.get("station") or "").upper() or None
        data = []
        for cat in Category.objects.prefetch_related("items__modifiers"):
            items = []
            for mi in cat.items.all():
                if station and mi.station != station:
                    continue
                left = portions_available(mi)
                items.append({
                    "id": mi.id, "name": mi.name, "price": mi.price, "tax_rate": mi.tax_rate, "station": mi.station,
                    "available": mi.is_available and (left is None or left > 0),
                    "portions_left": left,
                    "modifiers": [{"id": m.id, "name": m.name, "price_delta": m.price_delta}
                                  for m in mi.modifiers.all()],
                })
            if items:
                data.append({"id": cat.id, "name": cat.name, "items": items})
        return Response(data)


class TableListView(POSView):
    def get(self, request):
        qs = Table.objects.annotate(open_orders=Count("orders", filter=Q(orders__status__in=Order.OCCUPYING)))
        return Response(list(qs.values("id", "name", "capacity", "status", "open_orders")))


# ---------- orders ----------

def _with_kots(data, kots):
    """Attach the tickets that were just printed (station + number) so the screen can say where the items went."""
    data["kots"] = [{"number": k.number, "station": k.station} for k in kots]
    return data


class OrderCreateView(POSView):
    def get(self, request):
        """Orders still in use. Waiters see their own; cashiers and managers see all."""
        qs = order_qs().filter(status__in=Order.OCCUPYING).order_by("created_at")
        if request.user.role == User.Role.WAITER and not request.user.is_manager:
            qs = qs.filter(waiter=request.user)
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"].upper())
        if request.query_params.get("table"):
            qs = qs.filter(table_id=request.query_params["table"])
        return Response(OrderSerializer(qs, many=True).data)

    def post(self, request):
        d = OrderCreateIn(data=request.data)
        d.is_valid(raise_exception=True)
        fields = dict(d.validated_data)
        lines, order_type = fields.pop("items"), fields.pop("order_type", None)
        kots = []
        with transaction.atomic():   # the order, its lines AND the kitchen / bar tickets are created together, or not at all
            order = open_order(waiter=request.user, order_type=order_type, **fields)
            if lines:
                add_items(order=order, lines=lines, user=request.user)
                kots = send_order(order=order, user=request.user)   # a new order goes straight to the kitchen / bar
        return Response(_with_kots(OrderSerializer(order_qs().get(pk=order.pk)).data, kots), status=201)


def _visible_order(request, pk):
    order = get_object_or_404(order_qs(), pk=pk)
    if request.user.role == User.Role.WAITER and not request.user.is_manager and order.waiter_id != request.user.pk:
        raise ManagerApprovalRequired("This order belongs to another waiter")
    return order


class OrderDetailView(POSView):
    def get(self, request, pk):
        return Response(OrderSerializer(_visible_order(request, pk)).data)


class AddItemView(POSView):
    def post(self, request, pk):
        d = AddItemIn(data=request.data)
        d.is_valid(raise_exception=True)
        v = d.validated_data
        add_item(order=get_object_or_404(Order, pk=pk), menu_item=v["menu_item"], user=request.user,
                 quantity=v["quantity"], modifiers=Modifier.objects.filter(pk__in=v["modifiers"]),
                 notes=v["notes"], course=v["course"], expected_version=v["version"])
        return Response(OrderSerializer(order_qs().get(pk=pk)).data, status=201)


class BulkAddItemsView(POSView):
    """POST {"items": [{"menu_item": id, "quantity": n}, ...], "version": n}: add several lines at once
    and send them to the kitchen / bar straight away."""

    def post(self, request, pk):
        d = BulkItemsIn(data=request.data)
        d.is_valid(raise_exception=True)
        order = get_object_or_404(Order, pk=pk)
        with transaction.atomic():   # items added to an order are sent in the same step: nothing is left unsent
            add_items(order=order, lines=d.validated_data["items"], user=request.user,
                      expected_version=d.validated_data["version"])
            kots = send_order(order=order, user=request.user)
        return Response(_with_kots(OrderSerializer(order_qs().get(pk=pk)).data, kots), status=201)


class ItemQuantityView(POSView):
    """PATCH {"quantity": n} on an unsent line."""

    def patch(self, request, pk, item_id):
        try:
            qty = int(request.data.get("quantity"))
        except (TypeError, ValueError):
            raise InvalidState("quantity must be a whole number")
        set_item_quantity(item=get_object_or_404(OrderItem, pk=item_id, order_id=pk), user=request.user,
                          quantity=qty, expected_version=_version(request.data))
        return Response(OrderSerializer(order_qs().get(pk=pk)).data)


class SendOrderView(POSView):
    def post(self, request, pk):
        kots = send_order(order=get_object_or_404(Order, pk=pk), user=request.user,
                          expected_version=_version(request.data))
        out = OrderSerializer(order_qs().get(pk=pk)).data
        out["kots"] = [{"number": k.number, "station": k.station} for k in kots]
        return Response(out)


class VoidItemView(POSView):
    def post(self, request, pk, item_id):
        d = request.data
        item = get_object_or_404(OrderItem, pk=item_id, order_id=pk)
        if item.status == OrderItem.Status.PENDING:
            remove_pending_item(item=item, user=request.user, expected_version=_version(d))
        else:
            void_item(item=item, user=request.user, reason=d.get("reason", ""),
                      wasted=bool(d.get("wasted", False)), manager_pin=d.get("manager_pin"),
                      expected_version=_version(d))
        return Response(OrderSerializer(order_qs().get(pk=pk)).data)


class CancelOrderView(POSView):
    def post(self, request, pk):
        cancel_order(order=get_object_or_404(Order, pk=pk), user=request.user,
                     expected_version=_version(request.data))
        return Response(OrderSerializer(order_qs().get(pk=pk)).data)


def _id_list(value, field):
    if not isinstance(value, list):
        raise InvalidState(f'"{field}" must be a list of ids')
    try:
        return [int(x) for x in value]
    except (TypeError, ValueError):
        raise InvalidState(f'"{field}" must be a list of ids')


class MergeOrdersView(POSView):
    """POST {"from": [order ids]} on the order that should survive: those orders are folded into it."""

    def post(self, request, pk):
        ids = _id_list(request.data.get("from"), "from")
        sources = list(Order.objects.filter(pk__in=ids))
        if len(sources) != len(set(ids)):
            raise InvalidState("One of those orders doesn't exist")
        merge_orders(target=get_object_or_404(Order, pk=pk), sources=sources, user=request.user,
                     expected_version=_version(request.data))
        return Response(OrderSerializer(order_qs().get(pk=pk)).data)


class SplitOrderView(POSView):
    """POST {"items": [item ids]}: move those lines to a new order so they can be billed separately."""

    def post(self, request, pk):
        new = split_order(source=get_object_or_404(Order, pk=pk), item_ids=_id_list(request.data.get("items"), "items"),
                          user=request.user, expected_version=_version(request.data))
        return Response({"order": OrderSerializer(order_qs().get(pk=pk)).data,
                         "new_order": OrderSerializer(order_qs().get(pk=new.pk)).data}, status=201)


# ---------- billing ----------

class BillOrderView(POSView):
    def post(self, request, pk):
        d = request.data
        pct = _num(d["discount_percent"], "discount_percent") if d.get("discount_percent") else None
        amt = _money(d["discount_amount"], "discount_amount") if d.get("discount_amount") else None
        bill = generate_bill(order=get_object_or_404(Order, pk=pk), user=request.user,
                             discount_percent=pct, discount_amount=amt,
                             discount_reason=d.get("discount_reason", ""), manager_pin=d.get("manager_pin"))
        return Response(clean(bill_data(bill)), status=201)


def _visible_bill(request, pk):
    bill = get_object_or_404(Bill.objects.select_related("order__table", "order__waiter", "booking__booked_by"), pk=pk)
    owner = bill.order.waiter_id if bill.order_id else bill.booking.booked_by_id
    if request.user.role == User.Role.WAITER and not request.user.is_manager and owner != request.user.pk:
        raise ManagerApprovalRequired("This bill belongs to another waiter's order")
    return bill


class BillDetailView(POSView):
    def get(self, request, pk):
        return Response(clean(bill_data(_visible_bill(request, pk))))


class BillReceiptView(POSView):
    """Printable receipt (HTML). Every print is logged so reprints can be reviewed.
    Cashiers and managers can print any bill. A waiter can print the guest bill for their own order while it is
    still unpaid (the page says AMOUNT DUE); the paid receipt is the cashier's."""

    def get(self, request, pk):
        u = request.user
        if u.is_manager or active_assignment(u, Role.CASHIER):
            bill = get_object_or_404(Bill.objects.select_related("order__table", "order__waiter", "booking__booked_by"), pk=pk)
        else:
            _require_assignment(u, Role.WAITER)
            bill = _visible_bill(request, pk)   # also refuses another waiter's bill
            if bill.status != Bill.Status.OPEN:
                raise ManagerApprovalRequired("Receipts for paid bills are printed by the cashier. Please send the guest to the cashier.")
        audit(request.user, "bill_printed", bill, number=bill.number)
        return HttpResponse(render_receipt(bill), content_type="text/html; charset=utf-8")


class BillListView(POSView):
    """The cashier's Paid tab: bills paid in the shifts open right now (newest first), with whether
    their receipt has been printed since payment, so none is forgotten and reprints are visible."""

    def get(self, request):
        _require_cashier(request.user, "The paid-bills list")
        bills = list(Bill.objects.filter(status=Bill.Status.PAID, payments__drawer__shift__status=Shift.Status.OPEN)
                     .select_related("order__table", "order__waiter", "booking__booked_by").distinct().order_by("-paid_at", "-id")[:200])
        prints = {}
        for log in AuditLog.objects.filter(action="bill_printed", ref_type="Bill",
                                           ref_id__in=[str(b.pk) for b in bills]).select_related("user").order_by("created_at"):
            prints.setdefault(log.ref_id, []).append(log)
        rows = []
        for b in bills:
            after = [p for p in prints.get(str(b.pk), []) if b.paid_at and p.created_at >= b.paid_at]   # prints before payment don't count
            last = after[-1] if after else None
            o, bk = b.order, b.booking
            who = o.waiter if o else bk.booked_by
            rows.append({"id": b.id, "number": b.number, "order": b.order_id, "order_number": o.number if o else bk.number,
                         "booking": b.booking_id, "source": o.source if o else "ROOM",
                         "table": o.table.name if o and o.table_id else None, "waiter": who.id,
                         "waiter_name": who.get_full_name() or who.username, "total": b.total, "paid_at": b.paid_at,
                         "printed": len(after), "last_printed_at": last.created_at if last else None,
                         "last_printed_by": (last.user.get_full_name() or last.user.username) if last and last.user else None})
        return Response(clean(rows))


# ---------- full-page order / invoice view ----------

def business_info():
    """Who the invoice is 'From'. All optional settings; the receipt uses the same ones."""
    g = lambda k, d="": getattr(settings, k, d)
    return {"name": g("POS_BUSINESS_NAME", "Nova PoS"), "address": g("POS_BUSINESS_ADDRESS"),
            "phone": g("POS_BUSINESS_PHONE"), "email": g("POS_BUSINESS_EMAIL"), "tax_id": g("POS_BUSINESS_TAX_ID")}


class OrderPageView(POSView):
    """Everything the order page needs in one call: the order, its bill (if any) with the payments received,
    the lines with VAT, the totals, and who the invoice is from. Works for open, billed and paid orders.
    A waiter only gets their own orders (same rule as the order detail)."""

    def get(self, request, pk):
        order = _visible_order(request, pk)
        bills = list(order.bills.all())
        bill = next((b for b in bills if b.status in (Bill.Status.OPEN, Bill.Status.PAID)), None) \
            or next((b for b in sorted(bills, key=lambda b: b.created_at, reverse=True) if b.status == Bill.Status.REFUNDED), None)
        if bill:
            b = bill_data(bill)
            pays = bill.payments.select_related("user").order_by("created_at", "id")
            payments = [{"id": p.id, "kind": p.kind, "method": p.method, "amount": p.amount, "reference": p.reference,
                         "tendered": p.tendered, "change_due": p.change_due, "created_at": p.created_at,
                         "by": p.user.get_full_name() or p.user.username} for p in pays]
            totals = {"subtotal": bill.subtotal, "tax": bill.tax, "discount": bill.discount_amount,
                      "service_charge": bill.service_charge, "total": bill.total, "paid": bill.amount_paid, "balance": bill.balance}
            lines = bill.lines
            meta = {"id": bill.id, "number": bill.number, "status": bill.status, "terminal": bill.terminal,
                    "created_at": bill.created_at, "paid_at": bill.paid_at, "discount_reason": bill.discount_reason}
        else:
            a = compute_bill_amounts(order)   # a preview: nothing is saved until the order is billed
            lines = a["lines"]
            totals = {"subtotal": a["subtotal"], "tax": a["tax"], "discount": a["discount"],
                      "service_charge": a["service_charge"], "total": a["total"], "paid": D(0), "balance": a["total"]}
            payments, meta = [], None
        voided = [{"name": i.name, "quantity": i.quantity, "reason": i.void_reason}
                  for i in order.items.all() if i.status == "VOID"]
        return Response(clean({"order": OrderSerializer(order).data, "bill": meta, "lines": lines, "totals": totals,
                               "payments": payments, "voided": voided, "business": business_info(),
                               "prices_include_tax": settings.PRICES_INCLUDE_TAX}))


class CancelBillView(POSView):
    def post(self, request, pk):
        d = request.data
        bill = cancel_bill(bill=get_object_or_404(Bill, pk=pk), user=request.user,
                           reason=d.get("reason", ""), manager_pin=d.get("manager_pin"))
        return Response(clean(bill_data(bill)))


# ---------- payment setup + M-Pesa STK push (Safaricom Daraja) ----------
# Optional settings.py entries (anything missing simply switches that feature off):
#   MPESA_ENV             "sandbox" (default) or "production"
#   MPESA_CONSUMER_KEY / MPESA_CONSUMER_SECRET / MPESA_PASSKEY   from the Daraja portal
#   MPESA_SHORTCODE       the store / head-office number the passkey belongs to
#   MPESA_TILL_NUMBER     the Buy Goods till customers pay (defaults to MPESA_SHORTCODE)
#   MPESA_TRANSACTION_TYPE "CustomerBuyGoodsOnline" (default, till) or "CustomerPayBillOnline"
#   MPESA_CALLBACK_URL    public https URL of /api/payments/mpesa/callback/ (Safaricom must be able to reach it)
#   MPESA_CALLBACK_SECRET long random string; appended to the callback URL so only Safaricom's request is accepted
#   POS_BANK_NAME / POS_BANK_ACCOUNT_NAME / POS_BANK_ACCOUNT_NUMBER   shown for bank transfers
# The STK state lives in Django's cache, so use a shared cache (Redis / Memcached / database) when running several workers.

STK_TTL = 3600
STK_MESSAGES = {1: "The customer's M-Pesa balance is too low", 1032: "The customer cancelled the request",
                1037: "The request timed out on the customer's phone", 2001: "The customer entered a wrong M-Pesa PIN"}


def _mpesa_cfg():
    g = lambda k, d="": getattr(settings, k, d) or d
    return {"env": g("MPESA_ENV", "sandbox"), "key": g("MPESA_CONSUMER_KEY"), "secret": g("MPESA_CONSUMER_SECRET"),
            "passkey": g("MPESA_PASSKEY"), "shortcode": str(g("MPESA_SHORTCODE")), "till": str(g("MPESA_TILL_NUMBER")),
            "tx_type": g("MPESA_TRANSACTION_TYPE", "CustomerBuyGoodsOnline"), "callback_url": g("MPESA_CALLBACK_URL"),
            "callback_secret": g("MPESA_CALLBACK_SECRET")}


def _mpesa_ready(c):
    return all(c[k] for k in ("key", "secret", "passkey", "shortcode", "callback_url", "callback_secret"))


def _stk_key(checkout_id):
    return f"pos:stk:{checkout_id}"


def _normalise_phone(raw):
    s = re.sub(r"[\s\-()]", "", str(raw or ""))
    m = re.fullmatch(r"(?:\+?254|0)?([17]\d{8})", s)
    if not m:
        raise InvalidState("Enter a valid Safaricom number, e.g. 0712 345 678")
    return "254" + m.group(1)


def _daraja(c, path, payload=None, headers=None):
    base = "https://api.safaricom.co.ke" if c["env"] == "production" else "https://sandbox.safaricom.co.ke"
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=None if payload is None else json.dumps(payload).encode(),
                                 headers=h, method="GET" if payload is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("errorMessage")
        except Exception:
            detail = None
        raise InvalidState(f"M-Pesa: {detail or 'the request was rejected'}")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise InvalidState("Could not reach M-Pesa. Try again, or use the till number")


def _mpesa_token(c):
    tok = cache.get("pos:mpesa:token")
    if tok:
        return tok
    raw = base64.b64encode(f"{c['key']}:{c['secret']}".encode()).decode()
    d = _daraja(c, "/oauth/v1/generate?grant_type=client_credentials", headers={"Authorization": "Basic " + raw})
    tok = d.get("access_token")
    if not tok:
        raise InvalidState("M-Pesa did not return an access token. Check the consumer key and secret")
    cache.set("pos:mpesa:token", tok, max(60, int(d.get("expires_in") or 3599) - 60))
    return tok


class PaymentConfigView(POSView):
    """What the payment dialog needs to show: the till number, whether STK push is set up, and the bank details."""

    def get(self, request):
        c = _mpesa_cfg()
        return Response({"mpesa": {"till": c["till"] or c["shortcode"], "stk": _mpesa_ready(c)},
                         "bank": {"name": getattr(settings, "POS_BANK_NAME", ""),
                                  "account_name": getattr(settings, "POS_BANK_ACCOUNT_NAME", ""),
                                  "account_number": getattr(settings, "POS_BANK_ACCOUNT_NUMBER", "")}})


class MpesaStkView(POSView):
    """Send an STK push (the PIN prompt) to the customer's phone for a bill's balance."""

    def post(self, request):
        d = request.data
        bill = get_object_or_404(Bill.objects.select_related("order", "booking"), pk=d.get("bill"))
        drawer = _require_drawer(request.user)
        _check_bill_access(request.user, drawer, bill.document)
        if bill.status != Bill.Status.OPEN:
            raise InvalidState(f"Bill {bill.number} is {bill.get_status_display().lower()}")
        amount = _money(d.get("amount"))
        if amount <= 0 or amount > bill.balance:
            raise InvalidState(f"Amount must be between 0.01 and the balance of {bill.balance}")
        if amount != amount.to_integral_value():
            raise InvalidState("STK push needs a whole-shilling amount. Use the till number for cents")
        phone = _normalise_phone(d.get("phone"))
        c = _mpesa_cfg()
        if not _mpesa_ready(c):
            raise InvalidState("STK push is not set up yet. Use the till number instead")
        ts = datetime.now(dt_tz(timedelta(hours=3))).strftime("%Y%m%d%H%M%S")   # Daraja expects Nairobi time
        password = base64.b64encode(f"{c['shortcode']}{c['passkey']}{ts}".encode()).decode()
        resp = _daraja(c, "/mpesa/stkpush/v1/processrequest", {
            "BusinessShortCode": c["shortcode"], "Password": password, "Timestamp": ts, "TransactionType": c["tx_type"],
            "Amount": int(amount), "PartyA": phone, "PartyB": c["till"] or c["shortcode"], "PhoneNumber": phone,
            "CallBackURL": f"{c['callback_url']}?k={c['callback_secret']}",
            "AccountReference": str(bill.number)[:12], "TransactionDesc": f"Bill {bill.number}"[:13],
        }, headers={"Authorization": "Bearer " + _mpesa_token(c)})
        if str(resp.get("ResponseCode")) != "0" or not resp.get("CheckoutRequestID"):
            raise InvalidState(resp.get("ResponseDescription") or resp.get("errorMessage") or "M-Pesa rejected the request")
        cid = resp["CheckoutRequestID"]
        cache.set(_stk_key(cid), {"status": "PENDING", "bill": bill.id, "amount": str(amount), "phone": phone,
                                  "user": request.user.id}, STK_TTL)
        return Response({"checkout_id": cid, "phone": phone}, status=201)


class MpesaStkStatusView(POSView):
    """Polled by the payment dialog until the customer has paid, refused or the request has expired."""

    def get(self, request, checkout_id):
        e = cache.get(_stk_key(checkout_id))
        if not e or e.get("user") != request.user.id:
            return Response({"status": "UNKNOWN"})
        return Response({"status": e["status"], "receipt": e.get("receipt", ""), "message": e.get("message", "")})


class MpesaCallbackView(APIView):
    """Safaricom posts the STK result here. No login is possible, so the URL carries MPESA_CALLBACK_SECRET and
    only checkout ids this POS created are accepted."""
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        secret = _mpesa_cfg()["callback_secret"]
        if not secret or not hmac.compare_digest(str(request.query_params.get("k", "")), secret):
            return Response(status=403)
        cb = ((request.data or {}).get("Body") or {}).get("stkCallback") or {}
        cid = cb.get("CheckoutRequestID")
        e = cache.get(_stk_key(cid)) if cid else None
        if e and e.get("status") == "PENDING":
            try:
                code = int(cb.get("ResultCode", -1))
            except (TypeError, ValueError):
                code = -1
            meta = {i.get("Name"): i.get("Value") for i in (cb.get("CallbackMetadata") or {}).get("Item", []) if isinstance(i, dict)}
            if code == 0 and meta.get("MpesaReceiptNumber"):
                e.update(status="SUCCESS", receipt=str(meta["MpesaReceiptNumber"]))
            else:
                e.update(status="FAILED", message=STK_MESSAGES.get(code) or cb.get("ResultDesc") or "The payment was not completed")
            cache.set(_stk_key(cid), e, STK_TTL)
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})


class PayView(POSView):
    def post(self, request, pk):
        d = request.data
        tendered = d.get("tendered")
        method, reference, key = d.get("method"), d.get("reference", ""), d.get("idempotency_key")
        entry, stk_id = None, d.get("stk_checkout_id") if method == Payment.Method.MOBILE_MONEY else None
        if stk_id:   # an STK push: trust only what Safaricom told the callback, never what the browser says
            entry = cache.get(_stk_key(stk_id))
            retry = bool(entry) and entry.get("status") == "USED" and key and entry.get("used_key") == key
            if not entry or entry.get("user") != request.user.id or entry.get("bill") != pk \
                    or (entry.get("status") != "SUCCESS" and not retry):
                raise InvalidState("This M-Pesa request has not been confirmed yet")
            if D(entry["amount"]) != _money(d.get("amount")):
                raise InvalidState("The amount does not match the M-Pesa request")
            reference = entry["receipt"]
        p = take_payment(bill=get_object_or_404(Bill, pk=pk), user=request.user, method=method,
                         amount=_money(d.get("amount")), reference=reference,
                         tendered=None if tendered in (None, "") else _money(tendered, "tendered"),
                         idempotency_key=key)
        if entry and entry.get("status") == "SUCCESS":
            entry.update(status="USED", used_key=key)
            cache.set(_stk_key(stk_id), entry, STK_TTL)
        bill = Bill.objects.select_related("order", "booking").get(pk=pk)
        return Response(clean({**bill_data(bill), "change_due": p.change_due}), status=201)


class RefundView(POSView):
    def post(self, request, pk):
        d = request.data
        amount = d.get("amount")
        payment = get_object_or_404(Payment, pk=pk)
        refund_payment(payment=payment, user=request.user, reason=d.get("reason", ""),
                       amount=None if amount in (None, "") else _money(amount),
                       manager_pin=d.get("manager_pin"))
        return Response(clean(bill_data(Bill.objects.select_related("order", "booking").get(pk=payment.bill_id))))


class ZPreviewView(POSView):
    permission_classes = [IsManager]

    def get(self, request):
        return Response(clean(preview_day()))


class ZCloseView(POSView):
    permission_classes = [IsManager]

    def post(self, request):
        r = close_day(user=request.user)
        return Response(clean({"number": r.number, **r.data}), status=201)


# =============================================================================
# Inventory & purchasing services (no HTTP endpoints yet; used by orders and the admin/back office)
# =============================================================================

COSTED_INBOUND = {MovementType.OPENING, MovementType.PURCHASE}


@transaction.atomic
def record_movement(*, item, location, quantity, movement_type, unit_cost=None,
                    user=None, ref_type="", ref_id="", reason=""):
    """Write one ledger row and update the cached balance (and avg cost) atomically.
    Nothing else may write StockBalance."""
    quantity = D(quantity)
    if quantity == 0:
        raise ValueError("Movement quantity cannot be zero")

    # Lock order: item row, then balance row (avoids deadlocks between terminals).
    item = StockItem.objects.select_for_update().get(pk=item.pk)
    StockBalance.objects.get_or_create(item=item, location=location)
    balance = StockBalance.objects.select_for_update().get(item=item, location=location)

    new_qty = balance.quantity + quantity
    if quantity < 0 and new_qty < 0 and not settings.ALLOW_NEGATIVE_STOCK:
        raise InsufficientStock(f"Not enough {item.name} in {location.name} "
                                f"(have {balance.quantity}, need {-quantity})")

    if quantity > 0 and movement_type in COSTED_INBOUND and unit_cost is not None:
        unit_cost = D(unit_cost)
        total = item.balances.aggregate(t=Sum("quantity"))["t"] or D(0)
        if total <= 0:
            item.avg_cost = unit_cost
        else:
            item.avg_cost = (total * item.avg_cost + quantity * unit_cost) / (total + quantity)
        item.save(update_fields=["avg_cost", "updated_at"])
    elif unit_cost is None:
        unit_cost = item.avg_cost

    balance.quantity = new_qty
    balance.save(update_fields=["quantity"])
    return StockMovement.objects.create(
        item=item, location=location, quantity=quantity, unit_cost=unit_cost,
        movement_type=movement_type, ref_type=ref_type, ref_id=str(ref_id),
        user=user, reason=reason)


@transaction.atomic
def transfer_stock(*, item, from_location, to_location, quantity, user=None):
    """Two movements sharing one transfer reference. Average cost is unchanged."""
    quantity = D(quantity)
    if quantity <= 0 or from_location == to_location:
        raise ValueError("Transfer needs a positive quantity and two different locations")
    ref = DocumentSequence.next("transfer", "TRF-")
    out = record_movement(item=item, location=from_location, quantity=-quantity,
                          movement_type=MovementType.TRANSFER, user=user, ref_type="transfer", ref_id=ref)
    record_movement(item=item, location=to_location, quantity=quantity,
                    movement_type=MovementType.TRANSFER, unit_cost=out.unit_cost,
                    user=user, ref_type="transfer", ref_id=ref)
    return ref


def record_waste(*, item, location, quantity, reason, user=None):
    if not reason:
        raise ValueError("Waste needs a reason")
    return record_movement(item=item, location=location, quantity=-D(quantity),
                           movement_type=MovementType.WASTE, user=user, reason=reason)


@transaction.atomic
def start_stock_take(*, location, user, items=None, is_opening=False, blind=True):
    take = StockTake.objects.create(
        number=DocumentSequence.next("stocktake", "ST-"), location=location,
        is_opening=is_opening, blind=blind, created_by=user)
    if items is None:
        items = StockItem.objects.filter(is_active=True)
    for item in items:
        expected = D(0) if is_opening else item.on_hand(location)
        StockTakeLine.objects.create(take=take, item=item, expected=expected)
    return take


def record_count(*, take, item, counted, unit_cost=None):
    if take.status != StockTake.Status.DRAFT:
        raise InvalidState("Stock take is already approved")
    line = take.lines.get(item=item)
    line.counted = D(counted)
    if unit_cost is not None:
        line.unit_cost = D(unit_cost)
    line.save()
    return line


@transaction.atomic
def approve_stock_take(*, take, approver):
    """Apply variances as ADJUSTMENT (or OPENING) movements.

    The adjustment is counted - expected_at_start, applied as a delta, so sales
    that happened while counting are preserved.
    """
    if not approver.is_manager:
        raise ManagerApprovalRequired("Only a manager can approve a stock take")
    take = StockTake.objects.select_for_update().get(pk=take.pk)
    if take.status != StockTake.Status.DRAFT:
        raise InvalidState("Stock take is already approved")

    mtype = MovementType.OPENING if take.is_opening else MovementType.ADJUSTMENT
    applied = 0
    for line in take.lines.select_related("item"):
        if line.counted is None or line.variance == 0:
            continue
        cost = line.unit_cost
        if take.is_opening and cost is None:
            cost = line.item.avg_cost
        record_movement(item=line.item, location=take.location, quantity=line.variance,
                        movement_type=mtype, unit_cost=cost if take.is_opening else None,
                        user=approver, ref_type="stock_take", ref_id=take.number,
                        reason="Opening count" if take.is_opening else "Stock-take variance")
        applied += 1
    take.status = StockTake.Status.APPROVED
    take.approved_by = approver
    take.save(update_fields=["status", "approved_by", "updated_at"])
    audit(approver, "stock_take_approved", take, lines_adjusted=applied)
    return take


@transaction.atomic
def create_purchase_order(*, supplier, user, lines, notes=""):
    """lines: iterable of (stock_item, qty_in_purchase_units, cost_per_purchase_unit)."""
    po = PurchaseOrder.objects.create(number=DocumentSequence.next("po", "PO-"),
                                      supplier=supplier, created_by=user, notes=notes)
    for item, qty, cost in lines:
        PurchaseOrderLine.objects.create(po=po, item=item, qty_ordered=D(qty), unit_cost=D(cost))
    return po


def send_purchase_order(po):
    if po.status != PurchaseOrder.Status.DRAFT:
        raise InvalidState("Only draft POs can be sent")
    po.status = PurchaseOrder.Status.SENT
    po.save(update_fields=["status", "updated_at"])
    return po


@transaction.atomic
def receive_goods(*, po, location, user, lines, invoice_no=""):
    """lines: iterable of dicts {po_line, qty, unit_cost (optional)}, qty in purchase units.

    Converts to stock units, writes PURCHASE movements (which update the weighted
    average cost), and moves the PO to PARTIAL / RECEIVED.
    """
    po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
    if po.status in (PurchaseOrder.Status.DRAFT, PurchaseOrder.Status.CANCELLED, PurchaseOrder.Status.RECEIVED):
        raise InvalidState(f"Cannot receive goods against a {po.get_status_display().lower()} PO")

    receipt = GoodsReceipt.objects.create(number=DocumentSequence.next("grn", "GRN-"), po=po,
                                          location=location, invoice_no=invoice_no, received_by=user)
    for row in lines:
        po_line = PurchaseOrderLine.objects.select_for_update().get(pk=row["po_line"].pk, po=po)
        qty = D(row["qty"])
        if qty <= 0:
            continue
        cost = D(row.get("unit_cost", po_line.unit_cost))
        item = po_line.item
        factor = item.stock_units_per_purchase_unit
        record_movement(item=item, location=location, quantity=qty * factor,
                        unit_cost=cost / factor, movement_type=MovementType.PURCHASE, user=user,
                        ref_type="goods_receipt", ref_id=receipt.number, reason=f"Invoice {invoice_no}".strip())
        GoodsReceiptLine.objects.create(receipt=receipt, po_line=po_line, qty=qty, unit_cost=cost)
        po_line.qty_received += qty
        po_line.save(update_fields=["qty_received"])

    done = all(l.qty_received >= l.qty_ordered for l in po.lines.all())
    po.status = PurchaseOrder.Status.RECEIVED if done else PurchaseOrder.Status.PARTIAL
    po.save(update_fields=["status", "updated_at"])
    audit(user, "goods_received", receipt, po=po.number, invoice=invoice_no)
    return receipt


# =============================================================================
# Rooms: availability, booking, billing, check-in / check-out
# =============================================================================
# A booking is billed and paid through the same Bill / Payment / drawer code as an order: its Bill has
# booking set and order empty. Optional settings: ROOM_VAT_PERCENT (default 16), ROOM_LEVY_PERCENT (default 2,
# the catering levy). Room rates are per night and EXCLUDE both; the bill adds them on top.

ROOM_NOTES = {Room.Status.CLEANING: "The room is still under cleaning.", Room.Status.MAINTENANCE: "The room is out of service."}
MAX_STAY_NIGHTS = 90


def _room_vat():
    return D(str(getattr(settings, "ROOM_VAT_PERCENT", 16)))


def _room_levy():
    return D(str(getattr(settings, "ROOM_LEVY_PERCENT", 2)))


def _date(value, field):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise InvalidState(f"{field} must be a date like 2026-10-08")


def room_conflicts(room_ids, check_in, check_out, exclude=None):
    """Active bookings that already hold any of these rooms on any night of [check_in, check_out)."""
    qs = RoomBookingLine.objects.filter(room_id__in=room_ids, booking__status__in=RoomBooking.ACTIVE,
                                        booking__check_in__lt=check_out, booking__check_out__gt=check_in
                                        ).select_related("room", "booking")
    return qs.exclude(booking=exclude) if exclude else qs


def room_amounts(booking):
    """What the stay costs: nights x nightly rate per room, then VAT on each line and the levy on the net."""
    nights, vat = booking.nights, _room_vat()
    lines, net_sum, tax_sum = [], D(0), D(0)
    for l in sorted(booking.lines.all(), key=lambda x: x.id):
        net = q(l.unit_price * nights)
        tax = q(net * vat / 100)
        net_sum += net
        tax_sum += tax
        lines.append({"name": f"{l.room.name} ({l.get_meal_plan_display()})", "category": "Accommodation", "quantity": nights,
                      "unit_price": str(l.unit_price), "line_total": str(net), "modifiers": [], "tax_rate": str(vat),
                      "net": str(net), "tax": str(tax), "room": l.room.name, "meal_plan": l.meal_plan})
    levy = q(net_sum * _room_levy() / 100)
    return {"lines": lines, "subtotal": net_sum, "tax": tax_sum, "levy": levy, "total": net_sum + tax_sum + levy}


def _check_booking_access(user, booking):
    """Waiters work on their own bookings; cashiers and managers on any."""
    a = active_assignment(user)
    if a is not None and a.role == Role.WAITER and not user.is_manager and booking.booked_by_id != user.pk:
        raise ManagerApprovalRequired(f"Booking {booking.number} belongs to another waiter")


def _booking_actor(user):
    if not user.is_manager and active_assignment(user) is None:
        raise NotOnShift("You are not on an open shift. Ask a manager to add you.")


def _live_bill(booking):
    return next((b for b in booking.bills.all() if b.status in (Bill.Status.OPEN, Bill.Status.PAID)), None)


@transaction.atomic
def generate_booking_bill(*, booking, user):
    """One bill per booking, numbered from the user's terminal (or the shared WTR series), paid into their drawer."""
    drawer = _require_drawer(user)
    booking = RoomBooking.objects.select_for_update().get(pk=booking.pk)
    _check_bill_access(user, drawer, booking)
    if booking.status not in RoomBooking.ACTIVE:
        raise InvalidState(f"Booking {booking.number} is {booking.get_status_display().lower()}")
    if booking.bills.filter(status__in=[Bill.Status.OPEN, Bill.Status.PAID]).exists():
        raise InvalidState("This booking already has a bill")
    a = room_amounts(booking)
    terminal = drawer.terminal or "WTR"
    return Bill.objects.create(
        number=DocumentSequence.next(f"bill-{terminal}", f"{terminal}-B"), booking=booking, terminal=terminal,
        lines=a["lines"], subtotal=a["subtotal"], tax=a["tax"], levy=a["levy"], total=a["total"], created_by=user)


@transaction.atomic
def create_booking(*, user, guest_name, phone, check_in, check_out, rooms, occupants=1, narrative="", payment=None):
    """Book one or more rooms for a guest. `rooms` = [{"room": id, "meal_plan": "BNB"}]. With `payment`
    ({"method", "amount", "reference", "tendered", "idempotency_key"}) the bill is created and the money taken in
    the same step, so a failed payment leaves no booking behind."""
    drawer = _require_drawer(user)
    key = (payment or {}).get("idempotency_key")
    if key and Payment.objects.filter(idempotency_key=key).exists():   # a retry after a lost reply: never book (and charge) twice
        raise InvalidState("This booking was already submitted. Check the Bookings list")
    guest_name, phone = (guest_name or "").strip(), (phone or "").strip()
    if not guest_name:
        raise InvalidState("Enter the guest's full name")
    if not phone:
        raise InvalidState("Enter the guest's phone number")
    if check_in < timezone.localdate():
        raise InvalidState("Check-in cannot be in the past")
    if check_out <= check_in:
        raise InvalidState("Check-out must be after check-in")
    if (check_out - check_in).days > MAX_STAY_NIGHTS:
        raise InvalidState(f"A stay can be at most {MAX_STAY_NIGHTS} nights")
    if not rooms:
        raise InvalidState("Add at least one room")
    ids = [r["room"] for r in rooms]
    if len(set(ids)) != len(ids):
        raise InvalidState("Each room can only be added once")
    found = {r.pk: r for r in Room.objects.select_for_update().filter(pk__in=ids, is_active=True)}   # locks the rooms: two clerks cannot take the same one
    picked = []
    for r in rooms:
        room = found.get(r["room"])
        if room is None:
            raise InvalidState("That room does not exist")
        if room.status == Room.Status.MAINTENANCE:
            raise RoomUnavailable(f"{room.name} is out of service")
        if room.rate(r["meal_plan"]) <= 0:
            raise InvalidState(f"{room.name} is not offered on {MealPlan(r['meal_plan']).label.lower()}")
        picked.append((room, r["meal_plan"]))
    clash = room_conflicts(ids, check_in, check_out).first()
    if clash:
        b = clash.booking
        raise RoomUnavailable(f"{clash.room.name} is already booked {b.check_in:%d %b} to {b.check_out:%d %b} ({b.number})")

    booking = RoomBooking.objects.create(number=DocumentSequence.next("booking", "BK-"), guest_name=guest_name, phone=phone,
                                         occupants=occupants, narrative=narrative or "", check_in=check_in, check_out=check_out,
                                         shift=drawer.shift, booked_by=user)
    RoomBookingLine.objects.bulk_create([RoomBookingLine(booking=booking, room=room, meal_plan=plan, unit_price=room.rate(plan))
                                         for room, plan in picked])
    audit(user, "booking_created", booking, rooms=[r.name for r, _ in picked], nights=booking.nights, guest=guest_name)
    if payment:
        bill = generate_booking_bill(booking=booking, user=user)
        take_payment(bill=bill, user=user, **payment)
    return booking


@transaction.atomic
def cancel_booking(*, booking, user, reason):
    _booking_actor(user)
    booking = RoomBooking.objects.select_for_update().get(pk=booking.pk)
    _check_booking_access(user, booking)
    if booking.status == RoomBooking.Status.CHECKED_IN:
        raise InvalidState("The guest is already in the room. Check them out instead")
    if booking.status != RoomBooking.Status.RESERVED:
        raise InvalidState(f"Booking {booking.number} is {booking.get_status_display().lower()}")
    if not (reason or "").strip():
        raise InvalidState("A reason is required")
    live = booking.bills.filter(status__in=[Bill.Status.OPEN, Bill.Status.PAID]).first()
    if live:
        raise InvalidState(f"Refund the payments on bill {live.number} first" if live.amount_paid else f"Cancel bill {live.number} first")
    booking.status, booking.cancel_reason = RoomBooking.Status.CANCELLED, reason.strip()[:200]
    booking.save(update_fields=["status", "cancel_reason", "updated_at"])
    audit(user, "booking_cancelled", booking, reason=booking.cancel_reason)
    return booking


@transaction.atomic
def check_in_booking(*, booking, user):
    _booking_actor(user)
    booking = RoomBooking.objects.select_for_update().get(pk=booking.pk)
    _check_booking_access(user, booking)
    if booking.status != RoomBooking.Status.RESERVED:
        raise InvalidState(f"Booking {booking.number} is {booking.get_status_display().lower()}")
    if timezone.localdate() < booking.check_in:
        raise InvalidState(f"Check-in is on {booking.check_in:%d %b %Y}")
    not_ready = [l.room.name for l in booking.lines.select_related("room") if l.room.status != Room.Status.AVAILABLE]
    if not_ready:
        raise RoomUnavailable(f"{', '.join(not_ready)} not ready yet (cleaning or out of service)")
    booking.status, booking.checked_in_at = RoomBooking.Status.CHECKED_IN, timezone.now()
    booking.save(update_fields=["status", "checked_in_at", "updated_at"])
    audit(user, "guest_checked_in", booking)
    return booking


@transaction.atomic
def check_out_booking(*, booking, user):
    """Needs the bill settled in full. The rooms then go to housekeeping (Being cleaned)."""
    _booking_actor(user)
    booking = RoomBooking.objects.select_for_update().get(pk=booking.pk)
    _check_booking_access(user, booking)
    if booking.status != RoomBooking.Status.CHECKED_IN:
        raise InvalidState(f"Booking {booking.number} is {booking.get_status_display().lower()}")
    bill = booking.bills.filter(status__in=[Bill.Status.OPEN, Bill.Status.PAID]).first()
    if bill is None or bill.status != Bill.Status.PAID:
        raise InvalidState("Settle the bill in full before check-out" + (f" (balance {bill.balance})" if bill else ""))
    booking.status, booking.checked_out_at = RoomBooking.Status.CHECKED_OUT, timezone.now()
    booking.save(update_fields=["status", "checked_out_at", "updated_at"])
    Room.objects.filter(booking_lines__booking=booking).update(status=Room.Status.CLEANING)
    audit(user, "guest_checked_out", booking)
    return booking


def set_room_status(*, room, user, status):
    _booking_actor(user)
    if status not in Room.Status.values:
        raise InvalidState(f"Unknown room status {status}")
    if status == Room.Status.MAINTENANCE:
        _require_manager(user, "Taking a room out of service")
    room.status = status
    room.save(update_fields=["status", "updated_at"])
    audit(user, "room_status", room, status=status)
    return room


# ---------- serializers ----------

def booking_data(b):
    """One booking for the list and the booking page (use _booking_qs so lines, rooms and bills are prefetched)."""
    live = _live_bill(b)
    a = room_amounts(b)
    total = live.total if live else a["total"]
    paid = live.amount_paid if live else D(0)
    state = ("UNBILLED" if not live else "PAID" if live.status == Bill.Status.PAID else "PART" if paid > 0 else "UNPAID")
    if not live and any(x.status == Bill.Status.REFUNDED for x in b.bills.all()):
        state = "REFUNDED"
    return {"id": b.id, "number": b.number, "guest_name": b.guest_name, "phone": b.phone, "occupants": b.occupants,
            "narrative": b.narrative, "check_in": b.check_in, "check_out": b.check_out, "nights": b.nights,
            "status": b.status, "cancel_reason": b.cancel_reason, "created_at": b.created_at,
            "checked_in_at": b.checked_in_at, "checked_out_at": b.checked_out_at,
            "waiter": b.booked_by_id, "waiter_name": b.booked_by.get_full_name() or b.booked_by.username,
            "rooms": [l.room.name for l in sorted(b.lines.all(), key=lambda x: x.id)],
            "lines": [{"room": l.room_id, "name": l.room.name, "meal_plan": l.meal_plan, "unit_price": l.unit_price}
                      for l in sorted(b.lines.all(), key=lambda x: x.id)],
            "total": total, "paid": paid, "balance": total - paid, "pay_state": state, "bill_id": live.id if live else None}


def _booking_qs():
    return RoomBooking.objects.select_related("booked_by").prefetch_related("lines__room", "bills")


def booking_page(b):
    """Everything the booking page needs in one call (same shape as the order page)."""
    b = _booking_qs().get(pk=b.pk)
    bill = _live_bill(b) or next((x for x in sorted(b.bills.all(), key=lambda x: x.created_at, reverse=True)
                                  if x.status == Bill.Status.REFUNDED), None)
    if bill:
        payments = [{"id": p.id, "kind": p.kind, "method": p.method, "amount": p.amount, "reference": p.reference,
                     "tendered": p.tendered, "change_due": p.change_due, "created_at": p.created_at,
                     "by": p.user.get_full_name() or p.user.username}
                    for p in bill.payments.select_related("user").order_by("created_at", "id")]
        totals = {"subtotal": bill.subtotal, "tax": bill.tax, "levy": bill.levy, "total": bill.total,
                  "paid": bill.amount_paid, "balance": bill.balance}
        lines = bill.lines
        meta = {"id": bill.id, "number": bill.number, "status": bill.status, "terminal": bill.terminal,
                "created_at": bill.created_at, "paid_at": bill.paid_at}
    else:
        a = room_amounts(b)   # a preview: nothing is saved until the booking is billed
        lines, payments, meta = a["lines"], [], None
        totals = {"subtotal": a["subtotal"], "tax": a["tax"], "levy": a["levy"], "total": a["total"], "paid": D(0), "balance": a["total"]}
    return clean({"booking": booking_data(b), "bill": meta, "lines": lines, "totals": totals, "payments": payments,
                  "business": business_info(), "vat_percent": _room_vat(), "levy_percent": _room_levy()})


class RoomLineIn(serializers.Serializer):
    room = serializers.IntegerField(min_value=1)
    meal_plan = serializers.ChoiceField(choices=MealPlan.values)


class BookingPaymentIn(serializers.Serializer):
    method = serializers.ChoiceField(choices=Payment.Method.values)
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=D("0.01"))
    reference = serializers.CharField(max_length=80, required=False, allow_blank=True, default="")
    tendered = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True, default=None)
    idempotency_key = serializers.CharField(max_length=64, required=False, allow_null=True, allow_blank=True, default=None)


class BookingCreateIn(serializers.Serializer):
    guest_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=30)
    occupants = serializers.IntegerField(min_value=1, max_value=20, default=1)
    narrative = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    check_in = serializers.DateField()
    check_out = serializers.DateField()
    rooms = RoomLineIn(many=True, allow_empty=False, max_length=30)
    payment = BookingPaymentIn(required=False, allow_null=True, default=None)


# ---------- API views ----------

class RoomListView(POSView):
    """Rooms with their rates and whether each is free for the dates asked (?from=YYYY-MM-DD&to=YYYY-MM-DD)."""

    def get(self, request):
        today = timezone.localdate()
        check_in = _date(request.query_params.get("from") or today, "From date")
        check_out = _date(request.query_params.get("to") or check_in + timedelta(days=1), "To date")
        if check_out <= check_in:
            check_out = check_in + timedelta(days=1)
        rooms = list(Room.objects.filter(is_active=True))
        held = {}
        for c in room_conflicts([r.pk for r in rooms], check_in, check_out):
            held.setdefault(c.room_id, c.booking)
        out = []
        for r in rooms:
            b = held.get(r.pk)
            out.append({"id": r.id, "name": r.name, "room_type": r.room_type, "capacity": r.capacity, "status": r.status,
                        "note": ROOM_NOTES.get(r.status, ""), "rates": {k: v for k, v in r.rates.items() if v > 0},
                        "booked": b is not None, "booking": b.number if b else None, "guest": b.guest_name if b else None,
                        "until": b.check_out if b else None, "booking_id": b.id if b else None})
        return Response(clean({"from": check_in, "to": check_out, "nights": (check_out - check_in).days, "rooms": out}))


class RoomStatusView(POSView):
    def post(self, request, pk):
        room = set_room_status(room=get_object_or_404(Room, pk=pk), user=request.user, status=request.data.get("status"))
        return Response({"id": room.id, "status": room.status, "note": ROOM_NOTES.get(room.status, "")})


class GuestLookupView(POSView):
    """\"Find customer\": guests from earlier bookings, matched on name or phone."""

    def get(self, request):
        term = (request.query_params.get("q") or "").strip()
        if len(term) < 2:
            return Response([])
        seen, out = set(), []
        for b in RoomBooking.objects.filter(Q(guest_name__icontains=term) | Q(phone__icontains=term)).order_by("-created_at")[:60]:
            if b.phone in seen:
                continue
            seen.add(b.phone)
            out.append({"guest_name": b.guest_name, "phone": b.phone, "narrative": b.narrative, "occupants": b.occupants})
            if len(out) == 8:
                break
        return Response(out)


def _visible_booking(request, pk):
    b = get_object_or_404(_booking_qs(), pk=pk)
    if request.user.role == User.Role.WAITER and not request.user.is_manager and b.booked_by_id != request.user.pk:
        raise ManagerApprovalRequired("This booking belongs to another waiter")
    return b


class BookingListCreateView(POSView):
    def get(self, request):
        """Bookings still active plus anything made in the last 90 days. Waiters see their own; cashiers and managers see all."""
        since = timezone.now() - timedelta(days=90)
        qs = _booking_qs().filter(Q(created_at__gte=since) | Q(status__in=RoomBooking.ACTIVE)).order_by("-created_at")[:500]
        rows = [booking_data(b) for b in qs
                if not (request.user.role == User.Role.WAITER and not request.user.is_manager and b.booked_by_id != request.user.pk)]
        return Response(clean(rows))

    def post(self, request):
        d = BookingCreateIn(data=request.data)
        d.is_valid(raise_exception=True)
        f = dict(d.validated_data)
        f["rooms"] = [dict(r) for r in f["rooms"]]
        pay = f.pop("payment", None)
        if pay:
            pay = dict(pay)
            if pay["method"] != Payment.Method.CASH:
                pay["tendered"] = None
        b = create_booking(user=request.user, payment=pay, **f)
        return Response(booking_page(b), status=201)


class BookingDetailView(POSView):
    def get(self, request, pk):
        return Response(booking_page(_visible_booking(request, pk)))


class BookingBillView(POSView):
    def post(self, request, pk):
        generate_booking_bill(booking=_visible_booking(request, pk), user=request.user)
        return Response(booking_page(get_object_or_404(RoomBooking, pk=pk)), status=201)


class BookingCheckInView(POSView):
    def post(self, request, pk):
        check_in_booking(booking=_visible_booking(request, pk), user=request.user)
        return Response(booking_page(get_object_or_404(RoomBooking, pk=pk)))


class BookingCheckOutView(POSView):
    def post(self, request, pk):
        check_out_booking(booking=_visible_booking(request, pk), user=request.user)
        return Response(booking_page(get_object_or_404(RoomBooking, pk=pk)))


class BookingCancelView(POSView):
    def post(self, request, pk):
        cancel_booking(booking=_visible_booking(request, pk), user=request.user, reason=request.data.get("reason", ""))
        return Response(booking_page(get_object_or_404(RoomBooking, pk=pk)))