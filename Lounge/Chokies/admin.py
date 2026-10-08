import csv

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Sum
from django.db.models.functions import TruncDay, TruncMonth, TruncWeek, TruncYear
from django.forms.models import BaseInlineFormSet
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.template import engines
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html

from Chokies.models import (KOT, AuditLog, Bill, CashMovement, Category, DocumentSequence, GoodsReceipt,
                        GoodsReceiptLine, Location, MenuItem, Modifier, Order, OrderItem, Payment,
                        PurchaseOrder, PurchaseOrderLine, RecipeLine, Shift, ShiftAssignment, StockBalance, StockItem,
                        StockMovement, StockTake, StockTakeLine, Supplier, Table, User, ZReport)
from Chokies.views import render_receipt


class ReadOnly(admin.ModelAdmin):
    """Records that change only through Chokies.views services (ledger, bills, reports)."""
    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


# ---- users ----
class UserForm(UserChangeForm):
    new_pin = forms.CharField(required=False, help_text="Set/replace the 4-6 digit PIN")

    class Meta(UserChangeForm.Meta):
        model = User

    def clean_new_pin(self):
        pin = self.cleaned_data.get("new_pin", "")
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 6):
            raise forms.ValidationError("PIN must be 4 to 6 digits.")
        return pin

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("new_pin"):
            user.set_pin(self.cleaned_data["new_pin"])
        if commit:
            user.save()
        return user


@admin.register(User)
class POSUserAdmin(UserAdmin):
    form = UserForm
    list_display = ("username", "first_name", "role", "is_active")
    fieldsets = UserAdmin.fieldsets + (("POS", {"fields": ("role", "new_pin", "failed_pin_attempts", "pin_locked_until")}),)


# ---- inventory ----
class BalanceInline(admin.TabularInline):
    model = StockBalance
    extra = 0
    readonly_fields = ("location", "quantity")
    can_delete = False
    def has_add_permission(self, request, obj=None): return False


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "category", "stock_unit", "avg_cost", "reorder_level", "is_active")
    search_fields = ("name", "sku")
    list_filter = ("category", "is_active")
    readonly_fields = ("avg_cost",)
    inlines = [BalanceInline]


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnly):
    list_display = ("created_at", "item", "location", "quantity", "unit_cost", "movement_type", "ref_type", "ref_id")
    list_filter = ("movement_type", "location")
    search_fields = ("item__name", "ref_id")


class StockTakeLineInline(admin.TabularInline):
    model = StockTakeLine
    extra = 0


@admin.register(StockTake)
class StockTakeAdmin(admin.ModelAdmin):
    list_display = ("number", "location", "status", "is_opening", "created_by")
    inlines = [StockTakeLineInline]


admin.site.register(Location)


# ---- menu ----
class RecipeInline(admin.TabularInline):
    model = RecipeLine
    extra = 1


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "station", "track_stock", "is_available")
    list_filter = ("category", "station", "is_available")
    search_fields = ("name",)
    inlines = [RecipeInline]
    filter_horizontal = ("modifiers",)


admin.site.register(Category)
admin.site.register(Modifier)


# ---- purchasing ----
class POLineInline(admin.TabularInline):
    model = PurchaseOrderLine
    extra = 1
    readonly_fields = ("qty_received",)


@admin.register(PurchaseOrder)
class PurchaseOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "supplier", "status", "created_at")
    list_filter = ("status",)
    inlines = [POLineInline]


class GoodsReceiptLineInline(admin.TabularInline):
    model = GoodsReceiptLine
    extra = 0


@admin.register(GoodsReceipt)
class GoodsReceiptAdmin(ReadOnly):
    # Created through Chokies.views.receive_goods so stock stays correct.
    list_display = ("number", "po", "location", "invoice_no", "received_by")


admin.site.register(Supplier)


# ---- orders ----
class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = [f.name for f in OrderItem._meta.fields if f.name != "id"]
    can_delete = False
    def has_add_permission(self, request, obj=None): return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("number", "source", "order_type", "table", "waiter", "shift", "status", "created_at")
    list_filter = ("status", "source", "order_type", "shift")
    inlines = [OrderItemInline]
    readonly_fields = ("number", "version")  # numbers/versions are set by services


admin.site.register(Table)
admin.site.register(KOT)


# ---- billing ----
class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    can_delete = False
    readonly_fields = [f.name for f in Payment._meta.fields if f.name != "id"]
    def has_add_permission(self, request, obj=None): return False


PERIODS = {"day": (TruncDay, "%a %d %b %Y"), "week": (TruncWeek, "Week of %d %b %Y"),
           "month": (TruncMonth, "%B %Y"), "year": (TruncYear, "%Y")}
MONEY = ("net", "tax", "service", "discounts", "total", "collected")

# The two admin pages below are built from these strings, so no template files need to be copied anywhere.
BILL_LIST_TEMPLATE = """{% extends "admin/change_list.html" %}
{% block object-tools-items %}
  <li><a href="sales-summary/" class="addlink">Sales summary</a></li>
  {{ block.super }}
{% endblock %}
{% block result_list %}
  {% if sales %}
  <div style="display:flex;flex-wrap:wrap;gap:12px;margin:0 0 14px">
    <div style="flex:1;min-width:380px;border:1px solid var(--hairline-color,#ddd);border-radius:6px;padding:10px 14px">
      <b>Sales in this view (paid bills)</b>
      <table style="width:100%;margin-top:6px">
        <tr><td>Bills</td><td style="text-align:right"><b>{{ sales.bills }}</b></td>
            <td>Net</td><td style="text-align:right">{{ sales.net|floatformat:2 }}</td></tr>
        <tr><td>VAT</td><td style="text-align:right">{{ sales.tax|floatformat:2 }}</td>
            <td>Service charge</td><td style="text-align:right">{{ sales.service|floatformat:2 }}</td></tr>
        <tr><td>Discounts</td><td style="text-align:right">{{ sales.discounts|floatformat:2 }}</td>
            <td><b>Total</b></td><td style="text-align:right;font-size:1.2em"><b>{{ sales.total|floatformat:2 }}</b></td></tr>
      </table>
    </div>
    <div style="flex:1;min-width:260px;border:1px solid var(--hairline-color,#ddd);border-radius:6px;padding:10px 14px">
      <b>By status</b>
      <table style="width:100%;margin-top:6px">{% for label, n, t in by_status %}
        <tr><td>{{ label }}</td><td style="text-align:right">{{ n }}</td><td style="text-align:right">{{ t|floatformat:2 }}</td></tr>{% endfor %}</table>
    </div>
    <div style="flex:1;min-width:200px;border:1px solid var(--hairline-color,#ddd);border-radius:6px;padding:10px 14px">
      <b>Payments by method</b>
      <table style="width:100%;margin-top:6px">{% for m in methods %}
        <tr><td>{{ m.method }}</td><td style="text-align:right">{{ m.amount|floatformat:2 }}</td></tr>{% empty %}<tr><td>None</td></tr>{% endfor %}</table>
    </div>
  </div>
  {% endif %}
  {{ block.super }}
{% endblock %}
"""
SALES_SUMMARY_TEMPLATE = """{% extends "admin/base_site.html" %}
{% block breadcrumbs %}<div class="breadcrumbs"><a href="../../../">Home</a> &rsaquo; <a href="../">Bills</a> &rsaquo; Sales summary</div>{% endblock %}
{% block content %}
<form method="get" style="display:flex;flex-wrap:wrap;gap:12px;align-items:end;margin-bottom:18px">
  <label>Group by<br><select name="period">{% for p in periods %}<option value="{{ p }}" {% if p == period %}selected{% endif %}>{{ p|title }}</option>{% endfor %}</select></label>
  <label>From<br><input type="date" name="from" value="{{ start|date:'Y-m-d' }}"></label>
  <label>To<br><input type="date" name="to" value="{{ end|date:'Y-m-d' }}"></label>
  <label>Bills<br><select name="status">{% for s in statuses %}<option value="{{ s }}" {% if s == status %}selected{% endif %}>{{ s|title }}</option>{% endfor %}</select></label>
  <input type="submit" value="Show">
  <a class="button" href="?period={{ period }}&from={{ start|date:'Y-m-d' }}&to={{ end|date:'Y-m-d' }}&status={{ status }}&format=csv">Download CSV</a>
</form>
<table style="width:100%">
  <thead><tr><th>Period</th><th style="text-align:right">Bills</th><th style="text-align:right">Net</th><th style="text-align:right">VAT</th>
    <th style="text-align:right">Service</th><th style="text-align:right">Discounts</th><th style="text-align:right">Total</th><th style="text-align:right">Collected</th></tr></thead>
  <tbody>{% for r in rows %}
    <tr><td>{{ r.label }}</td><td style="text-align:right">{{ r.bills }}</td><td style="text-align:right">{{ r.net|floatformat:2 }}</td>
      <td style="text-align:right">{{ r.tax|floatformat:2 }}</td><td style="text-align:right">{{ r.service|floatformat:2 }}</td>
      <td style="text-align:right">{{ r.discounts|floatformat:2 }}</td><td style="text-align:right"><b>{{ r.total|floatformat:2 }}</b></td>
      <td style="text-align:right">{{ r.collected|floatformat:2 }}</td></tr>
  {% empty %}<tr><td colspan="8">No bills in this range.</td></tr>{% endfor %}</tbody>
  <tfoot><tr style="font-weight:bold"><td>TOTAL</td><td style="text-align:right">{{ totals.bills }}</td><td style="text-align:right">{{ totals.net|floatformat:2 }}</td>
    <td style="text-align:right">{{ totals.tax|floatformat:2 }}</td><td style="text-align:right">{{ totals.service|floatformat:2 }}</td>
    <td style="text-align:right">{{ totals.discounts|floatformat:2 }}</td><td style="text-align:right">{{ totals.total|floatformat:2 }}</td>
    <td style="text-align:right">{{ totals.collected|floatformat:2 }}</td></tr></tfoot>
</table>
<h3 style="margin-top:24px">Payments received by method (net of refunds)</h3>
<table>{% for m in methods %}<tr><td style="padding-right:30px">{{ m.method }}</td><td style="text-align:right">{{ m.amount|floatformat:2 }}</td></tr>{% empty %}<tr><td>None</td></tr>{% endfor %}</table>
{% endblock %}
"""


def _template(source):
    return engines.all()[0].from_string(source)


def _sums(qs):
    r = qs.order_by().aggregate(bills=Count("id"), net=Sum("subtotal"), tax=Sum("tax"), service=Sum("service_charge"),
                                discounts=Sum("discount_amount"), total=Sum("total"), collected=Sum("amount_paid"))
    return {k: v or 0 for k, v in r.items()}


def _methods(qs):
    return list(Payment.objects.filter(bill__in=qs.order_by().values("pk")).values("method")
                .annotate(amount=Sum("amount")).order_by("method"))


@admin.register(Bill)
class BillAdmin(ReadOnly):
    list_display = ("number", "order", "status", "total", "amount_paid", "created_at", "print_link")
    list_filter = (("created_at", admin.DateFieldListFilter), "status", "terminal")  # Today / 7 days / month / year
    date_hierarchy = "created_at"                                                    # year > month > day drill-down
    search_fields = ("number",)
    inlines = [PaymentInline]

    @property
    def change_list_template(self):
        return _template(BILL_LIST_TEMPLATE)

    @admin.display(description="Receipt")
    def print_link(self, obj):
        return format_html('<a href="{}" target="_blank">Print</a>',
                           reverse(f"admin:{self.opts.app_label}_{self.opts.model_name}_receipt", args=[obj.pk]))

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        return [path("sales-summary/", self.admin_site.admin_view(self.sales_summary), name="%s_%s_sales_summary" % info),
                path("<int:pk>/receipt/", self.admin_site.admin_view(self.receipt), name="%s_%s_receipt" % info),
                ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        """Totals of whatever the filters currently show (paid = sales; the rest for reference)."""
        resp = super().changelist_view(request, extra_context)
        cd = getattr(resp, "context_data", None)
        if cd and "cl" in cd:
            qs = cd["cl"].queryset
            by = {r["status"]: r for r in qs.order_by().values("status").annotate(n=Count("id"), t=Sum("total"))}
            cd["sales"] = _sums(qs.filter(status=Bill.Status.PAID))
            cd["by_status"] = [(label, by.get(v, {}).get("n", 0), by.get(v, {}).get("t") or 0) for v, label in Bill.Status.choices]
            cd["methods"] = _methods(qs)
        return resp

    def receipt(self, request, pk):
        bill = get_object_or_404(Bill.objects.select_related("order__table", "order__waiter"), pk=pk)
        if not self.has_view_permission(request, bill):
            raise PermissionDenied
        return HttpResponse(render_receipt(bill, autoprint=True))

    def sales_summary(self, request):
        """Daily / weekly / monthly / yearly totals between two dates. ?format=csv downloads it."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        period = request.GET.get("period") if request.GET.get("period") in PERIODS else "day"
        today = timezone.localdate()
        start = parse_date(request.GET.get("from") or "") or today.replace(day=1)
        end = parse_date(request.GET.get("to") or "") or today
        status = request.GET.get("status", "PAID")
        qs = Bill.objects.filter(created_at__date__gte=start, created_at__date__lte=end)
        if status != "ALL":
            qs = qs.filter(status=status)
        trunc, fmt = PERIODS[period]
        rows = [{**r, "label": r["p"].strftime(fmt)} for r in
                qs.annotate(p=trunc("created_at")).values("p").annotate(
                    bills=Count("id"), net=Sum("subtotal"), tax=Sum("tax"), service=Sum("service_charge"),
                    discounts=Sum("discount_amount"), total=Sum("total"), collected=Sum("amount_paid")).order_by("-p")]
        totals = _sums(qs)
        if request.GET.get("format") == "csv":
            resp = HttpResponse(content_type="text/csv")
            resp["Content-Disposition"] = f'attachment; filename="sales-{period}-{start}-{end}.csv"'
            w = csv.writer(resp)
            w.writerow(["Period", "Bills", *[m.title() for m in MONEY]])
            for r in rows:
                w.writerow([r["label"], r["bills"], *[r[m] or 0 for m in MONEY]])
            w.writerow(["TOTAL", totals["bills"], *[totals[m] for m in MONEY]])
            return resp
        return TemplateResponse(request, _template(SALES_SUMMARY_TEMPLATE), {
            **self.admin_site.each_context(request), "title": "Sales summary", "opts": self.opts, "rows": rows,
            "totals": totals, "methods": _methods(qs), "period": period, "start": start, "end": end, "status": status,
            "periods": list(PERIODS), "statuses": ["PAID", "OPEN", "CANCELLED", "REFUNDED", "ALL"]})


# ---- shifts: managers/admins create these; staff then log in with their PIN ----
class ShiftAssignmentFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        terminals = set()
        for form in self.forms:
            cd = getattr(form, "cleaned_data", None)
            if not cd or cd.get("DELETE") or cd.get("role") != ShiftAssignment.Role.CASHIER:
                continue
            terminal = cd.get("terminal") or "T1"
            if terminal in terminals:
                raise forms.ValidationError(f"Terminal {terminal} is given to two cashiers.")
            terminals.add(terminal)


class ShiftAssignmentInline(admin.TabularInline):
    model = ShiftAssignment
    formset = ShiftAssignmentFormSet
    extra = 2
    fields = ("user", "role", "terminal", "opening_float", "ended_at", "expected_cash", "counted_cash", "variance")
    readonly_fields = ("ended_at", "expected_cash", "counted_cash", "variance")


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    # Staff + float are edited here; closing (cash count) goes through the API / views.close_shift.
    list_display = ("id", "name", "status", "opened_at", "closed_at", "created_by")
    list_filter = ("status",)
    fields = ("name", "notes", "status", "opened_at", "closed_at", "created_by", "closed_by")
    readonly_fields = ("status", "opened_at", "closed_at", "created_by", "closed_by")
    inlines = [ShiftAssignmentInline]

    def has_delete_permission(self, request, obj=None): return False

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.status == Shift.Status.OPEN

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ZReport)
class ZReportAdmin(ReadOnly):
    list_display = ("number", "period_start", "period_end", "closed_by")


admin.site.register(CashMovement, ReadOnly)


# ---- system ----
@admin.register(AuditLog)
class AuditLogAdmin(ReadOnly):
    list_display = ("created_at", "user", "action", "ref_type", "ref_id")
    list_filter = ("action",)


admin.site.register(DocumentSequence)