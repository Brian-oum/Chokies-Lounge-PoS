"""Chokies POS - every model (and the domain exceptions) in one module.

Flow these models support
-------------------------
1. A manager/admin creates a SHIFT and assigns staff to it (SHIFT ASSIGNMENTS:
   waiters and cashiers; a cashier also gets a terminal + opening float = a cash drawer).
2. Assigned staff log in with username + PIN. Waiters/cashiers can only log in
   while they are on an open shift.
3. A waiter opens an ORDER from the menu or from the bar. A table is optional and
   several orders may share a table.
4. Every order has its own number and is billed (BILL) and paid (PAYMENT) separately
   by a cashier, against that cashier's drawer.

Sections: exceptions - base - accounts - inventory - catalog - purchasing -
          shifts - orders - billing
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Sum
from django.utils import timezone

TWO = Decimal("0.01")


# =============================================================================
# Domain exceptions (status_code is used by the API layer in views.py)
# =============================================================================

class DomainError(Exception):
    status_code = 400


class InvalidState(DomainError):
    status_code = 409


class OrderConflict(DomainError):
    status_code = 409


class ItemUnavailable(DomainError):
    status_code = 409


class InsufficientStock(DomainError):
    status_code = 409


class ManagerApprovalRequired(DomainError):
    status_code = 403


class NotOnShift(DomainError):
    status_code = 403


class AuthFailed(DomainError):
    status_code = 401


class PinLocked(DomainError):
    status_code = 429


# =============================================================================
# Base
# =============================================================================

class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class DocumentSequence(models.Model):
    """Gap-free, per-name counters (orders, POs, receipts...).

    Call inside the same transaction that creates the document: if that
    transaction rolls back, the number is released, so there are no gaps.
    """
    name = models.CharField(max_length=50, unique=True)
    last_value = models.PositiveBigIntegerField(default=0)

    @classmethod
    def next(cls, name, prefix="", width=6):
        with transaction.atomic():
            seq, _ = cls.objects.select_for_update().get_or_create(name=name)
            seq.last_value += 1
            seq.save(update_fields=["last_value"])
            return f"{prefix}{seq.last_value:0{width}d}"


class AuditLog(models.Model):
    """Append-only record of sensitive actions."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=50)
    ref_type = models.CharField(max_length=50, blank=True)
    ref_id = models.CharField(max_length=50, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError("Audit log is append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Audit log is append-only")


def audit(user, action, ref=None, **details):
    ref_type = ref.__class__.__name__ if ref is not None else ""
    ref_id = str(getattr(ref, "pk", "")) if ref is not None else ""
    return AuditLog.objects.create(user=user, action=action, ref_type=ref_type, ref_id=ref_id, details=details)


# =============================================================================
# Accounts
# =============================================================================

class User(AbstractUser):
    class Role(models.TextChoices):
        WAITER = "WAITER", "Waiter"
        CASHIER = "CASHIER", "Cashier"
        MANAGER = "MANAGER", "Manager"
        STOREKEEPER = "STOREKEEPER", "Storekeeper"
        ADMIN = "ADMIN", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.WAITER)
    pin_hash = models.CharField(max_length=128, blank=True)
    failed_pin_attempts = models.PositiveSmallIntegerField(default=0)
    pin_locked_until = models.DateTimeField(null=True, blank=True)
    # Bumped on logout / PIN change: every token issued earlier stops working.
    token_version = models.PositiveIntegerField(default=0)

    def set_pin(self, raw_pin: str):
        self.pin_hash = make_password(raw_pin)
        self.failed_pin_attempts = 0
        self.pin_locked_until = None
        self.token_version += 1

    def check_pin(self, raw_pin: str) -> bool:
        return bool(self.pin_hash) and check_password(raw_pin, self.pin_hash)

    @property
    def pin_is_locked(self) -> bool:
        return bool(self.pin_locked_until and self.pin_locked_until > timezone.now())

    @property
    def is_manager(self) -> bool:
        return self.is_superuser or self.role in (self.Role.MANAGER, self.Role.ADMIN)

    @property
    def can_take_payments(self) -> bool:
        return self.is_manager or self.role == self.Role.CASHIER

    @property
    def needs_shift(self) -> bool:
        """Waiters and cashiers may only work (and log in) while on an open shift."""
        return not self.is_superuser and self.role in (self.Role.WAITER, self.Role.CASHIER)


# =============================================================================
# Inventory
# =============================================================================

class Location(models.Model):
    code = models.SlugField(unique=True)
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class StockItem(TimeStamped):
    """An ingredient or drink. Quantities are held in the *stock unit*.

    Three units:
      purchase unit  (crate)  -> stock_units_per_purchase_unit stock units
      stock unit     (bottle)
      recipe unit    (ml)     -> recipe_units_per_stock_unit per stock unit
    """
    sku = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=150)
    category = models.CharField(max_length=80, blank=True)
    stock_unit = models.CharField(max_length=20, default="unit")
    recipe_unit = models.CharField(max_length=20, default="unit")
    recipe_units_per_stock_unit = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    purchase_unit = models.CharField(max_length=20, default="unit")
    stock_units_per_purchase_unit = models.DecimalField(max_digits=14, decimal_places=4, default=1)
    avg_cost = models.DecimalField("Weighted avg cost / stock unit", max_digits=14, decimal_places=4, default=0)
    reorder_level = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    par_level = models.DecimalField(max_digits=16, decimal_places=4, default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.stock_unit})"

    def on_hand(self, location=None) -> Decimal:
        qs = self.balances.all()
        if location is not None:
            qs = qs.filter(location=location)
        return qs.aggregate(t=Sum("quantity"))["t"] or Decimal("0")

    @property
    def needs_reorder(self):
        return self.on_hand() <= self.reorder_level


class StockBalance(models.Model):
    """Cached SUM(movements) per item+location. Only record_movement() (views.py) writes it."""
    item = models.ForeignKey(StockItem, on_delete=models.CASCADE, related_name="balances")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="balances")
    quantity = models.DecimalField(max_digits=18, decimal_places=6, default=0)

    class Meta:
        unique_together = ("item", "location")


class MovementType(models.TextChoices):
    OPENING = "OPENING", "Opening stock"
    PURCHASE = "PURCHASE", "Purchase"
    SALE = "SALE", "Sale"
    SALE_REVERSAL = "SALE_REVERSAL", "Sale reversal (void)"
    WASTE = "WASTE", "Waste"
    ADJUSTMENT = "ADJUSTMENT", "Stock-take adjustment"
    TRANSFER = "TRANSFER", "Transfer"
    RETURN = "RETURN", "Supplier return"


class LedgerImmutable(RuntimeError):
    pass


class StockMovement(models.Model):
    """The ledger. Rows are never updated or deleted; corrections are new rows."""
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="movements")
    location = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="movements")
    quantity = models.DecimalField(max_digits=18, decimal_places=6)  # signed, stock units
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    movement_type = models.CharField(max_length=20, choices=MovementType.choices)
    ref_type = models.CharField(max_length=30, blank=True)
    ref_id = models.CharField(max_length=50, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["item", "location"]), models.Index(fields=["ref_type", "ref_id"])]

    def save(self, *args, **kwargs):
        if self.pk:
            raise LedgerImmutable("Stock movements cannot be edited; add a correcting movement.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise LedgerImmutable("Stock movements cannot be deleted.")


class StockTake(TimeStamped):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Counting"
        APPROVED = "APPROVED", "Approved"

    number = models.CharField(max_length=20, unique=True)
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    is_opening = models.BooleanField(default=False)
    blind = models.BooleanField(default=True, help_text="Hide expected qty from counters")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.PROTECT, related_name="+")

    def __str__(self):
        return self.number


class StockTakeLine(models.Model):
    take = models.ForeignKey(StockTake, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT)
    expected = models.DecimalField(max_digits=18, decimal_places=6, default=0)  # frozen when the take starts
    counted = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)  # opening only

    class Meta:
        unique_together = ("take", "item")

    @property
    def variance(self):
        return None if self.counted is None else self.counted - self.expected


# =============================================================================
# Catalog (menu)
# =============================================================================

class Station(models.TextChoices):
    KITCHEN = "KITCHEN", "Kitchen"
    BAR = "BAR", "Bar"


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name


class Modifier(models.Model):
    name = models.CharField(max_length=100)
    price_delta = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    def __str__(self):
        return self.name


class MenuItem(TimeStamped):
    name = models.CharField(max_length=150)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="items")
    price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_rate = models.DecimalField("Tax %", max_digits=5, decimal_places=2, default=Decimal("16.00"))
    station = models.CharField(max_length=10, choices=Station.choices, default=Station.KITCHEN)
    track_stock = models.BooleanField(default=True, help_text="Deduct ingredients via the recipe on sale")
    is_available = models.BooleanField(default=True, help_text="Untick to 86 (sold out) manually")
    modifiers = models.ManyToManyField(Modifier, blank=True, related_name="menu_items")

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.name


class RecipeLine(models.Model):
    """quantity is in the stock item's *recipe unit* (e.g. 25 ml of whisky)."""
    menu_item = models.ForeignKey(MenuItem, on_delete=models.CASCADE, related_name="recipe_lines")
    stock_item = models.ForeignKey(StockItem, on_delete=models.PROTECT, related_name="recipe_lines")
    quantity = models.DecimalField(max_digits=14, decimal_places=4)

    class Meta:
        unique_together = ("menu_item", "stock_item")

    @property
    def stock_quantity(self) -> Decimal:
        """Quantity per portion converted to stock units."""
        return self.quantity / self.stock_item.recipe_units_per_stock_unit


# =============================================================================
# Purchasing
# =============================================================================

class Supplier(models.Model):
    name = models.CharField(max_length=150, unique=True)
    phone = models.CharField(max_length=50, blank=True)
    email = models.EmailField(blank=True)

    def __str__(self):
        return self.name


class PurchaseOrder(TimeStamped):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SENT = "SENT", "Sent"
        PARTIAL = "PARTIAL", "Partially received"
        RECEIVED = "RECEIVED", "Received"
        CANCELLED = "CANCELLED", "Cancelled"

    number = models.CharField(max_length=20, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.number


class PurchaseOrderLine(models.Model):
    po = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(StockItem, on_delete=models.PROTECT)
    qty_ordered = models.DecimalField("Qty (purchase units)", max_digits=14, decimal_places=4)
    unit_cost = models.DecimalField("Cost per purchase unit", max_digits=14, decimal_places=4)
    qty_received = models.DecimalField(max_digits=14, decimal_places=4, default=0)


class GoodsReceipt(TimeStamped):
    number = models.CharField(max_length=20, unique=True)
    po = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    location = models.ForeignKey(Location, on_delete=models.PROTECT)
    invoice_no = models.CharField(max_length=60, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")


class GoodsReceiptLine(models.Model):
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    po_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT)
    qty = models.DecimalField("Qty (purchase units)", max_digits=14, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=4)


# =============================================================================
# Shifts: the roster a manager creates, and each person's place on it
# =============================================================================

class Shift(TimeStamped):
    """A working period created by a manager/admin. Created = open.

    Staff are attached through ShiftAssignment. Waiters/cashiers can only log in
    and work while they are assigned to an OPEN shift.
    """
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        CLOSED = "CLOSED", "Closed"

    name = models.CharField(max_length=60, blank=True, help_text="e.g. Lunch, Evening")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(default=timezone.now)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.PROTECT, related_name="+")
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"Shift #{self.pk}" + (f" {self.name}" if self.name else "")


class ShiftAssignment(models.Model):
    """One person on one shift. For a CASHIER this row is also their cash drawer
    (terminal + opening float, and the count recorded when the drawer is closed)."""
    class Role(models.TextChoices):
        WAITER = "WAITER", "Waiter"
        CASHIER = "CASHIER", "Cashier"

    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="staff")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shift_assignments")
    role = models.CharField(max_length=10, choices=Role.choices)
    terminal = models.CharField(max_length=10, blank=True, help_text="Cashiers only. Defaults to T1")
    opening_float = models.DecimalField(max_digits=12, decimal_places=2, default=0, help_text="Cashiers only")
    created_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)  # signed off / drawer closed
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    counted_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    variance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shift", "user"], name="one_assignment_per_user_per_shift")]

    def __str__(self):
        return f"{self.user} as {self.get_role_display()} on {self.shift}"

    @property
    def is_active(self) -> bool:
        return self.ended_at is None and self.shift.status == Shift.Status.OPEN

    def clean(self):
        """Rules shared by the admin, the API and the services."""
        errors = []
        user = getattr(self, "user", None) if self.user_id else None
        if user is not None:
            if not user.is_active:
                errors.append(f"{user} is deactivated.")
            allowed = {User.Role.WAITER: [self.Role.WAITER], User.Role.CASHIER: [self.Role.CASHIER],
                       User.Role.MANAGER: list(self.Role), User.Role.ADMIN: list(self.Role)}.get(user.role, [])
            if user.is_superuser:
                allowed = list(self.Role)
            if self.role not in allowed:
                errors.append(f"{user} ({user.get_role_display()}) cannot work as {self.get_role_display().lower()}.")
            elif self.ended_at is None:
                clash = (ShiftAssignment.objects.filter(user=user, ended_at__isnull=True,
                                                        shift__status=Shift.Status.OPEN)
                         .exclude(pk=self.pk).exclude(shift_id=self.shift_id).first())
                if clash:
                    errors.append(f"{user} is already on {clash.shift}.")
        if self.role == self.Role.CASHIER:
            self.terminal = self.terminal or "T1"
            if self.ended_at is None:
                taken = (ShiftAssignment.objects.filter(role=self.Role.CASHIER, terminal=self.terminal,
                                                        ended_at__isnull=True, shift__status=Shift.Status.OPEN)
                         .exclude(pk=self.pk).first())
                if taken:
                    errors.append(f"Terminal {self.terminal} is already used by {taken.user} on {taken.shift}.")
        else:
            self.terminal, self.opening_float = "", Decimal(0)
        if self.opening_float is not None and self.opening_float < 0:
            errors.append("Opening float cannot be negative.")
        if self.shift_id and self._state.adding and self.shift.status != Shift.Status.OPEN:
            errors.append("Staff can only be added to an open shift.")
        if errors:
            raise ValidationError(errors)


# =============================================================================
# Orders
# =============================================================================

class Table(models.Model):
    class Status(models.TextChoices):
        FREE = "FREE", "Free"
        OCCUPIED = "OCCUPIED", "Occupied"

    name = models.CharField(max_length=30, unique=True)
    capacity = models.PositiveSmallIntegerField(default=4)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.FREE)

    def __str__(self):
        return self.name


class Order(TimeStamped):
    """One order = one number = one bill. A table can carry several orders.

    Orders are combined or divided BEFORE billing (see merge_orders / split_order in views.py),
    so the one-bill-per-order rule never has to bend."""
    class Source(models.TextChoices):
        MENU = "MENU", "Menu"
        BAR = "BAR", "Bar"

    class Type(models.TextChoices):
        DINE_IN = "DINE_IN", "Dine-in"
        TAKEAWAY = "TAKEAWAY", "Takeaway"
        BAR_TAB = "BAR_TAB", "Bar tab"
        ROOM_SERVICE = "ROOM_SERVICE", "Room service"

    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        SENT = "SENT", "Sent to kitchen/bar"
        BILLED = "BILLED", "Billed"
        PAID = "PAID", "Paid"
        POSTED_TO_ROOM = "POSTED_TO_ROOM", "Posted to room"
        CANCELLED = "CANCELLED", "Cancelled"
        MERGED = "MERGED", "Merged into another order"

    ACTIVE = (Status.OPEN, Status.SENT)                    # items can still be changed
    OCCUPYING = (Status.OPEN, Status.SENT, Status.BILLED)  # still in use / not yet settled

    number = models.CharField(max_length=20, unique=True)
    source = models.CharField(max_length=5, choices=Source.choices, default=Source.MENU)
    order_type = models.CharField(max_length=15, choices=Type.choices, default=Type.DINE_IN)
    # Null only for orders that pre-date shifts; the API always sets it.
    shift = models.ForeignKey(Shift, null=True, blank=True, on_delete=models.PROTECT, related_name="orders")
    table = models.ForeignKey(Table, null=True, blank=True, on_delete=models.PROTECT, related_name="orders")
    room_number = models.CharField(max_length=10, blank=True)  # becomes a FK when the rooms app lands
    notes = models.CharField(max_length=300, blank=True)  # order-level note from the waiter ("birthday table", ...)
    waiter = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN)
    version = models.PositiveIntegerField(default=0)  # optimistic concurrency between terminals
    # Set when this order was folded into another one so the guests can be billed together.
    merged_into = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
                                    related_name="merged_from")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number

    def totals(self):
        net = tax = Decimal(0)
        for it in self.items.exclude(status=OrderItem.Status.VOID):
            line, rate = it.line_total, it.tax_rate
            if settings.PRICES_INCLUDE_TAX:
                line_tax = line * rate / (100 + rate)
                net += line - line_tax
            else:
                line_tax = line * rate / 100
                net += line
            tax += line_tax
        net, tax = net.quantize(TWO, ROUND_HALF_UP), tax.quantize(TWO, ROUND_HALF_UP)
        return {"subtotal": net, "tax": tax, "total": net + tax}


class KOT(models.Model):
    """Kitchen/bar order ticket. kind=VOID tells the station an item was cancelled."""
    class Kind(models.TextChoices):
        NEW = "NEW", "New items"
        VOID = "VOID", "Void"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="kots")
    number = models.CharField(max_length=20, unique=True)
    station = models.CharField(max_length=10, choices=Station.choices)
    kind = models.CharField(max_length=5, choices=Kind.choices, default=Kind.NEW)
    printed = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.number


class OrderItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Not sent"
        SENT = "SENT", "Sent"
        VOID = "VOID", "Void"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    menu_item = models.ForeignKey(MenuItem, on_delete=models.PROTECT)
    # Snapshots: later menu edits must never change an existing order.
    name = models.CharField(max_length=150)
    station = models.CharField(max_length=10, choices=Station.choices)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2)
    modifiers = models.JSONField(default=list, blank=True)  # [{"name":..., "price":...}]
    quantity = models.PositiveSmallIntegerField(default=1)
    notes = models.CharField(max_length=200, blank=True)
    course = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    kot = models.ForeignKey(KOT, null=True, blank=True, on_delete=models.SET_NULL, related_name="items")
    void_reason = models.CharField(max_length=200, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def unit_total(self):
        return self.unit_price + sum((Decimal(str(m["price"])) for m in self.modifiers), Decimal(0))

    @property
    def line_total(self):
        return self.unit_total * self.quantity


# =============================================================================
# Billing
# =============================================================================

class CashMovement(models.Model):
    """Cash put into / taken out of a cashier's drawer outside of sales."""
    class Kind(models.TextChoices):
        PAID_IN = "PAID_IN", "Cash paid in"
        PAYOUT = "PAYOUT", "Cash payout"

    drawer = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT, related_name="cash_movements")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # always positive
    reason = models.CharField(max_length=200)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)


class Bill(TimeStamped):
    """The bill for exactly one order."""
    class Status(models.TextChoices):
        OPEN = "OPEN", "Awaiting payment"
        PAID = "PAID", "Paid"
        CANCELLED = "CANCELLED", "Cancelled"
        REFUNDED = "REFUNDED", "Refunded"

    number = models.CharField(max_length=20, unique=True)  # gap-free per terminal, never deleted
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="bills")
    terminal = models.CharField(max_length=10)
    lines = models.JSONField(default=list)  # immutable receipt snapshot
    subtotal = models.DecimalField("Net", max_digits=12, decimal_places=2)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_reason = models.CharField(max_length=200, blank=True)
    discount_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                             on_delete=models.PROTECT, related_name="+")
    service_charge = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    amount_paid = models.DecimalField("Paid (net of refunds)", max_digits=12, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                     on_delete=models.PROTECT, related_name="+")
    cancel_reason = models.CharField(max_length=200, blank=True)
    # Reserved for fiscal device / e-invoicing integration
    fiscal_status = models.CharField(max_length=20, blank=True)
    fiscal_number = models.CharField(max_length=60, blank=True)
    fiscal_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["order"], condition=Q(status__in=["OPEN", "PAID"]),
                                               name="one_live_bill_per_order")]

    def __str__(self):
        return self.number

    @property
    def balance(self) -> Decimal:
        return self.total - self.amount_paid


class Payment(models.Model):
    class Method(models.TextChoices):
        CASH = "CASH", "Cash"
        CARD = "CARD", "Card"
        MOBILE_MONEY = "MOBILE_MONEY", "Mobile money"
        VOUCHER = "VOUCHER", "Voucher"

    class Kind(models.TextChoices):
        PAYMENT = "PAYMENT", "Payment"
        REFUND = "REFUND", "Refund"

    bill = models.ForeignKey(Bill, on_delete=models.PROTECT, related_name="payments")
    drawer = models.ForeignKey(ShiftAssignment, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PAYMENT)
    method = models.CharField(max_length=15, choices=Method.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)  # signed: refunds are negative
    tendered = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)  # cash handed over
    reference = models.CharField(max_length=80, blank=True)  # card slip / mobile-money code
    reason = models.CharField(max_length=200, blank=True)
    refund_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="refunds")
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def change_due(self):
        if self.kind == self.Kind.PAYMENT and self.tendered is not None:
            return self.tendered - self.amount
        return Decimal(0)


class ZReport(models.Model):
    """End-of-day snapshot. Immutable."""
    number = models.CharField(max_length=20, unique=True)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    data = models.JSONField()
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_end"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError("Z reports are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Z reports cannot be deleted")