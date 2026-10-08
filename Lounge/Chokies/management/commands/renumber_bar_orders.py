"""One-off clean-up: move the old BAR-xxxxxx orders into the shared ORD- series.

    python manage.py renumber_bar_orders            # preview only, changes nothing
    python manage.py renumber_bar_orders --apply    # do it

Each BAR- order gets the next ORD- number that is not used by any other order (oldest first, so the
relative order of the renamed orders is kept). Running it again finds nothing left to do.

What it touches / leaves alone
  * Order.number is the only column changed. Bills keep their own numbers; receipts reprinted later
    show the new order number.
  * Every rename is written to the append-only audit log (action "order_renumbered", old + new number),
    so the old number can always be looked up.
  * The stock ledger is NOT edited (its rows are immutable by design). Past sale rows keep the old
    number in their free-text "reason"; they stay linked to the right order through ref_id.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from Chokies.models import Order, audit
from Chokies.views import next_order_number


class Command(BaseCommand):
    help = "Renumber existing BAR- orders into the shared ORD- series (preview unless --apply is given)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true",
                            help="Really rename the orders. Without this flag nothing is saved.")

    def handle(self, *args, **opts):
        old_orders = list(Order.objects.filter(number__startswith="BAR-").order_by("created_at", "id"))
        if not old_orders:
            self.stdout.write("No BAR- orders found. Nothing to do.")
            return

        renamed = []
        with transaction.atomic():
            for order in old_orders:
                new = next_order_number()
                while Order.objects.filter(number=new).exists():   # never reuse a number that is taken
                    new = next_order_number()
                old, order.number = order.number, new
                order.save(update_fields=["number", "updated_at"])
                audit(None, "order_renumbered", order, old_number=old, new_number=new)
                renamed.append((old, new, order.get_status_display()))
            if not opts["apply"]:
                transaction.set_rollback(True)   # same code path as a real run, but nothing is kept

        for old, new, status in renamed:
            self.stdout.write(f"  {old}  ->  {new}   ({status})")
        if opts["apply"]:
            self.stdout.write(self.style.SUCCESS(f"Renumbered {len(renamed)} order(s)."))
        else:
            self.stdout.write(self.style.WARNING(
                f"Preview only: {len(renamed)} order(s) would be renamed. Re-run with --apply to save."))