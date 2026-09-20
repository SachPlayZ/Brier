"""Self-serve sign-up, and the ownership rules that make open sign-up safe."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import Group, User
from django.urls import reverse

from expenses.models import Claim, Employee
from tests.test_workflow import make_claim

pytestmark = pytest.mark.django_db

GOOD = {"full_name": "Meera  Iyer", "username": "meera", "department": "Sales",
        "password1": "correct-horse-battery-9", "password2": "correct-horse-battery-9"}


def post(client, **overrides):
    return client.post(reverse("signup"), {**GOOD, **overrides})


@pytest.fixture
def finance_user():
    user = User.objects.create_user("finance", password="pw")
    user.groups.add(Group.objects.create(name="finance"))
    return user


# ------------------------------------------------------------------- sign-up
def test_signup_page_renders_with_labels_and_a_login_link(client):
    html = client.get(reverse("signup")).content.decode()
    assert "Create your account" in html and 'for="id_username"' in html
    assert reverse("login") in html and "Have an account?" in html


def test_signup_creates_user_and_linked_employee_and_signs_in(client):
    r = post(client)
    assert r.status_code == 302 and r.url == reverse("claim_submit")
    user = User.objects.get(username="meera")
    assert user.first_name == "Meera" and user.check_password(GOOD["password1"])
    emp = user.employee
    assert (emp.full_name, emp.department) == ("Meera Iyer", "Sales")     # whitespace collapsed
    assert client.get(reverse("claim_submit")).status_code == 200          # already logged in


def test_signup_success_message_names_the_employee_code(client):
    html = client.post(reverse("signup"), GOOD, follow=True).content.decode()
    assert "Welcome to Brier, Meera" in html and "Your employee code is E001" in html


def test_employee_codes_are_sequential_and_match_the_seed_style(client, django_user_model):
    Employee.objects.create(employee_code="E030", full_name="Existing")
    post(client, username="first")
    client.logout()
    post(client, username="second")
    assert User.objects.get(username="first").employee.employee_code == "E031"
    assert User.objects.get(username="second").employee.employee_code == "E032"


def test_signup_never_grants_reviewer_or_staff_rights(client):
    post(client)
    user = User.objects.get(username="meera")
    assert not user.is_staff and not user.is_superuser and not user.groups.exists()
    assert client.get(reverse("review_queue")).status_code == 302            # bounced, not shown


@pytest.mark.parametrize("override,message", [
    ({"password2": "different-password-9"}, "do not match"),
    ({"password1": "12345678", "password2": "12345678"}, "entirely numeric"),
    ({"password1": "short1", "password2": "short1"}, "too short"),
    ({"full_name": ""}, "required"),
    ({"username": "bad name!"}, "valid username"),
])
def test_invalid_signup_is_rejected_with_an_inline_error(client, override, message):
    r = post(client, **override)
    assert r.status_code == 200 and not User.objects.filter(username="meera").exists()
    html = r.content.decode()
    assert message in html and 'class="field-error"' in html and "is-invalid" in html
    assert not Employee.objects.exists()                                    # nothing half-created


def test_username_taken_check_ignores_case(client):
    User.objects.create_user("Meera", password="pw")
    r = post(client)
    assert r.status_code == 200 and "That username is taken." in r.content.decode()


def test_password_too_similar_to_the_username_is_rejected(client):
    r = post(client, password1="meera1234", password2="meera1234")
    assert r.status_code == 200 and "too similar" in r.content.decode()


def test_signed_in_visitors_are_redirected_away_from_signup(client, finance_user):
    User.objects.create_user("emp", password="pw")
    client.login(username="emp", password="pw")
    assert client.get(reverse("signup")).url == reverse("claim_list")
    client.logout(); client.login(username="finance", password="pw")
    assert client.get(reverse("signup")).url == reverse("dashboard")


def test_login_page_links_to_signup(client):
    assert reverse("signup") in client.get(reverse("login")).content.decode()


# ------------------------------------------------- who can see whose claims
@pytest.fixture
def two_employees():
    a = User.objects.create_user("alice", password="pw")
    b = User.objects.create_user("bob", password="pw")
    ea = Employee.objects.create(employee_code="E001", full_name="Alice", user=a)
    eb = Employee.objects.create(employee_code="E002", full_name="Bob", user=b)
    return make_claim("AL-CLAIM-1", ea), make_claim("BO-CLAIM-2", eb, receipt_id="RB2")


def test_employee_sees_only_their_own_claims(client, two_employees):
    client.login(username="alice", password="pw")
    html = client.get(reverse("claim_list")).content.decode()
    assert "AL-CLAIM-1" in html and "BO-CLAIM-2" not in html


def test_scope_all_is_ignored_for_employees(client, two_employees):
    client.login(username="alice", password="pw")
    html = client.get(reverse("claim_list") + "?scope=all").content.decode()
    assert "BO-CLAIM-2" not in html and 'id="f-scope"' not in html                  # the control is hidden too


def test_employee_cannot_open_someone_elses_claim(client, two_employees):
    client.login(username="alice", password="pw")
    assert client.get(reverse("claim_detail", args=["BO-CLAIM-2"])).status_code == 404
    assert client.get(reverse("claim_detail", args=["AL-CLAIM-1"])).status_code == 200


def test_login_with_no_employee_record_sees_no_claims(client, two_employees):
    User.objects.create_user("ghost", password="pw")
    client.login(username="ghost", password="pw")
    html = client.get(reverse("claim_list")).content.decode()
    assert "AL-CLAIM-1" not in html and "BO-CLAIM-2" not in html


def test_reviewer_still_sees_everything(client, two_employees, finance_user):
    client.login(username="finance", password="pw")
    html = client.get(reverse("claim_list") + "?scope=all").content.decode()
    assert "AL-CLAIM-1" in html and "BO-CLAIM-2" in html and 'id="f-scope"' in html
    assert client.get(reverse("claim_detail", args=["BO-CLAIM-2"])).status_code == 200


def test_dashboard_is_reviewer_only(client, two_employees, finance_user):
    client.login(username="alice", password="pw")
    assert client.get(reverse("dashboard")).url == reverse("claim_list")
    client.logout(); client.login(username="finance", password="pw")
    assert client.get(reverse("dashboard")).status_code == 200


def test_sidebar_hides_dashboard_and_review_from_employees(client, two_employees):
    client.login(username="alice", password="pw")
    html = client.get(reverse("claim_list")).content.decode()
    assert "Dashboard" not in html and "Review queue" not in html and "Log out" in html


# ------------------------------------------------------- post-login routing
def test_login_sends_a_reviewer_to_the_dashboard(client, finance_user):
    """LOGIN_REDIRECT_URL is one value for everyone, so the view has to route by role."""
    response = client.post(reverse("login"),
                           {"username": "finance", "password": "pw"})
    assert response.url == reverse("dashboard")


def test_login_sends_an_employee_to_their_claims(client):
    User.objects.create_user("bela", password="pw")
    response = client.post(reverse("login"), {"username": "bela", "password": "pw"})
    assert response.url == reverse("claim_list")


def test_login_still_honours_an_explicit_next(client, finance_user):
    response = client.post(reverse("login") + "?next=" + reverse("review_queue"),
                           {"username": "finance", "password": "pw"})
    assert response.url == reverse("review_queue")


# ----------------------------------------------------------- the front door
def test_root_sends_a_logged_out_visitor_to_the_landing_page(client):
    """`/` is marketing, not a login wall."""
    response = client.get("/")
    assert response.status_code == 302
    assert response.url == reverse("landing")


def test_root_still_works_for_signed_in_users(client, finance_user):
    client.login(username="finance", password="pw")
    assert client.get("/").status_code == 200
    client.logout()
    User.objects.create_user("kiran", password="pw")
    client.login(username="kiran", password="pw")
    assert client.get("/").url == reverse("claim_list")


def test_deep_links_still_ask_a_logged_out_visitor_to_sign_in(client):
    """Only the front door changed. A link to a real page still routes through login."""
    for name in ("claim_list", "review_queue", "claim_submit"):
        response = client.get(reverse(name))
        assert response.status_code == 302
        assert response.url.startswith(reverse("login")), f"{name} -> {response.url}"
