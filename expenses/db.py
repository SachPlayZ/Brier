"""SQLite tuning applied to every new connection.

SQLite's defaults are wrong for a server that both reads and writes on several
threads: the rollback journal makes readers block writers, and the five-second
lock timeout is short enough that one slow request fails the next.
"""
from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    with connection.cursor() as cursor:
        # WAL lets readers continue while a write is in flight.
        cursor.execute("PRAGMA journal_mode=WAL;")
        # Wait for a lock rather than failing instantly.
        cursor.execute("PRAGMA busy_timeout=30000;")
        # Durable enough for this workload, far fewer fsyncs.
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
