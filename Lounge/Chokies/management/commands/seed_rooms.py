"""Add sample rooms so room booking can be tried out.

    python manage.py seed_rooms

Safe to run more than once: rooms are matched by name, so running it again updates their rates / status instead of
adding duplicates, and it never deletes anything (existing bookings are untouched).

A meal plan with no rate is "not offered" for that room (BO = room only, BNB = bed & breakfast, HB = half board,
FB = full board). One room is left in MAINTENANCE on purpose so you can see an out-of-service room being refused.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from Chokies.models import Room

# (name, room_type, rate_bo, rate_bnb, rate_hb, rate_fb) - None = plan not offered
ROOMS = [
    ("Std Room 001", "Standard", 2500, 3500, None, None),
    ("Std Room 002", "Standard", 2500, 3500, None, None),
    ("Std Room 003", "Standard", 2500, 3500, None, None),
    ("Std Room 004", "Standard", 2500, 3500, None, None),
    ("Studio 001", "Studio", 3000, 4500, 5500, None),
    ("Studio 002", "Studio", 3000, 4500, 5500, None),
    ("DELUXE 001", "Executive", 4000, 5500, 7000, 8500),
    ("DELUXE 002", "Executive", 4000, 5500, 7000, 8500),
    ("DELUXE 003", "Executive", 4000, 5500, 7000, 8500),
    ("Family Suite 001", "Family Suite", 6000, 8000, 10000, 12000),
]
OUT_OF_SERVICE = ("Std Room 005", "Standard", 2500, 3500, None, None)


class Command(BaseCommand):
    help = "Add (or refresh) sample rooms for trying out room booking."

    @transaction.atomic
    def handle(self, *args, **opts):
        made = updated = 0
        for row, status in [(r, Room.Status.AVAILABLE) for r in ROOMS] + [(OUT_OF_SERVICE, Room.Status.MAINTENANCE)]:
            name, room_type, bo, bnb, hb, fb = row
            rates = {"rate_bo": bo, "rate_bnb": bnb, "rate_hb": hb, "rate_fb": fb}
            rates = {k: v for k, v in rates.items() if v is not None}          # leave un-offered plans at their default
            room, created = Room.objects.get_or_create(name=name, defaults={"room_type": room_type, "status": status, **rates})
            if created:
                made += 1
                continue
            for k, v in {"room_type": room_type, **rates}.items():
                setattr(room, k, v)
            room.save()                    # status is left alone: don't undo a check-out / housekeeping change
            updated += 1
        self.stdout.write(self.style.SUCCESS(f"Rooms: {made} added, {updated} already there (rates refreshed)."))