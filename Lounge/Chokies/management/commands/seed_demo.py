"""python manage.py seed_demo  -- idempotent demo data: staff, tables, menu, stock, an open shift, and the
manager back-office activity (suppliers, purchases, LPOs, issues, stock takes, orders, voids, discounts)."""
from datetime import timedelta
from decimal import Decimal as D

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from Chokies import manager_views as mv
from Chokies import views
from Chokies.models import (Category, DomainError, GoodsReceipt, Location, MenuItem, Modifier, PurchaseOrder, RecipeLine,
                            Shift, ShiftAssignment, StockItem, StockMovement, StockTake, Supplier, Table, User)

STAFF = [  # username, role, pin, first name, extra
    ("manager1", "MANAGER", "9999", "Mary", {"is_staff": True}),
    ("waiter1", "WAITER", "1111", "Wanjiku", {}),
    ("waiter2", "WAITER", "2222", "Otieno", {}),
    ("cashier1", "CASHIER", "3333", "Chebet", {}),
    ("store1", "STOREKEEPER", "5555", "Kamau", {}),
]
TABLES = [(f"T{i}", c) for i, c in enumerate([2, 2, 4, 4, 4, 4, 6, 6, 8, 8], 1)] + \
         [(f"Patio {i}", 4) for i in range(1, 5)] + [("VIP 1", 8), ("VIP 2", 10)]
MODS = [("Extra cheese", 100), ("No onions", 0), ("Extra spicy", 0), ("Extra sauce", 50)]
# sku, name, stock unit, recipe unit, recipe units per stock unit, cost per stock unit, opening qty, station,
# purchase unit, stock units per purchase unit   (the last two only matter when the item is first created)
STOCK = [("TUSK", "Tusker Lager 500ml", "bottle", "bottle", 1, 190, 120, "BAR", "crate", 24), ("MALT", "Tusker Malt 500ml", "bottle", "bottle", 1, 210, 60, "BAR", "crate", 24),
         ("GUIN", "Guinness 500ml", "bottle", "bottle", 1, 230, 48, "BAR", "crate", 24), ("HEIN", "Heineken 330ml", "bottle", "bottle", 1, 250, 48, "BAR", "crate", 24),
         ("WCAP", "White Cap 500ml", "bottle", "bottle", 1, 200, 60, "BAR", "crate", 24), ("SODA", "Soda 300ml", "bottle", "bottle", 1, 60, 96, "BAR", "crate", 24),
         ("WATR", "Water 500ml", "bottle", "bottle", 1, 40, 96, "BAR", "pack", 12), ("JWBL", "Johnnie Walker Black 750ml", "bottle", "ml", 750, 5200, 8, "BAR", "carton", 12),
         ("JAME", "Jameson 750ml", "bottle", "ml", 750, 3800, 8, "BAR", "carton", 12), ("SMIR", "Smirnoff 750ml", "bottle", "ml", 750, 1900, 10, "BAR", "carton", 12),
         ("GORD", "Gordon's Gin 750ml", "bottle", "ml", 750, 2200, 8, "BAR", "carton", 12), ("WINE", "House Red Wine 750ml", "bottle", "ml", 750, 1500, 12, "BAR", "case", 6),
         ("CHKN", "Chicken (portions)", "portion", "portion", 1, 280, 60, "KITCHEN", "portion", 1), ("BEEF", "Beef (portions)", "portion", "portion", 1, 320, 60, "KITCHEN", "portion", 1)]
# category, name, price, station, [(sku, recipe qty)], [modifiers]
MENU = [
    ("Starters", "Samosas (3)", 350, "KITCHEN", [], []), ("Starters", "Chicken Wings", 650, "KITCHEN", [("CHKN", 1)], ["Extra spicy", "Extra sauce"]),
    ("Starters", "Kachumbari Salad", 300, "KITCHEN", [], ["No onions"]), ("Starters", "Soup of the Day", 400, "KITCHEN", [], []),
    ("Mains", "Nyama Choma (½ kg)", 1200, "KITCHEN", [("BEEF", 1)], ["Extra spicy"]), ("Mains", "Chicken Pilau", 650, "KITCHEN", [("CHKN", 1)], []),
    ("Mains", "Beef Stew & Ugali", 600, "KITCHEN", [("BEEF", 1)], []), ("Mains", "Tilapia Fry", 950, "KITCHEN", [], []),
    ("Mains", "Chicken Curry & Rice", 750, "KITCHEN", [("CHKN", 1)], ["Extra spicy"]), ("Mains", "Burger & Fries", 850, "KITCHEN", [("BEEF", 1)], ["Extra cheese", "No onions"]),
    ("Mains", "Mixed Grill Platter", 2400, "KITCHEN", [("BEEF", 1), ("CHKN", 1)], []),
    ("Sides", "Chips", 300, "KITCHEN", [], []), ("Sides", "Ugali", 100, "KITCHEN", [], []), ("Sides", "Chapati", 80, "KITCHEN", [], []), ("Sides", "Sukuma Wiki", 150, "KITCHEN", [], []),
    ("Desserts", "Fruit Platter", 400, "KITCHEN", [], []), ("Desserts", "Ice Cream", 350, "KITCHEN", [], []), ("Desserts", "Cheesecake", 500, "KITCHEN", [], []),
    ("Soft Drinks", "Soda", 150, "BAR", [("SODA", 1)], []), ("Soft Drinks", "Mineral Water", 120, "BAR", [("WATR", 1)], []),
    ("Soft Drinks", "Fresh Juice", 400, "BAR", [], []), ("Soft Drinks", "Coffee", 250, "BAR", [], []),
    ("Beers", "Tusker Lager", 350, "BAR", [("TUSK", 1)], []), ("Beers", "Tusker Malt", 380, "BAR", [("MALT", 1)], []), ("Beers", "Guinness", 400, "BAR", [("GUIN", 1)], []),
    ("Beers", "Heineken", 450, "BAR", [("HEIN", 1)], []), ("Beers", "White Cap", 350, "BAR", [("WCAP", 1)], []),
    ("Spirits & Wine", "Johnnie Walker Black (tot)", 500, "BAR", [("JWBL", 25)], []), ("Spirits & Wine", "Jameson (tot)", 450, "BAR", [("JAME", 25)], []),
    ("Spirits & Wine", "Smirnoff (tot)", 300, "BAR", [("SMIR", 25)], []), ("Spirits & Wine", "Gordon's Gin (tot)", 300, "BAR", [("GORD", 25)], []),
    ("Spirits & Wine", "House Red (glass)", 700, "BAR", [("WINE", 150)], []),
    ("Cocktails", "Dawa", 700, "BAR", [("SMIR", 50)], []), ("Cocktails", "Mojito", 800, "BAR", [], []), ("Cocktails", "Margarita", 850, "BAR", [], []),
]
SUPPLIERS = [("Kenya Breweries Distributors", "0712 345 678"), ("Wines & Spirits Wholesalers", "0733 456 789"),
             ("Fresh Meats Ltd", "0722 111 222"), ("Naivas Cash & Carry", "0700 987 654")]


class Command(BaseCommand):
    help = "Create demo staff, tables, menu, stock, an open shift and manager back-office activity (safe to run repeatedly)."

    def handle(self, *a, **o):
        st = getattr(settings, "STATION_LOCATIONS", {"KITCHEN": "kitchen", "BAR": "bar"})
        loc = {k: Location.objects.get_or_create(code=v, defaults={"name": v.title()})[0] for k, v in st.items()}
        self.store = Location.objects.get_or_create(code="store", defaults={"name": "Main store"})[0]

        users, shown = {}, []
        for username, role, pin, first, extra in STAFF:
            u, new = User.objects.get_or_create(username=username, defaults={"role": role, "first_name": first, **extra})
            if new or not u.pin_hash:
                u.role = role
                u.set_pin(pin)
                u.save()
                shown.append((username, role, pin))
            users[username] = u

        for name, cap in TABLES:
            Table.objects.get_or_create(name=name, defaults={"capacity": cap})
        mods = {n: Modifier.objects.get_or_create(name=n, defaults={"price_delta": D(p)})[0] for n, p in MODS}

        items = {}
        for sku, name, unit, runit, per, cost, qty, station, punit, pper in STOCK:
            it, _ = StockItem.objects.get_or_create(sku=sku, defaults=dict(
                name=name, category=station.title(), stock_unit=unit, recipe_unit=runit, recipe_units_per_stock_unit=per,
                purchase_unit=punit, stock_units_per_purchase_unit=pper, reorder_level=qty // 5))
            if it.on_hand() == 0:
                views.record_movement(item=it, location=loc[station], quantity=qty, unit_cost=cost,
                                      movement_type="OPENING", user=users["manager1"], reason="Demo opening stock")
            items[sku] = it

        for order, (cat, name, price, station, recipe, mod_names) in enumerate(MENU):
            c, _ = Category.objects.get_or_create(name=cat, defaults={"sort_order": len(Category.objects.all())})
            m, new = MenuItem.objects.get_or_create(name=name, category=c, defaults={
                "price": D(price), "station": station, "track_stock": bool(recipe)})
            if new:
                for sku, q in recipe:
                    RecipeLine.objects.create(menu_item=m, stock_item=items[sku], quantity=D(q))
                m.modifiers.set([mods[n] for n in mod_names])

        crew = [users["waiter1"], users["waiter2"], users["cashier1"]]
        busy = ShiftAssignment.objects.filter(user__in=crew, ended_at__isnull=True, shift__status="OPEN").exists()
        if busy:
            self.stdout.write("An open shift already includes the demo staff; left it alone.")
        else:
            views.create_shift(manager=users["manager1"], name="Demo shift", staff=[
                {"user": users["waiter1"], "role": "WAITER"}, {"user": users["waiter2"], "role": "WAITER"},
                {"user": users["cashier1"], "role": "CASHIER", "terminal": "T1", "opening_float": 5000}])
            self.stdout.write("Opened 'Demo shift' (waiter1, waiter2, cashier1 on terminal T1, float 5,000).")

        self.stdout.write(self.style.SUCCESS(
            f"Menu {MenuItem.objects.count()} items, {Table.objects.count()} tables, {StockItem.objects.count()} stock items."))
        for u, r, p in shown:
            self.stdout.write(f"  new user  {u:10} {r:12} PIN {p}")

        # ---- manager back office ------------------------------------------------------------------
        self.items, self.users, self.mgr, self.loc = items, users, users["manager1"], loc
        self.cost = {r[0]: D(r[5]) for r in STOCK}
        self.purchasing()
        self.issues()
        self.stock_takes()
        self.sales()

    # ------------------------------------------------------------------ helpers
    def say(self, msg):
        self.stdout.write(msg)

    def line(self, sku, qty, markup=1):
        """(item, qty in purchase units, cost per purchase unit) for the purchase forms."""
        item = self.items[sku]
        return item, D(qty), (self.cost[sku] * item.stock_units_per_purchase_unit * D(markup)).quantize(D("0.01"))

    def backdate(self, receipt, days):
        when = timezone.now() - timedelta(days=days)
        GoodsReceipt.objects.filter(pk=receipt.pk).update(created_at=when)
        PurchaseOrder.objects.filter(pk=receipt.po_id).update(created_at=when)

    # ------------------------------------------------------------------ purchasing: suppliers, direct purchases, LPOs
    def purchasing(self):
        if Supplier.objects.filter(name=SUPPLIERS[0][0]).exists():
            return self.say("Purchasing demo data already there; left alone.")
        mgr, store, L = self.mgr, self.store, self.line
        try:
            with transaction.atomic():
                sup = {n: Supplier.objects.create(name=n, phone=p) for n, p in SUPPLIERS}
                kbd, wsw, meats, naivas = (sup[n] for n, _ in SUPPLIERS)
                # direct purchases: bought and received at once, into the store
                for days, who, lines, inv in (
                        (9, naivas, [L("SODA", 3), L("WATR", 2)], "NCC-90871"),
                        (4, meats, [L("CHKN", 40), L("BEEF", 30)], "FM-4411"),
                        (1, naivas, [L("SODA", 1)], "NCC-91002")):
                    self.backdate(mv.direct_purchase(supplier=who, location=store, user=mgr, lines=lines, invoice_no=inv), days)
                # LPOs in every status
                mv.create_lpo(supplier=kbd, user=mgr, lines=[L("GUIN", 3), L("WCAP", 5)], notes="Weekend restock", send=False)       # draft
                mv.create_lpo(supplier=wsw, user=mgr, lines=[L("JAME", 2), L("GORD", 2)], notes="Deliver before Friday", send=True)  # sent
                part = mv.create_lpo(supplier=kbd, user=mgr, lines=[L("TUSK", 10), L("MALT", 4)], send=True)                         # part-delivered
                tusk = part.lines.get(item=self.items["TUSK"])
                self.backdate(mv.receive_lpo(po=part, location=store, user=mgr, rows=[(tusk.pk, D(6), None)], invoice_no="KBD-7781"), 2)
                full = mv.create_lpo(supplier=wsw, user=mgr, lines=[L("SMIR", 1)], send=True)                                       # received, invoice price a bit higher
                self.backdate(mv.receive_lpo(po=full, location=store, user=mgr, invoice_no="WSW-2210",
                                             rows=[(l.pk, l.qty_ordered, (l.unit_cost * D("1.03")).quantize(D("0.01"))) for l in full.lines.all()]), 6)
                gone = mv.create_lpo(supplier=naivas, user=mgr, lines=[L("WATR", 10)], send=True)                                   # cancelled
                mv.cancel_lpo(po=gone, user=mgr, reason="Supplier out of stock")
            self.say("Added 4 suppliers, 3 direct purchases and 5 LPOs (draft, sent, part-delivered, received, cancelled).")
        except DomainError as e:
            self.say(f"Purchasing demo skipped: {e}")

    # ------------------------------------------------------------------ issue stock + write-off
    def issues(self):
        if StockMovement.objects.filter(ref_type="issue").exists():
            return self.say("Issue-stock demo data already there; left alone.")
        it, mgr, bar, kit = self.items, self.mgr, self.loc["BAR"], self.loc["KITCHEN"]
        try:
            with transaction.atomic():
                mv.issue_stock(from_location=self.store, to_location=bar, user=mgr, note="Bar restock", lines=[
                    (it["TUSK"], D(48)), (it["SODA"], D(48)), (it["WATR"], D(12)), (it["SMIR"], D(6))])
                mv.issue_stock(from_location=self.store, to_location=kit, user=mgr, note="Kitchen daily issue", lines=[
                    (it["CHKN"], D(20)), (it["BEEF"], D(15))])
                mv.write_off_stock(location=bar, user=mgr, reason="Bottles broken at delivery", lines=[(it["TUSK"], D(3))])
            self.say("Issued stock from the store to the bar and kitchen, and wrote off 3 broken bottles.")
        except DomainError as e:
            self.say(f"Issue-stock demo skipped (the store is short of stock): {e}")

    # ------------------------------------------------------------------ stock takes
    def stock_takes(self):
        if StockTake.objects.exists():
            return self.say("Stock takes already exist; left alone.")
        it, mgr = self.items, self.mgr
        with transaction.atomic():
            done = views.start_stock_take(location=self.loc["KITCHEN"], user=mgr, items=[it["CHKN"], it["BEEF"]], blind=True)
            for sku, delta in (("CHKN", -1), ("BEEF", 0)):               # one chicken portion short, beef is right
                views.record_count(take=done, item=it[sku], counted=done.lines.get(item=it[sku]).expected + delta)
            views.approve_stock_take(take=done, approver=mgr)
            StockTake.objects.filter(pk=done.pk).update(created_at=timezone.now() - timedelta(days=3))
            live = views.start_stock_take(location=self.loc["BAR"], user=mgr, blind=True,
                                          items=[it["TUSK"], it["WCAP"], it["GUIN"], it["JAME"]])   # still being counted
            views.record_count(take=live, item=it["TUSK"], counted=live.lines.get(item=it["TUSK"]).expected - 2)
            views.record_count(take=live, item=it["WCAP"], counted=live.lines.get(item=it["WCAP"]).expected)
        self.say("Added one approved stock take (kitchen) and one still being counted (bar).")

    # ------------------------------------------------------------------ orders, bills, voids, discounts
    def sales(self):
        shift = Shift.objects.filter(name="Demo shift", status="OPEN").first()
        if shift is None:
            return self.say("No open 'Demo shift', so the demo orders, voids and discounts were skipped.")
        if shift.orders.exists():
            return self.say("The demo shift already has orders; left alone.")
        w, c, mgr = self.users["waiter1"], self.users["cashier1"], self.mgr
        menu, tables = {m.name: m for m in MenuItem.objects.all()}, {t.name: t for t in Table.objects.all()}

        def order(lines, table=None, source="MENU", send=True):
            o = views.open_order(waiter=w, source=source, table=tables.get(table))
            for name, qty in lines:
                views.add_item(order=o, menu_item=menu[name], user=w, quantity=qty)
            if send:
                views.send_order(order=o, user=w)
            return o

        def paid(o, method, ref=""):
            b = views.generate_bill(order=o, user=w)
            views.take_payment(bill=b, user=c, method=method, amount=b.total, reference=ref,
                               tendered=b.total if method == "CASH" else None)

        try:
            with transaction.atomic():
                paid(order([("Chicken Pilau", 2), ("Soda", 2)], "T1"), "CASH")
                paid(order([("Nyama Choma (½ kg)", 1), ("Tusker Lager", 3)], "T2"), "MOBILE_MONEY", "QGH7K2LMNP")
                paid(order([("Guinness", 2), ("Smirnoff (tot)", 2)], source="BAR"), "CARD", "SLIP-30291")
                # unpaid bills for the Discounts page; two already carry a manager discount
                views.generate_bill(order=order([("Chicken Curry & Rice", 3), ("White Cap", 4)], "T3"), user=w)
                b2 = views.generate_bill(order=order([("Chicken Pilau", 4), ("Jameson (tot)", 4), ("Soda", 4)], "T4"), user=w)
                b3 = views.generate_bill(order=order([("Nyama Choma (½ kg)", 2), ("Tusker Lager", 6)], "T5"), user=w)
                mv.apply_bill_discount(bill=b2, user=mgr, reason="Birthday table", percent=D("10"))
                beer = next(n for n, l in enumerate(b3.lines) if l["name"] == "Tusker Lager")
                mv.apply_bill_discount(bill=b3, user=mgr, reason="Regular customer: drinks only", percent=D("15"), line_indexes=[beer])
                # open orders with sent items for the Voids page, and two voids already on record
                order([("Beef Stew & Ugali", 1), ("Guinness", 2)], "T6")
                o = order([("Chicken Wings", 1), ("Tusker Lager", 2), ("Jameson (tot)", 2)], source="BAR")
                views.void_item(item=o.items.get(name="Chicken Wings"), user=mgr, reason="Guest left before it was cooked")
                views.void_item(item=o.items.get(name="Jameson (tot)"), user=mgr, reason="Poured the wrong measure", wasted=True)
                order([("Nyama Choma (½ kg)", 1)], send=False)   # an unsent draft; the Voids page ignores it
            self.say("Added 3 paid orders, 3 unpaid bills (2 discounted), open orders and 2 voids.")
        except (DomainError, KeyError) as e:
            self.say(f"Demo orders skipped: {e!r} (a menu item or stock level differs from the demo data)")