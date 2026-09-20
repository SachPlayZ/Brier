"""Create demo logins: one finance reviewer and a few employees."""
from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand

from expenses.models import Employee

FINANCE_GROUP = "finance"


class Command(BaseCommand):
    help = "Create the finance group and demo users (password: demo12345)."

    def add_arguments(self, parser):
        parser.add_argument("--password", default="demo12345")

    def handle(self, *args, **options):
        password = options["password"]
        finance, _ = Group.objects.get_or_create(name=FINANCE_GROUP)

        reviewer, created = User.objects.get_or_create(
            username="finance", defaults={"is_staff": True, "email": "finance@example.com"})
        reviewer.set_password(password)
        reviewer.is_staff = True
        reviewer.save()
        reviewer.groups.add(finance)
        self.stdout.write(f"  finance reviewer: finance / {password}")

        linked = 0
        for employee in Employee.objects.all()[:5]:
            user, _ = User.objects.get_or_create(
                username=employee.employee_code.lower(),
                defaults={"email": f"{employee.employee_code.lower()}@example.com"})
            user.set_password(password)
            user.save()
            employee.user = user
            employee.save(update_fields=["user"])
            linked += 1
        self.stdout.write(f"  linked {linked} employee logins (e001 .. / {password})")

        if not User.objects.filter(is_superuser=True).exists():
            User.objects.create_superuser("admin", "admin@example.com", password)
            self.stdout.write(f"  superuser: admin / {password}")
        self.stdout.write(self.style.SUCCESS("Demo users ready."))
