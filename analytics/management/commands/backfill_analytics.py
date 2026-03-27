from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from analytics.services import sync_all_po_analytics
from analytics.services.procurement import _build_item_payloads
from purchases.models import PurchaseOrder


class Command(BaseCommand):
    help = "Backfill analytics fact tables for existing purchase orders."

    def add_arguments(self, parser):
        parser.add_argument(
            "--po-number",
            dest="po_number",
            help="Backfill a single purchase order by PO number.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Inspect matching purchase orders without writing analytics facts.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        po_number = (options.get("po_number") or "").strip()

        queryset = PurchaseOrder.objects.select_related("vendor", "created_by").prefetch_related(
            "items__product"
        ).filter(
            Q(status=PurchaseOrder.STATUS_CLOSED) | Q(validated_at__isnull=False)
        ).order_by("created_at", "id")

        if po_number:
            queryset = queryset.filter(po_number=po_number)

        purchase_orders = list(queryset)
        inspected_count = len(purchase_orders)
        synced_count = 0
        skipped_count = 0
        failures = []

        mode_label = "DRY RUN" if dry_run else "BACKFILL"
        self.stdout.write(self.style.MIGRATE_HEADING(f"{mode_label}: analytics fact sync"))

        for purchase_order in purchase_orders:
            try:
                if dry_run:
                    would_sync = bool(_build_item_payloads(purchase_order))
                else:
                    result = sync_all_po_analytics(purchase_order)
            except Exception as exc:
                failures.append((purchase_order.po_number, str(exc)))
                self.stderr.write(
                    self.style.ERROR(f"FAILED {purchase_order.po_number}: {exc}")
                )
                continue

            if dry_run:
                if would_sync:
                    synced_count += 1
                    self.stdout.write(
                        self.style.WARNING(f"WOULD SYNC {purchase_order.po_number}")
                    )
                else:
                    skipped_count += 1
                    self.stdout.write(f"SKIP {purchase_order.po_number}")
                continue

            if result.get("purchase_order_fact") is None:
                skipped_count += 1
                self.stdout.write(f"SKIP {purchase_order.po_number}")
            else:
                synced_count += 1
                self.stdout.write(
                    self.style.SUCCESS(f"SYNCED {purchase_order.po_number}")
                )

        self.stdout.write("")
        self.stdout.write(f"Total POs inspected: {inspected_count}")
        self.stdout.write(f"Total POs synced: {synced_count}")
        self.stdout.write(f"Total POs skipped: {skipped_count}")
        self.stdout.write(f"Total failures: {len(failures)}")

        if failures:
            raise CommandError("Analytics backfill completed with failures.")
