"""End-to-end tests of: manager creates shift -> staff PIN login -> waiter orders -> cashier bills each order."""
from decimal import Decimal

from unittest import mock

from django.contrib import admin as django_admin
from django.http import JsonResponse
from django.test import TestCase, override_settings
from django.urls import include, path, reverse
from rest_framework.test import APIClient

from .models import (AuditLog, Category, Location, MenuItem, Order, RecipeLine, Shift, ShiftAssignment, StockItem, Table, User)
from . import views


# Tests bring their own URL layout, so they don't depend on the project's urls.py.
urlpatterns = [path("admin/", django_admin.site.urls), path("api/", include("Chokies.urls")),
               path("pin-login/", views.pin_login), path("", views.pos_page)]
APP = Shift._meta.app_label            # "chokies" or "Chokies", whichever the project uses


def mk_user(username, role, pin="1234", **kw):
    u = User.objects.create(username=username, role=role, first_name=username.title(), **kw)
    u.set_pin(pin)
    u.save()
    return u


@override_settings(ROOT_URLCONF="Chokies.tests",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],   # fast tests
                   REST_FRAMEWORK={"EXCEPTION_HANDLER": "Chokies.api.handlers.exception_handler"})  # stale on purpose
class FlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = mk_user("mary", User.Role.MANAGER, "9999")
        cls.waiter = mk_user("wanjiku", User.Role.WAITER, "1111")
        cls.waiter2 = mk_user("otieno", User.Role.WAITER, "2222")
        cls.cashier = mk_user("chebet", User.Role.CASHIER, "3333")
        cls.off_duty = mk_user("kamau", User.Role.WAITER, "4444")

        Location.objects.create(code="kitchen", name="Kitchen")
        bar = Location.objects.create(code="bar", name="Bar")
        cat_f, cat_d = Category.objects.create(name="Food"), Category.objects.create(name="Drinks")
        cls.pilau = MenuItem.objects.create(name="Pilau", category=cat_f, price=Decimal("500"), track_stock=False)
        cls.tusker = MenuItem.objects.create(name="Tusker", category=cat_d, price=Decimal("300"),
                                             station="BAR", track_stock=True)
        beer = StockItem.objects.create(sku="TUSK", name="Tusker bottle")
        RecipeLine.objects.create(menu_item=cls.tusker, stock_item=beer, quantity=1)
        views.record_movement(item=beer, location=bar, quantity=10, movement_type="OPENING", unit_cost=100)
        cls.t1, cls.t2 = Table.objects.create(name="T1"), Table.objects.create(name="T2")

    # -- helpers --
    def login(self, username, pin):
        c = APIClient()
        r = c.post("/api/auth/pin-login/", {"username": username, "pin": pin}, format="json")
        if r.status_code == 200:
            c.credentials(HTTP_AUTHORIZATION="Bearer " + r.data["token"])
        return c, r

    def start_shift(self):
        c, _ = self.login("mary", "9999")
        r = c.post("/api/shifts/", {"name": "Evening", "staff": [
            {"user": self.waiter.pk, "role": "WAITER"},
            {"user": self.waiter2.pk, "role": "WAITER"},
            {"user": self.cashier.pk, "role": "CASHIER", "terminal": "T1", "opening_float": "1000.00"},
        ]}, format="json")
        assert r.status_code == 201, r.data
        return c, r.data["id"]

    # -- tests --
    def test_pin_login_requires_open_shift(self):
        _, r = self.login("wanjiku", "1111")
        self.assertEqual(r.status_code, 403)               # no shift yet
        _, r = self.login("mary", "9999")
        self.assertEqual(r.status_code, 200)               # managers don't need one
        self.start_shift()
        _, r = self.login("wanjiku", "1111")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["user"]["shift"]["role"], "WAITER")
        _, r = self.login("kamau", "4444")                 # not on the roster
        self.assertEqual(r.status_code, 403)

    def test_only_managers_create_shifts(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        r = w.post("/api/shifts/", {"staff": [{"user": self.off_duty.pk, "role": "WAITER"}]}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_roster_and_lockout(self):
        self.start_shift()
        names = {x["username"] for x in APIClient().get("/api/auth/roster/").data}
        self.assertEqual(names, {"wanjiku", "otieno", "chebet"})
        for _ in range(5):
            _, r = self.login("wanjiku", "0000")
            self.assertEqual(r.status_code, 401)
        _, r = self.login("wanjiku", "1111")               # right PIN, but now locked
        self.assertEqual(r.status_code, 429)

    def test_staff_cannot_be_on_two_open_shifts_or_wrong_role(self):
        mgr, _ = self.start_shift()
        r = mgr.post("/api/shifts/", {"staff": [{"user": self.waiter.pk, "role": "WAITER"}]}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("already on", r.data["detail"])
        r = mgr.post("/api/shifts/", {"staff": [{"user": self.off_duty.pk, "role": "CASHIER"}]}, format="json")
        self.assertEqual(r.status_code, 409)
        # two cashiers, same terminal
        c2 = mk_user("cash2", User.Role.CASHIER)
        r = mgr.post("/api/shifts/", {"staff": [{"user": c2.pk, "role": "CASHIER", "terminal": "T1"}]}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_full_flow_each_order_billed_separately(self):
        mgr, shift_id = self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        cash, _ = self.login("chebet", "3333")

        # waiter opens orders: two on the same table (menu), one at the bar with no table
        o1 = w.post("/api/orders/", {"source": "MENU", "table": self.t1.pk}, format="json")
        o2 = w.post("/api/orders/", {"source": "MENU", "table": self.t1.pk}, format="json")
        o3 = w.post("/api/orders/", {"source": "BAR"}, format="json")
        for r in (o1, o2, o3):
            self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual([o1.data["number"], o2.data["number"], o3.data["number"]],
                         ["ORD-000001", "ORD-000002", "ORD-000003"])   # bar orders share the ORD- series
        self.assertEqual(o3.data["table"], None)
        self.assertEqual(o1.data["shift"], shift_id)

        # menu filtering by source
        self.assertEqual([i["name"] for c in w.get("/api/menu/?source=BAR").data for i in c["items"]], ["Tusker"])
        self.assertEqual([i["name"] for c in w.get("/api/menu/?source=MENU").data for i in c["items"]], ["Pilau"])

        # items + send
        w.post(f"/api/orders/{o1.data['id']}/items/", {"menu_item": self.pilau.pk, "quantity": 2}, format="json")
        w.post(f"/api/orders/{o2.data['id']}/items/", {"menu_item": self.pilau.pk}, format="json")
        w.post(f"/api/orders/{o3.data['id']}/items/", {"menu_item": self.tusker.pk, "quantity": 3}, format="json")
        for o in (o1, o2, o3):
            self.assertEqual(w.post(f"/api/orders/{o.data['id']}/send/", {}, format="json").status_code, 200)

        # another waiter can't touch it; waiter can't bill
        self.assertEqual(w2.post(f"/api/orders/{o1.data['id']}/items/",
                                 {"menu_item": self.pilau.pk}, format="json").status_code, 403)
        self.assertEqual(w2.get(f"/api/orders/{o1.data['id']}/").status_code, 403)
        self.assertEqual(w2.post(f"/api/orders/{o1.data['id']}/bill/", {}, format="json").status_code, 403)  # not theirs
        self.assertEqual(len(w.get("/api/orders/").data), 3)
        self.assertEqual(len(w2.get("/api/orders/").data), 0)
        self.assertEqual(len(cash.get("/api/orders/").data), 3)

        # the WAITER bills and collects their own orders; order 2 on the same table stays open
        b1 = w.post(f"/api/orders/{o1.data['id']}/bill/", {}, format="json")
        self.assertEqual(b1.status_code, 201, b1.data)
        self.assertEqual(b1.data["order_number"], "ORD-000001")
        self.assertEqual(Decimal(b1.data["total"]), Decimal("1000.00"))
        self.assertTrue(b1.data["number"].startswith("WTR-B"))
        self.assertEqual(w2.get(f"/api/bills/{b1.data['id']}/").status_code, 403)
        r = w.post(f"/api/bills/{b1.data['id']}/pay/", {"method": "CASH", "amount": "1000", "tendered": "1200"},
                   format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["change_due"]), Decimal("200"))
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "OCCUPIED")   # o2 still open

        b2 = w.post(f"/api/orders/{o2.data['id']}/bill/", {}, format="json")
        self.assertNotEqual(b1.data["id"], b2.data["id"])
        r = w.post(f"/api/bills/{b2.data['id']}/pay/", {"method": "CARD", "amount": "500", "reference": "SLIP1"},
                   format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "FREE")       # both settled

        # a cashier can bill and collect any order (into the cashier's own drawer)
        b3 = cash.post(f"/api/orders/{o3.data['id']}/bill/", {}, format="json")
        self.assertTrue(b3.data["number"].startswith("T1-B"))
        cash.post(f"/api/bills/{b3.data['id']}/pay/", {"method": "CASH", "amount": "900"}, format="json")
        self.assertEqual(Order.objects.filter(status="PAID").count(), 3)

        # drawers: waiter = cash 1000 (card is not cash); cashier = float 1000 + cash 900
        self.assertEqual(Decimal(w.get("/api/shift/current/").data["drawer"]["expected_cash"]), Decimal("1000.00"))
        self.assertEqual(Decimal(cash.get("/api/shift/current/").data["drawer"]["expected_cash"]), Decimal("1900.00"))

        # waiter with collections can't sign off without counting; then close needs every count
        self.assertEqual(w.post("/api/shift/end/", {}, format="json").status_code, 409)
        r = mgr.post(f"/api/shifts/{shift_id}/close/", {"counts": {str(self.cashier.pk): "1890"}}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("wanjiku", r.data["detail"])
        r = mgr.post(f"/api/shifts/{shift_id}/close/",
                     {"counts": {str(self.cashier.pk): "1890", str(self.waiter.pk): "1000"}}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["status"], "CLOSED")
        var = {s["username"]: Decimal(s["variance"]) for s in r.data["staff"] if s["variance"] is not None}
        self.assertEqual(var, {"chebet": Decimal("-10.00"), "wanjiku": Decimal("0.00")})

        # shift closed -> tokens stop working, no new login
        self.assertEqual(w.get("/api/orders/").status_code, 401)
        self.assertEqual(self.login("wanjiku", "1111")[1].status_code, 403)

    def test_shift_close_blocked_by_unsettled_orders(self):
        mgr, shift_id = self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w.post("/api/orders/", {"source": "MENU"}, format="json")
        r = mgr.post(f"/api/shifts/{shift_id}/close/", {"counts": {str(self.cashier.pk): "1000"}}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("still open", r.data["detail"])
        r = mgr.post(f"/api/shifts/{shift_id}/close/",
                     {"counts": {str(self.cashier.pk): "1000"}, "force": True}, format="json")
        self.assertEqual(r.status_code, 200)

    def test_empty_order_can_be_cancelled_and_frees_table(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        o = w.post("/api/orders/", {"table": self.t2.pk}, format="json")
        self.assertEqual(Table.objects.get(pk=self.t2.pk).status, "OCCUPIED")
        self.assertEqual(w.post(f"/api/orders/{o.data['id']}/cancel/", {}, format="json").status_code, 200)
        self.assertEqual(Table.objects.get(pk=self.t2.pk).status, "FREE")

    def test_logout_invalidates_token(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        self.assertEqual(w.post("/api/auth/logout/").status_code, 204)
        self.assertEqual(w.get("/api/auth/me/").status_code, 401)

    def test_admin_pages_render(self):
        admin = User.objects.create_superuser("root", password="x")
        self.client.force_login(admin)
        self.start_shift()
        for name in ("shift_add", "shift_changelist", "order_changelist", "bill_changelist", "user_changelist"):
            url = reverse(f"admin:{APP}_{name}")
            self.assertEqual(self.client.get(url).status_code, 200, url)
        sid = Shift.objects.get().pk
        self.assertEqual(self.client.get(reverse(f"admin:{APP}_shift_change", args=[sid])).status_code, 200)
        # create a shift through the admin form (inline staff)
        extra = mk_user("w3", User.Role.WAITER)
        data = {"name": "Late", "notes": "", "staff-TOTAL_FORMS": "2", "staff-INITIAL_FORMS": "0",
                "staff-MIN_NUM_FORMS": "0", "staff-MAX_NUM_FORMS": "1000",
                "staff-0-user": extra.pk, "staff-0-role": "WAITER", "staff-0-terminal": "", "staff-0-opening_float": "0",
                "staff-1-user": "", "staff-1-role": "", "staff-1-terminal": "", "staff-1-opening_float": "0"}
        r = self.client.post(reverse(f"admin:{APP}_shift_add"), data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["adminform"].form.errors)
        self.assertTrue(ShiftAssignment.objects.filter(user=extra, shift__name="Late").exists())
        self.assertEqual(Shift.objects.get(name="Late").created_by, admin)
    # -- merging and splitting orders --
    def _order(self, client, table=None, source="MENU", lines=()):
        o = client.post("/api/orders/", {"source": source, **({"table": table.pk} if table else {})}, format="json")
        self.assertEqual(o.status_code, 201, o.data)
        for item, qty in lines:
            r = client.post(f"/api/orders/{o.data['id']}/items/", {"menu_item": item.pk, "quantity": qty}, format="json")
            self.assertEqual(r.status_code, 201, r.data)
        return o.data["id"]

    def test_merge_rounds_into_one_bill(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        cash, _ = self.login("chebet", "3333")
        # the same guests ordered three times during the evening: three orders, one table
        a = self._order(w, self.t1, lines=[(self.pilau, 2)])
        b = self._order(w, self.t1, lines=[(self.pilau, 1)])
        c = self._order(w, self.t1, source="BAR", lines=[(self.tusker, 3)])
        for o in (a, b, c):
            w.post(f"/api/orders/{o}/send/", {}, format="json")

        r = w.post(f"/api/orders/{a}/merge/", {"from": [b, c]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(sorted(i["name"] for i in r.data["items"]), ["Pilau", "Pilau", "Tusker"])
        self.assertEqual(Decimal(r.data["totals"]["total"]), Decimal("2400.00"))      # 3 x 500 + 3 x 300
        self.assertEqual(Order.objects.get(pk=b).status, "MERGED")
        self.assertEqual(Order.objects.get(pk=b).merged_into_id, a)
        self.assertEqual([o["id"] for o in w.get("/api/orders/").data], [a])           # merged ones leave the list

        bill = cash.post(f"/api/orders/{a}/bill/", {}, format="json")
        self.assertEqual(bill.status_code, 201, bill.data)
        self.assertEqual(Decimal(bill.data["total"]), Decimal("2400.00"))
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "OCCUPIED")
        # a merged order is dead: it can't be billed, merged again or edited
        self.assertEqual(cash.post(f"/api/orders/{b}/bill/", {}, format="json").status_code, 409)
        self.assertEqual(w.post(f"/api/orders/{b}/items/", {"menu_item": self.pilau.pk}, format="json").status_code, 409)
        cash.post(f"/api/bills/{bill.data['id']}/pay/", {"method": "CASH", "amount": "2400"}, format="json")
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "FREE")

    def test_merge_rules(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        cash, _ = self.login("chebet", "3333")
        a = self._order(w, self.t1, lines=[(self.pilau, 1)])
        b = self._order(w2, self.t2, lines=[(self.pilau, 1)])
        self.assertEqual(w.post(f"/api/orders/{a}/merge/", {"from": [a]}, format="json").status_code, 409)
        self.assertEqual(w.post(f"/api/orders/{a}/merge/", {"from": []}, format="json").status_code, 409)
        self.assertEqual(w.post(f"/api/orders/{a}/merge/", {"from": [b]}, format="json").status_code, 403)  # not theirs
        self.assertEqual(w.post(f"/api/orders/{a}/merge/", {"from": [9999]}, format="json").status_code, 409)
        self.assertEqual(w.post(f"/api/orders/{a}/merge/", {"from": [a], "version": 0}, format="json").status_code, 409)
        # a stale screen is refused
        r = cash.post(f"/api/orders/{a}/merge/", {"from": [b], "version": 99}, format="json")
        self.assertEqual(r.status_code, 409)
        # a cashier can combine two waiters' orders; the surviving order keeps its table, the other table frees up
        r = cash.post(f"/api/orders/{a}/merge/", {"from": [b]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "OCCUPIED")
        self.assertEqual(Table.objects.get(pk=self.t2.pk).status, "FREE")
        self.assertTrue(AuditLog.objects.filter(action="orders_merged").exists())
        # billed orders can't be merged until the bill is cancelled
        c = self._order(w, self.t1, lines=[(self.pilau, 1)])
        w.post(f"/api/orders/{c}/send/", {}, format="json")
        w.post(f"/api/orders/{a}/send/", {}, format="json")
        cash.post(f"/api/orders/{a}/bill/", {}, format="json")
        r = cash.post(f"/api/orders/{c}/merge/", {"from": [a]}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("billed", r.data["detail"])

    def test_split_order_for_separate_bills(self):
        mgr, _ = self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        cash, _ = self.login("chebet", "3333")
        a = self._order(w, self.t1, lines=[(self.pilau, 2), (self.tusker, 1)])
        w.post(f"/api/orders/{a}/send/", {}, format="json")
        items = {i["name"]: i["id"] for i in w.get(f"/api/orders/{a}/").data["items"]}

        self.assertEqual(w.post(f"/api/orders/{a}/split/", {"items": []}, format="json").status_code, 409)
        self.assertEqual(w.post(f"/api/orders/{a}/split/", {"items": list(items.values())}, format="json").status_code, 409)
        r = w.post(f"/api/orders/{a}/split/", {"items": [items["Tusker"]]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        new = r.data["new_order"]
        self.assertEqual([i["name"] for i in new["items"]], ["Tusker"])
        self.assertEqual(new["table"], self.t1.pk)
        self.assertEqual(new["status"], "SENT")                      # the moved line was already sent
        self.assertEqual([i["name"] for i in r.data["order"]["items"]], ["Pilau"])
        self.assertNotEqual(new["number"], r.data["order"]["number"])

        b1 = cash.post(f"/api/orders/{a}/bill/", {}, format="json")
        b2 = cash.post(f"/api/orders/{new['id']}/bill/", {}, format="json")
        self.assertEqual((Decimal(b1.data["total"]), Decimal(b2.data["total"])), (Decimal("1000.00"), Decimal("300.00")))
        # the sent line keeps its stock booking: voiding it on the NEW order still returns the bottle
        self.assertEqual(mgr.post(f"/api/bills/{b2.data['id']}/cancel/", {"reason": "oops"}, format="json").status_code, 200)
        void = mgr.post(f"/api/orders/{new['id']}/items/{new['items'][0]['id']}/void/", {"reason": "not made"}, format="json")
        self.assertEqual(void.status_code, 200, void.data)
        from .models import StockBalance
        self.assertEqual(StockBalance.objects.get(item__sku="TUSK").quantity, Decimal("10"))

    def test_split_needs_an_unbilled_order_of_your_own(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        cash, _ = self.login("chebet", "3333")
        a = self._order(w, self.t1, lines=[(self.pilau, 1), (self.pilau, 1)])
        w.post(f"/api/orders/{a}/send/", {}, format="json")
        ids = [i["id"] for i in w.get(f"/api/orders/{a}/").data["items"]]
        self.assertEqual(w2.post(f"/api/orders/{a}/split/", {"items": ids[:1]}, format="json").status_code, 403)
        cash.post(f"/api/orders/{a}/bill/", {}, format="json")
        r = w.post(f"/api/orders/{a}/split/", {"items": ids[:1]}, format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("billed", r.data["detail"])

    # -- the waiter's order form: one order, items from both selling points --
    def test_create_order_with_items_from_both_selling_points(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        r = w.post("/api/orders/", {"source": "MENU", "table": self.t1.pk, "notes": "Birthday table",
                                    "items": [{"menu_item": self.pilau.pk, "quantity": 2},
                                              {"menu_item": self.tusker.pk, "quantity": 3}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["source"], "MENU")                      # it started from the restaurant
        self.assertEqual(r.data["notes"], "Birthday table")
        self.assertEqual(r.data["order_type"], "DINE_IN")
        self.assertEqual(sorted((i["name"], i["quantity"]) for i in r.data["items"]), [("Pilau", 2), ("Tusker", 3)])
        self.assertEqual(r.data["totals"]["total"], "1900.00")          # 2 x 500 + 3 x 300
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "OCCUPIED")
        sent = w.post(f"/api/orders/{r.data['id']}/send/", {}, format="json")
        self.assertEqual(sorted(k["station"] for k in sent.data["kots"]), ["BAR", "KITCHEN"])   # one ticket per selling point

    def test_create_order_is_all_or_nothing(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        self.tusker.is_available = False
        self.tusker.save()
        r = w.post("/api/orders/", {"source": "MENU", "table": self.t1.pk,
                                    "items": [{"menu_item": self.pilau.pk}, {"menu_item": self.tusker.pk}]}, format="json")
        self.assertEqual(r.status_code, 409, r.data)
        self.assertEqual(Order.objects.count(), 0)                                  # nothing half-created
        self.assertEqual(Table.objects.get(pk=self.t1.pk).status, "FREE")
        ok = w.post("/api/orders/", {"items": [{"menu_item": self.pilau.pk}]}, format="json")
        self.assertEqual(ok.data["number"], "ORD-000001")                           # and the number wasn't burnt

    def test_room_service_is_inferred_from_the_room_number(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        r = w.post("/api/orders/", {"room_number": "204", "items": [{"menu_item": self.pilau.pk}]}, format="json")
        self.assertEqual((r.status_code, r.data["order_type"], r.data["room_number"]), (201, "ROOM_SERVICE", "204"))

    def test_add_several_items_at_once(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        o = w.post("/api/orders/", {"source": "BAR"}, format="json").data
        lines = {"items": [{"menu_item": self.tusker.pk, "quantity": 2}, {"menu_item": self.pilau.pk}], "version": o["version"]}
        r = w.post(f"/api/orders/{o['id']}/items/bulk/", lines, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(sorted(i["name"] for i in r.data["items"]), ["Pilau", "Tusker"])   # a bar order takes kitchen items too
        self.assertEqual(w.post(f"/api/orders/{o['id']}/items/bulk/", lines, format="json").status_code, 409)  # stale version
        self.assertEqual(w.post(f"/api/orders/{o['id']}/items/bulk/", {"items": []}, format="json").status_code, 400)
        self.assertEqual(w2.post(f"/api/orders/{o['id']}/items/bulk/", {"items": [{"menu_item": self.pilau.pk}]}, format="json").status_code, 403)

    def test_menu_and_me_carry_what_the_form_needs(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        items = {i["name"]: i for c in w.get("/api/menu/").data for i in c["items"]}
        self.assertEqual(items["Pilau"]["station"], "KITCHEN")
        self.assertEqual(items["Tusker"]["station"], "BAR")
        self.assertEqual(str(items["Pilau"]["tax_rate"]), "16.00")
        self.assertIs(w.get("/api/auth/me/").data["prices_include_tax"], True)


class QuantityReceiptAdminTests(FlowTests):
    def paid_order(self, w, qty, method="CASH"):
        o = w.post("/api/orders/", {"source": "MENU"}, format="json").data
        w.post(f"/api/orders/{o['id']}/items/", {"menu_item": self.pilau.pk, "quantity": qty}, format="json")
        w.post(f"/api/orders/{o['id']}/send/", {}, format="json")
        b = w.post(f"/api/orders/{o['id']}/bill/", {}, format="json").data
        w.post(f"/api/bills/{b['id']}/pay/", {"method": method, "amount": b["total"],
                                              "reference": "R1" if method != "CASH" else ""}, format="json")
        return b

    @override_settings(STOCK_AVAILABILITY_MODE="block")   # this test is about the stock limit, whatever the project default
    def test_quantity_capture_merge_and_edit(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        o = w.post("/api/orders/", {}, format="json").data
        url = f"/api/orders/{o['id']}/items/"
        r = w.post(url, {"menu_item": self.pilau.pk, "quantity": 3}, format="json")
        r = w.post(url, {"menu_item": self.pilau.pk, "quantity": 2}, format="json")        # merges: 3 + 2
        self.assertEqual([(i["quantity"]) for i in r.data["items"]], [5])
        iid = r.data["items"][0]["id"]
        self.assertEqual(w.post(url, {"menu_item": self.pilau.pk, "quantity": 0}, format="json").status_code, 400)
        r = w.patch(f"{url}{iid}/", {"quantity": 4}, format="json")
        self.assertEqual((r.status_code, r.data["items"][0]["quantity"]), (200, 4))
        self.assertEqual(r.data["totals"]["total"], "2000.00")
        self.assertEqual(w.patch(f"{url}{iid}/", {"quantity": 501}, format="json").status_code, 409)
        # stock-tracked item can't exceed what's in stock (10 bottles)
        self.assertEqual(w.post(url, {"menu_item": self.tusker.pk, "quantity": 11}, format="json").status_code, 409)
        w.post(f"/api/orders/{o['id']}/send/", {}, format="json")
        self.assertEqual(w.patch(f"{url}{iid}/", {"quantity": 2}, format="json").status_code, 409)   # sent

    def test_receipt(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        cash, _ = self.login("chebet", "3333")
        b = self.paid_order(w, 2)
        r = cash.get(f"/api/bills/{b['id']}/receipt/")          # printing is the cashier's job
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for needle in (b["number"], "ORD-", "2 x Pilau", "1,000.00", "PAID", "Chokies Lounge"):
            self.assertIn(needle, html)
        self.assertEqual(w.get(f"/api/bills/{b['id']}/receipt/").status_code, 403)    # not even their own bill
        self.assertEqual(w2.get(f"/api/bills/{b['id']}/receipt/").status_code, 403)
        from .models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action="bill_printed").exists())

    def test_only_cashier_and_manager_print_receipts(self):
        mgr, _ = self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        cash, _ = self.login("chebet", "3333")
        o = w.post("/api/orders/", {"source": "MENU"}, format="json").data
        w.post(f"/api/orders/{o['id']}/items/", {"menu_item": self.pilau.pk, "quantity": 2}, format="json")
        w.post(f"/api/orders/{o['id']}/send/", {}, format="json")
        # the cashier clears a bill raised from a specific waiter's order
        b = cash.post(f"/api/orders/{o['id']}/bill/", {}, format="json").data
        self.assertEqual(cash.post(f"/api/bills/{b['id']}/pay/", {"method": "CASH", "amount": b["total"]}, format="json").status_code, 201)

        r = w.get(f"/api/bills/{b['id']}/receipt/")
        self.assertEqual(r.status_code, 403)
        self.assertIn("cashier", r.data["detail"])
        r = cash.get(f"/api/bills/{b['id']}/receipt/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("<td>Cashier</td><td class=r>Chebet</td>", r.content.decode())   # who took the payment
        self.assertIn("<td>Served by</td><td class=r>Wanjiku</td>", r.content.decode())   # who served
        self.assertEqual(mgr.get(f"/api/bills/{b['id']}/receipt/").status_code, 200)    # manager may too

    def test_paid_bills_list_for_cashier(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        cash, _ = self.login("chebet", "3333")
        paid_a = self.paid_order(w, 2)                 # collected by the waiter ...
        paid_b = self.paid_order(w2, 1)
        o = w.post("/api/orders/", {"source": "MENU"}, format="json").data          # ... and one still unpaid
        w.post(f"/api/orders/{o['id']}/items/", {"menu_item": self.pilau.pk}, format="json")
        w.post(f"/api/orders/{o['id']}/send/", {}, format="json")
        cash.post(f"/api/orders/{o['id']}/bill/", {}, format="json")

        self.assertEqual(w.get("/api/bills/").status_code, 403)                     # waiters don't get this list
        rows = cash.get("/api/bills/").data
        self.assertEqual([r["id"] for r in rows], [paid_b["id"], paid_a["id"]])    # paid only, newest first
        self.assertEqual({r["waiter_name"] for r in rows}, {"Wanjiku", "Otieno"})
        self.assertEqual([r["printed"] for r in rows], [0, 0])
        self.assertEqual(rows[1]["total"], "1000.00")

        # printing is tracked per bill: two prints of A, none of B
        cash.get(f"/api/bills/{paid_a['id']}/receipt/")
        cash.get(f"/api/bills/{paid_a['id']}/receipt/")
        rows = {r["id"]: r for r in cash.get("/api/bills/").data}
        self.assertEqual(rows[paid_a["id"]]["printed"], 2)
        self.assertEqual(rows[paid_a["id"]]["last_printed_by"], "Chebet")
        self.assertEqual(rows[paid_b["id"]]["printed"], 0)

        # the amount-due bill printed BEFORE payment is not the receipt: after paying, it still shows "not printed"
        open_bill = cash.get(f"/api/orders/{o['id']}/").data["bill_id"]
        cash.get(f"/api/bills/{open_bill}/receipt/")
        total = cash.get(f"/api/bills/{open_bill}/").data["total"]
        cash.post(f"/api/bills/{open_bill}/pay/", {"method": "CASH", "amount": total}, format="json")
        rows = {r["id"]: r for r in cash.get("/api/bills/").data}
        self.assertEqual(rows[open_bill]["printed"], 0)

    def test_order_page_for_an_open_order_previews_the_bill(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        o = w.post("/api/orders/", {"source": "MENU", "table": self.t1.pk,
                                    "items": [{"menu_item": self.pilau.pk, "quantity": 2}]}, format="json").data
        r = w.get(f"/api/orders/{o['id']}/page/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["order"]["number"], o["number"])
        self.assertIsNone(r.data["bill"])                                      # nothing billed yet: totals are a preview
        self.assertEqual([(l["name"], l["quantity"]) for l in r.data["lines"]], [("Pilau", 2)])
        self.assertEqual(r.data["totals"]["total"], "1000.00")
        self.assertEqual(r.data["totals"]["balance"], "1000.00")
        self.assertEqual(r.data["payments"], [])
        self.assertEqual(r.data["business"]["name"], "Chokies Lounge")
        self.assertEqual(w.get(f"/api/orders/{o['id']}/").data["status"], "SENT")   # and the preview changed nothing

    def test_order_page_for_a_paid_order_lists_payments_and_who_took_them(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        cash, _ = self.login("chebet", "3333")
        bill = self.paid_order(w, 2, "CARD")
        order_id = cash.get(f"/api/bills/{bill['id']}/").data["order"]
        for client in (w, cash):                                               # the waiter's own order, or any order for the cashier
            r = client.get(f"/api/orders/{order_id}/page/")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data["bill"]["status"], "PAID")
            self.assertEqual(r.data["bill"]["number"], bill["number"])
            self.assertEqual(r.data["totals"]["paid"], "1000.00")
            self.assertEqual(r.data["totals"]["balance"], "0.00")
            self.assertEqual([(p["method"], p["reference"], p["by"]) for p in r.data["payments"]], [("CARD", "R1", "Wanjiku")])
            self.assertTrue(r.data["lines"][0]["tax"])                         # per-line VAT comes from the billed snapshot

    def test_order_page_is_private_to_the_waiter_who_owns_the_order(self):
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        w2, _ = self.login("otieno", "2222")
        o = w.post("/api/orders/", {"source": "MENU"}, format="json").data
        self.assertEqual(w2.get(f"/api/orders/{o['id']}/page/").status_code, 403)
        self.assertEqual(w.get(f"/api/orders/{o['id']}/page/").status_code, 200)
        self.assertEqual(APIClient().get(f"/api/orders/{o['id']}/page/").status_code, 401)

    def test_admin_totals_summary_and_print(self):
        admin = User.objects.create_superuser("root", password="x")
        self.start_shift()
        w, _ = self.login("wanjiku", "1111")
        self.paid_order(w, 2)                      # 1000 cash
        self.paid_order(w, 1, "CARD")              # 500 card
        self.client.force_login(admin)
        r = self.client.get(reverse(f"admin:{APP}_bill_changelist"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["sales"]["bills"], 2)
        self.assertEqual(Decimal(r.context["sales"]["total"]), Decimal("1500.00"))
        self.assertEqual({m["method"]: Decimal(m["amount"]) for m in r.context["methods"]}, {"CASH": 1000, "CARD": 500})
        self.assertContains(r, "Sales in this view")
        r = self.client.get(reverse(f"admin:{APP}_bill_changelist") + "?created_at__gte=2000-01-01+00:00:00&status__exact=PAID")
        self.assertEqual(r.context["sales"]["bills"], 2)
        base = reverse(f"admin:{APP}_bill_sales_summary")
        for period in ("day", "week", "month", "year"):
            r = self.client.get(base, {"period": period, "from": "2000-01-01", "to": "2100-01-01"})
            self.assertEqual(r.status_code, 200, period)
            self.assertEqual(Decimal(r.context["totals"]["total"]), Decimal("1500.00"))
            self.assertEqual(len(r.context["rows"]), 1)
        csvr = self.client.get(base, {"format": "csv", "from": "2000-01-01", "to": "2100-01-01"})
        self.assertIn("TOTAL,2", csvr.content.decode())
        from .models import Bill
        rc = self.client.get(reverse(f"admin:{APP}_bill_receipt", args=[Bill.objects.first().pk]))
        self.assertContains(rc, "window.print()")


class PageViewTests(FlowTests):
    """The names the project's root urls.py imports (pin_login, pos_page) must exist and work."""

    def test_page_login_view_sets_session_and_api_accepts_it(self):
        self.start_shift()
        c = APIClient()
        r = c.post("/pin-login/", {"username": "wanjiku", "pin": "1111"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("token", r.json())
        self.assertEqual(c.get("/api/orders/").status_code, 200)          # via session cookie
        self.assertEqual(APIClient().post("/pin-login/", {"username": "kamau", "pin": "4444"},
                                          format="json").status_code, 403)
        self.assertEqual(APIClient().post("/pin-login/", {"username": "wanjiku", "pin": "x"},
                                          format="json").status_code, 401)
        # the login PAGE (GET) - force the built-in form so a project template can't interfere
        with mock.patch.object(views, "POS_LOGIN_TEMPLATE", "no-such-template.html"):
            g = APIClient().get("/pin-login/")
            self.assertEqual(g.status_code, 200)
            self.assertContains(g, 'name="pin"')
            bad = APIClient().post("/pin-login/", {"username": "wanjiku", "pin": "0"})
            self.assertEqual(bad.status_code, 401)
            self.assertContains(bad, "Wrong username or PIN", status_code=401)   # form redisplayed
        form = APIClient().post("/pin-login/", {"username": "wanjiku", "pin": "1111", "next": "/pos/"})
        self.assertEqual((form.status_code, form["Location"]), (302, "/pos/"))   # browser form post
        evil = APIClient().post("/pin-login/", {"username": "wanjiku", "pin": "1111", "next": "https://evil.example"})
        self.assertEqual(evil["Location"], "/")                                  # no open redirect

        # pos_page: logged-in session sees its user, anonymous sees None (template stubbed out)
        echo = lambda request, template, ctx=None, **kw: JsonResponse(ctx)
        with mock.patch.object(views, "render", echo):
            self.assertEqual(c.get("/").json()["pos_user"]["username"], "wanjiku")
            self.assertIsNone(APIClient().get("/").json()["pos_user"])
        # closing the shift also kills the browser session
        mgr, sid = self.mgr_and_shift()
        mgr.post(f"/api/shifts/{sid}/close/", {"counts": {str(self.cashier.pk): "1000"}}, format="json")
        self.assertEqual(c.get("/api/orders/").status_code, 401)

    def mgr_and_shift(self):
        mgr, _ = self.login("mary", "9999")
        return mgr, Shift.objects.get().pk



class RenumberBarOrdersTests(TestCase):
    """The management command that moves old BAR- orders into the ORD- series."""

    def test_renumber_bar_orders(self):
        from io import StringIO
        from django.core.management import call_command
        w = mk_user("wanjiku", User.Role.WAITER)
        mk = lambda n, src="MENU": Order.objects.create(number=n, source=src, waiter=w)
        o1, o2 = mk("ORD-000001"), mk("ORD-000002")
        b1, b2 = mk("BAR-000001", "BAR"), mk("BAR-000002", "BAR")
        views.DocumentSequence.next("order-menu", "ORD-")      # counter lags behind: it says 1 ...
        mk("ORD-000003")                                       # ... but 3 already exists, so 3 must be skipped

        out = StringIO()
        call_command("renumber_bar_orders", stdout=out)       # preview: nothing saved
        self.assertEqual(sorted(Order.objects.filter(source="BAR").values_list("number", flat=True)),
                         ["BAR-000001", "BAR-000002"])
        self.assertIn("Preview only", out.getvalue())
        self.assertFalse(AuditLog.objects.filter(action="order_renumbered").exists())

        call_command("renumber_bar_orders", "--apply", stdout=StringIO())
        b1.refresh_from_db(); b2.refresh_from_db()
        self.assertEqual((b1.number, b2.number), ("ORD-000004", "ORD-000005"))   # oldest first, 3 skipped
        self.assertEqual(Order.objects.count(), len(set(Order.objects.values_list("number", flat=True))))
        self.assertEqual(b1.source, "BAR")                                       # still a bar order
        log = AuditLog.objects.get(action="order_renumbered", ref_id=str(b1.pk))
        self.assertEqual((log.details["old_number"], log.details["new_number"]), ("BAR-000001", "ORD-000004"))

        out = StringIO()
        call_command("renumber_bar_orders", "--apply", stdout=out)               # second run: nothing left
        self.assertIn("Nothing to do", out.getvalue())
        self.assertEqual(views.next_order_number(), "ORD-000006")                # new orders carry on after