"""Manager back office: purchasing, inventory, voids and discounts (pages call the real services)."""
from decimal import Decimal as D

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import manager_views as mv
from . import views
from .models import (AuditLog, Bill, Category, GoodsReceipt, Location, MenuItem, MovementType, Order, OrderItem,
                     PurchaseOrder, RecipeLine, StockBalance, StockItem, StockMovement, StockTake, Supplier, User)

FAST = dict(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])


def mk_user(username, role, pin="1234"):
    u = User.objects.create(username=username, role=role, first_name=username.title())
    u.set_pin(pin)
    u.save()
    return u


def url(name, *a):
    return reverse(f"manager:{name}", args=a)


@override_settings(**FAST)
class ManagerBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = mk_user("mary", User.Role.MANAGER)
        cls.waiter = mk_user("wanjiku", User.Role.WAITER)
        cls.cashier = mk_user("chebet", User.Role.CASHIER)
        cls.store = Location.objects.create(code="store", name="Main store")
        cls.kitchen = Location.objects.create(code="kitchen", name="Kitchen")
        cls.bar = Location.objects.create(code="bar", name="Bar")
        cls.supplier = Supplier.objects.create(name="Bidco")
        # a crate of 24 bottles; recipe unit = stock unit
        cls.beer = StockItem.objects.create(sku="TUSK", name="Tusker", category="Beer", stock_unit="bottle",
                                            purchase_unit="crate", stock_units_per_purchase_unit=24,
                                            reorder_level=10)
        cls.rice = StockItem.objects.create(sku="RICE", name="Rice", category="Dry", stock_unit="kg", purchase_unit="bag",
                                            stock_units_per_purchase_unit=50)

    def setUp(self):
        self.c = Client()
        self.c.force_login(self.manager)

    def on_hand(self, item, loc):
        b = StockBalance.objects.filter(item=item, location=loc).first()
        return b.quantity if b else D(0)

    def lines(self, *rows, cost=True):
        """rows: (item, qty, cost) -> POST data."""
        d = {"item": [str(r[0].pk) for r in rows], "qty": [str(r[1]) for r in rows]}
        if cost:
            d["cost"] = [str(r[2]) for r in rows]
        return d


class AccessTests(ManagerBase):
    NAMES = ["dashboard", "purchases", "purchase_new", "lpos", "lpo_new", "receive", "items", "item_new", "balances",
             "stock_takes", "issue", "voids", "discounts"]

    def test_every_page_renders_for_a_manager(self):
        for n in self.NAMES:
            r = self.c.get(url(n))
            self.assertEqual(r.status_code, 200, n)
        self.assertEqual(self.c.get(url("item_detail", self.beer.pk)).status_code, 200)
        self.assertEqual(self.c.get(url("item_edit", self.beer.pk)).status_code, 200)

    def test_anonymous_is_sent_to_sign_in_and_staff_are_refused(self):
        r = Client().get(url("items"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/pin-login/", r["Location"])
        self.assertIn("next=", r["Location"])
        w = Client()
        w.force_login(self.waiter)
        self.assertEqual(w.get(url("items")).status_code, 403)
        self.assertEqual(w.post(url("voids")).status_code, 403)


class DirectPurchaseTests(ManagerBase):
    def post_purchase(self, **kw):
        data = {"supplier": self.supplier.pk, "location": self.store.pk, "invoice_no": "INV-1",
                **self.lines((self.beer, 2, "2400"), (self.rice, 1, "6000"))}
        data.update(kw)
        return self.c.post(url("purchase_new"), data)

    def test_direct_purchase_receives_stock_and_sets_average_cost(self):
        r = self.post_purchase()
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        self.assertEqual(self.on_hand(self.beer, self.store), 48)         # 2 crates x 24
        self.assertEqual(self.on_hand(self.rice, self.store), 50)
        self.beer.refresh_from_db()
        self.assertEqual(self.beer.avg_cost, D("100"))                    # 2400 / 24 bottles
        po = PurchaseOrder.objects.get()
        self.assertTrue(po.number.startswith("DP-"))
        self.assertEqual(po.status, "RECEIVED")
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(r["Location"], url("purchase_detail", receipt.pk))
        self.assertTrue(AuditLog.objects.filter(action="direct_purchase").exists())

    def test_purchases_list_shows_it_and_lpo_list_does_not(self):
        self.post_purchase()
        self.assertContains(self.c.get(url("purchases")), "GRN-000001")
        self.assertContains(self.c.get(url("purchases") + "?kind=direct"), "GRN-000001")
        self.assertNotContains(self.c.get(url("purchases") + "?kind=lpo"), "GRN-000001")
        self.assertNotContains(self.c.get(url("lpos")), "DP-000001")
        self.assertContains(self.c.get(url("purchase_detail", GoodsReceipt.objects.get().pk)), "Direct purchase")

    def test_new_supplier_typed_in_is_created_once(self):
        self.post_purchase(supplier="", new_supplier="Naivas Wholesale")
        self.post_purchase(supplier="", new_supplier="naivas wholesale")
        self.assertEqual(Supplier.objects.filter(name__iexact="naivas wholesale").count(), 1)

    def test_bad_input_changes_nothing(self):
        for bad in ({"qty": ["0", "1"]}, {"cost": ["x", "1"]}, {"supplier": ""}, {"location": ""}):
            r = self.post_purchase(**bad)
            self.assertEqual(r.status_code, 422, bad)
        self.assertEqual(GoodsReceipt.objects.count(), 0)
        self.assertEqual(PurchaseOrder.objects.count(), 0)
        self.assertEqual(StockMovement.objects.count(), 0)

    def test_failing_line_rolls_back_the_whole_purchase(self):
        data = {"supplier": self.supplier.pk, "location": self.store.pk, "item": [self.beer.pk, 99999], "qty": ["1", "1"], "cost": ["10", "10"]}
        self.assertEqual(self.c.post(url("purchase_new"), data).status_code, 422)
        self.assertEqual(StockMovement.objects.count(), 0)


class LpoTests(ManagerBase):
    def make_lpo(self, send=True):
        data = {"supplier": self.supplier.pk, "then": "send" if send else "draft", **self.lines((self.beer, 10, "2400"), (self.rice, 4, "6000"))}
        r = self.c.post(url("lpo_new"), data)
        self.assertEqual(r.status_code, 302)
        return PurchaseOrder.objects.get()

    def test_create_send_and_list(self):
        draft = self.make_lpo(send=False)
        self.assertEqual(draft.status, "DRAFT")
        self.assertTrue(draft.number.startswith("PO-"))
        self.assertContains(self.c.get(url("lpos")), draft.number)
        self.c.post(url("lpo_action", draft.pk, "send"))
        draft.refresh_from_db()
        self.assertEqual(draft.status, "SENT")
        self.assertEqual(self.c.get(url("lpos") + "?status=SENT").context["page"].paginator.count, 1)
        self.assertEqual(self.c.get(url("lpos") + "?status=DRAFT").context["page"].paginator.count, 0)

    def test_partial_then_full_receipt(self):
        po = self.make_lpo()
        beer_line, rice_line = po.lines.order_by("id")
        # a form for an LPO that is not sent is not offered
        r = self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk, f"qty_{beer_line.pk}": "6", "invoice_no": "D-1"})
        self.assertEqual(r.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.status, "PARTIAL")
        self.assertEqual(self.on_hand(self.beer, self.store), 144)        # 6 crates x 24
        # more than outstanding is refused, nothing booked
        r = self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk, f"qty_{beer_line.pk}": "5"})
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "still to receive", status_code=422)
        self.assertEqual(self.on_hand(self.beer, self.store), 144)
        # an empty form is refused
        self.assertEqual(self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk}).status_code, 422)
        # the rest, with the invoice price overriding the LPO price
        r = self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk, f"qty_{beer_line.pk}": "4",
                                          f"cost_{beer_line.pk}": "2640", f"qty_{rice_line.pk}": "4"})
        self.assertEqual(r.status_code, 302)
        po.refresh_from_db()
        self.assertEqual(po.status, "RECEIVED")
        self.beer.refresh_from_db()
        self.assertEqual(self.beer.avg_cost, D("100") * D("0.6") + D("110") * D("0.4"))   # 6 crates @100, 4 @110 per bottle
        self.assertEqual(self.c.get(url("lpo_detail", po.pk)).context["receipts"].count(), 2)
        # a finished LPO cannot be received against again
        self.assertEqual(self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk, f"qty_{beer_line.pk}": "1"}).status_code, 422)

    def test_receive_page_lists_only_waiting_lpos(self):
        draft = self.make_lpo(send=False)
        self.assertEqual(list(self.c.get(url("receive")).context["waiting"]), [])
        self.c.post(url("lpo_action", draft.pk, "send"))
        self.assertEqual(list(self.c.get(url("receive")).context["waiting"]), [draft])

    def test_cancel_rules(self):
        po = self.make_lpo()
        self.c.post(url("lpo_action", po.pk, "cancel"), {"reason": ""})
        po.refresh_from_db()
        self.assertEqual(po.status, "SENT")                                # a reason is required
        line = po.lines.first()
        self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk, f"qty_{line.pk}": "1"})
        self.c.post(url("lpo_action", po.pk, "cancel"), {"reason": "changed mind"})
        po.refresh_from_db()
        self.assertEqual(po.status, "PARTIAL")                             # part-delivered: cannot cancel
        self.c.post(url("lpo_action", po.pk, "close"), {"reason": "Supplier out of stock"})
        po.refresh_from_db()
        self.assertEqual(po.status, "RECEIVED")
        self.assertIn("Closed short", po.notes)

    def test_cancel_unreceived_lpo(self):
        po = self.make_lpo()
        self.c.post(url("lpo_action", po.pk, "cancel"), {"reason": "Wrong supplier"})
        po.refresh_from_db()
        self.assertEqual(po.status, "CANCELLED")
        self.assertEqual(self.c.post(url("receive"), {"po": po.pk, "location": self.store.pk, "qty_1": "1"}).status_code, 422)

    def test_actions_need_post_and_a_manager(self):
        po = self.make_lpo(send=False)
        self.assertEqual(self.c.get(url("lpo_action", po.pk, "send")).status_code, 405)
        w = Client()
        w.force_login(self.waiter)
        self.assertEqual(w.post(url("lpo_action", po.pk, "send")).status_code, 403)
        self.assertEqual(self.c.post(url("lpo_action", po.pk, "explode")).status_code, 403)


class ItemTests(ManagerBase):
    def form(self, **kw):
        d = {"sku": "GIN", "name": "Gin", "category": "Spirits", "stock_unit": "bottle", "purchase_unit": "carton",
             "stock_units_per_purchase_unit": "12", "recipe_unit": "ml", "recipe_units_per_stock_unit": "750",
             "reorder_level": "5", "par_level": "20", "is_active": "on"}
        d.update(kw)
        return d

    def test_create_edit_and_toggle(self):
        r = self.c.post(url("item_new"), self.form())
        self.assertEqual(r.status_code, 302)
        gin = StockItem.objects.get(sku="GIN")
        self.assertTrue(gin.is_active)
        self.c.post(url("item_edit", gin.pk), self.form(name="Gin 750ml", reorder_level="8"))
        gin.refresh_from_db()
        self.assertEqual((gin.name, gin.reorder_level), ("Gin 750ml", 8))
        self.c.post(url("item_toggle", gin.pk))
        gin.refresh_from_db()
        self.assertFalse(gin.is_active)
        self.assertNotIn(gin, list(self.c.get(url("items")).context["page"]))          # (the flash message also names it)
        self.assertIn(gin, list(self.c.get(url("items") + "?show=all").context["page"]))

    def test_validation(self):
        r = self.c.post(url("item_new"), self.form(stock_units_per_purchase_unit="0"))
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Must be more than zero", status_code=422)
        r = self.c.post(url("item_new"), self.form(sku="TUSK"))            # duplicate SKU
        self.assertEqual(r.status_code, 422)

    def test_list_flags_low_stock_and_detail_shows_ledger(self):
        views.record_movement(item=self.beer, location=self.store, quantity=4, movement_type="OPENING", unit_cost=100)
        r = self.c.get(url("items") + "?low=1")
        self.assertContains(r, "Tusker")
        self.assertNotContains(r, "Rice")
        d = self.c.get(url("item_detail", self.beer.pk))
        self.assertContains(d, "Opening stock")
        self.assertEqual(d.context["total"], 4)


class BalanceTests(ManagerBase):
    def test_balances_by_location_value_and_csv(self):
        views.record_movement(item=self.beer, location=self.store, quantity=30, movement_type="OPENING", unit_cost=100)
        views.record_movement(item=self.beer, location=self.bar, quantity=5, movement_type="OPENING", unit_cost=100)
        r = self.c.get(url("balances"))
        row = next(x for x in r.context["rows"] if x["item"] == self.beer)
        self.assertEqual(row["total"], 35)
        self.assertEqual(row["value"], 3500)
        only_bar = self.c.get(url("balances") + f"?location={self.bar.pk}")
        self.assertEqual(next(x for x in only_bar.context["rows"] if x["item"] == self.beer)["total"], 5)
        self.assertEqual([x["item"] for x in self.c.get(url("balances") + "?hide_zero=1").context["rows"]], [self.beer])
        csv_resp = self.c.get(url("balances") + "?export=csv")
        self.assertEqual(csv_resp["Content-Type"], "text/csv")
        self.assertIn("Tusker", csv_resp.content.decode())


class StockTakeTests(ManagerBase):
    def setUp(self):
        super().setUp()
        views.record_movement(item=self.beer, location=self.store, quantity=20, movement_type="OPENING", unit_cost=100)

    def start(self, **kw):
        d = {"location": self.store.pk, "kind": "regular", "blind": "1"}
        d.update(kw)
        r = self.c.post(url("stock_takes"), d)
        self.assertEqual(r.status_code, 302)
        return StockTake.objects.latest("id")

    def test_count_review_and_approve(self):
        take = self.start()
        self.assertEqual(take.lines.count(), 2)
        page = self.c.get(url("stock_take", take.pk))
        self.assertFalse(page.context["show_expected"])                    # blind while counting
        beer_line = take.lines.get(item=self.beer)
        self.c.post(url("stock_take", take.pk), {"action": "save", f"count_{beer_line.pk}": "17"})
        beer_line.refresh_from_db()
        self.assertEqual(beer_line.counted, 17)
        review = self.c.get(url("stock_take", take.pk))
        self.assertEqual(review.context["counted"], 1)
        self.assertEqual(review.context["net_value"], D("-300"))           # -3 bottles @100
        self.assertEqual(self.on_hand(self.beer, self.store), 20)          # nothing applied yet
        self.c.post(url("stock_take", take.pk), {"action": "approve", f"count_{beer_line.pk}": "17"})
        take.refresh_from_db()
        self.assertEqual(take.status, "APPROVED")
        self.assertEqual(take.approved_by, self.manager)
        self.assertEqual(self.on_hand(self.beer, self.store), 17)
        self.assertEqual(self.on_hand(self.rice, self.store), 0)           # uncounted: untouched
        self.assertEqual(StockMovement.objects.filter(movement_type=MovementType.ADJUSTMENT).count(), 1)

    def test_approved_take_cannot_be_changed(self):
        take = self.start()
        line = take.lines.get(item=self.beer)
        self.c.post(url("stock_take", take.pk), {"action": "approve", f"count_{line.pk}": "18"})
        self.c.post(url("stock_take", take.pk), {"action": "approve", f"count_{line.pk}": "5"})
        self.assertEqual(self.on_hand(self.beer, self.store), 18)

    def test_sales_during_counting_are_preserved(self):
        take = self.start()
        line = take.lines.get(item=self.beer)
        views.record_movement(item=self.beer, location=self.store, quantity=-2, movement_type="SALE")   # 18 now
        self.c.post(url("stock_take", take.pk), {"action": "approve", f"count_{line.pk}": "19"})         # counted 19, expected was 20
        self.assertEqual(self.on_hand(self.beer, self.store), 17)

    def test_negative_or_garbage_count_is_refused(self):
        take = self.start()
        line = take.lines.get(item=self.beer)
        for bad in ("-1", "abc"):
            self.c.post(url("stock_take", take.pk), {"action": "save", f"count_{line.pk}": bad})
            line.refresh_from_db()
            self.assertIsNone(line.counted)

    def test_opening_take_sets_quantity_and_cost(self):
        take = self.start(kind="opening", location=self.kitchen.pk)
        line = take.lines.get(item=self.rice)
        self.c.post(url("stock_take", take.pk), {"action": "approve", f"count_{line.pk}": "10", f"cost_{line.pk}": "120"})
        self.assertEqual(self.on_hand(self.rice, self.kitchen), 10)
        self.rice.refresh_from_db()
        self.assertEqual(self.rice.avg_cost, 120)

    def test_category_filter(self):
        take = self.start(category="Beer")
        self.assertEqual(list(take.lines.values_list("item__sku", flat=True)), ["TUSK"])


class IssueTests(ManagerBase):
    def setUp(self):
        super().setUp()
        views.record_movement(item=self.beer, location=self.store, quantity=48, movement_type="PURCHASE", unit_cost=100)
        views.record_movement(item=self.rice, location=self.store, quantity=5, movement_type="PURCHASE", unit_cost=120)

    def issue(self, **kw):
        d = {"mode": "issue", "from_location": self.store.pk, "to_location": self.kitchen.pk,
             **self.lines((self.beer, 24), (self.rice, 2), cost=False)}
        d.update(kw)
        return self.c.post(url("issue"), d)

    def test_issue_moves_stock_with_one_reference_and_keeps_cost(self):
        r = self.issue()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.on_hand(self.beer, self.store), 24)
        self.assertEqual(self.on_hand(self.beer, self.kitchen), 24)
        self.assertEqual(StockMovement.objects.filter(ref_type="issue").values("ref_id").distinct().count(), 1)
        self.beer.refresh_from_db()
        self.assertEqual(self.beer.avg_cost, 100)
        page = self.c.get(url("issue"))
        self.assertEqual(page.context["history"][0]["dest"], self.kitchen)
        self.assertEqual(page.context["history"][0]["value"], 24 * 100 + 2 * 120)

    @override_settings(ALLOW_NEGATIVE_STOCK=False)
    def test_issue_is_all_or_nothing(self):
        r = self.issue(**self.lines((self.beer, 10), (self.rice, 50), cost=False))      # only 5 kg of rice
        self.assertEqual(r.status_code, 422)
        self.assertContains(r, "Not enough Rice", status_code=422)
        self.assertEqual(self.on_hand(self.beer, self.store), 48)
        self.assertEqual(self.on_hand(self.beer, self.kitchen), 0)

    def test_same_location_and_missing_destination(self):
        self.assertEqual(self.issue(to_location=self.store.pk).status_code, 422)
        self.assertEqual(self.issue(to_location="").status_code, 422)

    def test_write_off_needs_a_reason_and_is_logged_as_waste(self):
        d = {"mode": "writeoff", "from_location": self.store.pk, **self.lines((self.beer, 3), cost=False)}
        self.assertEqual(self.c.post(url("issue"), {**d, "reason": ""}).status_code, 422)
        self.assertEqual(self.on_hand(self.beer, self.store), 48)
        self.assertEqual(self.c.post(url("issue"), {**d, "reason": "Broken crate"}).status_code, 302)
        self.assertEqual(self.on_hand(self.beer, self.store), 45)
        m = StockMovement.objects.get(movement_type=MovementType.WASTE)
        self.assertEqual((m.quantity, m.reason, m.ref_type), (-3, "Broken crate", "writeoff"))


class SalesControlTests(ManagerBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cat = Category.objects.create(name="Drinks")
        cls.tusker = MenuItem.objects.create(name="Tusker", category=cat, price=D("300"), station="BAR", track_stock=True)
        cls.pilau = MenuItem.objects.create(name="Pilau", category=cat, price=D("500"), track_stock=False)
        RecipeLine.objects.create(menu_item=cls.tusker, stock_item=cls.beer, quantity=1)

    def setUp(self):
        super().setUp()
        views.record_movement(item=self.beer, location=self.bar, quantity=10, movement_type="OPENING", unit_cost=100)
        views.create_shift(manager=self.manager, staff=[{"user": self.waiter, "role": "WAITER"},
                                                         {"user": self.cashier, "role": "CASHIER", "terminal": "T1"}])
        self.order = views.open_order(waiter=self.waiter, source="BAR")
        views.add_item(order=self.order, menu_item=self.tusker, user=self.waiter, quantity=2)
        views.add_item(order=self.order, menu_item=self.pilau, user=self.waiter, quantity=1)
        views.send_order(order=self.order, user=self.waiter)

    def item(self, name):
        return self.order.items.get(name=name)

    # ---- voids ----
    def test_void_returns_stock_when_not_made(self):
        self.assertEqual(self.on_hand(self.beer, self.bar), 8)
        self.assertContains(self.c.get(url("voids")), self.order.number)
        r = self.c.post(url("void_item", self.item("Tusker").pk), {"reason": "Guest changed mind", "wasted": "0"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.item("Tusker").status, OrderItem.Status.VOID)
        self.assertEqual(self.on_hand(self.beer, self.bar), 10)
        self.assertEqual(self.item("Tusker").voided_by, self.manager)
        self.assertContains(self.c.get(url("voids")), "Guest changed mind")

    def test_void_as_waste_keeps_stock_deducted(self):
        self.c.post(url("void_item", self.item("Tusker").pk), {"reason": "Dropped", "wasted": "1"})
        self.assertEqual(self.on_hand(self.beer, self.bar), 8)
        self.assertEqual(StockMovement.objects.filter(movement_type=MovementType.WASTE).count(), 1)

    def test_void_needs_a_reason_and_a_manager(self):
        self.c.post(url("void_item", self.item("Tusker").pk), {"reason": "", "wasted": "0"})
        self.assertEqual(self.item("Tusker").status, OrderItem.Status.SENT)
        w = Client()
        w.force_login(self.waiter)
        self.assertEqual(w.post(url("void_item", self.item("Tusker").pk), {"reason": "x"}).status_code, 403)

    def test_billed_order_must_have_its_bill_cancelled_first(self):
        bill = views.generate_bill(order=self.order, user=self.waiter)
        self.assertNotContains(self.c.get(url("voids")) , f'action="{url("void_item", self.item("Pilau").pk)}"')
        self.c.post(url("void_item", self.item("Pilau").pk), {"reason": "x", "wasted": "0"})
        self.assertEqual(self.item("Pilau").status, OrderItem.Status.SENT)
        self.c.post(url("cancel_bill", bill.pk), {"reason": "Wrong items"})
        bill.refresh_from_db()
        self.assertEqual(bill.status, Bill.Status.CANCELLED)
        self.c.post(url("void_item", self.item("Pilau").pk), {"reason": "Cold", "wasted": "0"})
        self.assertEqual(self.item("Pilau").status, OrderItem.Status.VOID)

    # ---- discounts ----
    def bill(self):
        return views.generate_bill(order=self.order, user=self.waiter)       # 2 x 300 + 500 = 1100, VAT inclusive

    def apply(self, bill, **kw):
        d = {"kind": "percent", "value": "10", "reason": "Birthday", "scope": "bill"}
        d.update(kw)
        return self.c.post(url("discount_action", bill.pk, "apply"), d)

    def test_percentage_on_the_whole_bill(self):
        bill = self.bill()
        self.assertEqual(bill.total, D("1100.00"))
        self.assertContains(self.c.get(url("discounts")), bill.number)
        self.apply(bill)
        bill.refresh_from_db()
        self.assertEqual((bill.discount_amount, bill.total), (D("110.00"), D("990.00")))
        self.assertEqual(bill.discount_approved_by, self.manager)
        self.assertEqual(bill.discount_reason, "Birthday")
        self.assertEqual(round(bill.subtotal + bill.tax, 2), D("990.00"))
        self.assertTrue(AuditLog.objects.filter(action="discount_applied", ref_id=str(bill.pk)).exists())
        self.assertContains(self.c.get(url("discounts")), "Birthday")

    def test_amount_and_replacing_an_earlier_discount(self):
        bill = self.bill()
        self.apply(bill, kind="amount", value="200")
        self.apply(bill, kind="amount", value="50", reason="Corrected")
        bill.refresh_from_db()
        self.assertEqual((bill.discount_amount, bill.total), (D("50.00"), D("1050.00")))    # replaced, not stacked

    def test_discount_on_selected_products_only(self):
        bill = self.bill()
        idx = next(i for i, l in enumerate(bill.lines) if l["name"] == "Tusker")
        self.apply(bill, scope="lines", line=[str(idx)], value="50")                          # 50% of the 600 beer line
        bill.refresh_from_db()
        self.assertEqual(bill.discount_amount, D("300.00"))
        self.assertEqual(bill.total, D("800.00"))
        self.assertIn("Tusker", bill.discount_reason)
        self.assertEqual(self.apply(bill, scope="lines", kind="amount", value="700", line=[str(idx)]).status_code, 302)
        bill.refresh_from_db()
        self.assertEqual(bill.discount_amount, D("300.00"))                                   # 700 > 600: refused
        self.apply(bill, scope="lines", value="10")                                           # nothing ticked: refused
        bill.refresh_from_db()
        self.assertEqual(bill.discount_amount, D("300.00"))

    def test_remove_discount(self):
        bill = self.bill()
        self.apply(bill)
        self.c.post(url("discount_action", bill.pk, "remove"), {"reason": "Entered by mistake"})
        bill.refresh_from_db()
        self.assertEqual((bill.discount_amount, bill.total, bill.discount_approved_by), (0, D("1100.00"), None))
        self.assertTrue(AuditLog.objects.filter(action="discount_removed").exists())

    def test_discount_guards(self):
        bill = self.bill()
        for bad in ({"value": "0"}, {"value": "101"}, {"value": "abc"}, {"reason": ""}, {"kind": "amount", "value": "1100.01"}):
            self.apply(bill, **bad)
            bill.refresh_from_db()
            self.assertEqual(bill.discount_amount, 0, bad)
        self.apply(bill, value="100")                                                         # a free bill would be unpayable
        bill.refresh_from_db()
        self.assertEqual(bill.discount_amount, 0)

    def test_cannot_discount_below_what_was_already_paid_or_a_paid_bill(self):
        bill = self.bill()
        views.take_payment(bill=bill, user=self.cashier, method="CASH", amount=D("1000"))
        self.apply(bill, value="20")                                                          # new total 880 < 1000 paid
        bill.refresh_from_db()
        self.assertEqual(bill.discount_amount, 0)
        views.take_payment(bill=bill, user=self.cashier, method="CASH", amount=D("100"))
        self.apply(bill)
        bill.refresh_from_db()
        self.assertEqual((bill.status, bill.discount_amount), ("PAID", 0))
        self.assertNotContains(self.c.get(url("discounts")), bill.number + "</b>")

    def test_waiters_cannot_discount(self):
        bill = self.bill()
        w = Client()
        w.force_login(self.waiter)
        self.assertEqual(w.post(url("discount_action", bill.pk, "apply"), {"kind": "percent", "value": "10", "reason": "x"}).status_code, 403)