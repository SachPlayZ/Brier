"""Landing page: the numbers must come from the evaluation report, never from the template."""
from __future__ import annotations

import json
import re

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from expenses import views
from expenses.views import landing_metrics

REPORT = {
    "meta": {"mode": "simulated", "receipts": 123},
    "extraction": {
        "n_receipts": 123, "arithmetic_ok_rate": 0.8123,
        "fields": [{"field": "vendor", "f1": 0.5551}, {"field": "total", "f1": 0.4442},
                   {"field": "date", "f1": 0.7773}],
        "by_noise_tier": {"0": [{"field": "vendor", "f1": 1.0}], "3": [{"field": "vendor", "f1": 0.6666}]},
        "calibration": {"ece": 0.0987},
    },
    "dedup": None,
}


@pytest.fixture
def report_file(tmp_path, settings):
    """Point the view at a temp BASE_DIR whose data/eval_report.json we control."""
    (tmp_path / "data").mkdir()
    settings.BASE_DIR = tmp_path

    def write(payload):
        (tmp_path / "data" / "eval_report.json").write_text(json.dumps(payload), encoding="utf-8")
    return write


# -------------------------------------------------------------- metrics reader
def test_metrics_are_read_from_the_report(tmp_path):
    p = tmp_path / "r.json"; p.write_text(json.dumps(REPORT), encoding="utf-8")
    m = landing_metrics(p)
    assert (m["receipts"], m["vendor_f1"], m["total_f1"], m["ece"]) == (123, 0.5551, 0.4442, 0.0987)
    assert m["arithmetic_pct"] == 81.2
    assert [r["name"] for r in m["rows"]] == ["vendor", "date", "total"]      # only fields that exist
    vendor = next(r for r in m["rows"] if r["name"] == "vendor")
    assert (vendor["clean"], vendor["photo"]) == (1.0, 0.6666)
    date = next(r for r in m["rows"] if r["name"] == "date")
    assert date["clean"] is None and date["photo"] is None                    # missing stays missing


@pytest.mark.parametrize("payload", [None, {}, {"extraction": {}}, {"extraction": {"fields": []}}])
def test_missing_or_empty_report_yields_no_metrics(tmp_path, payload):
    p = tmp_path / "r.json"
    if payload is not None:
        p.write_text(json.dumps(payload), encoding="utf-8")
    assert landing_metrics(p) is None


def test_corrupt_report_yields_no_metrics(tmp_path):
    p = tmp_path / "r.json"; p.write_text("{not json", encoding="utf-8")
    assert landing_metrics(p) is None


@pytest.mark.parametrize("payload", [
    [1, 2, 3],                                                        # not an object
    {"extraction": {"fields": [{"field": "vendor"}]}},                # row has no f1
    {"extraction": {"fields": [{"f1": 0.9}]}},                        # row has no field
    {"extraction": {"fields": {"vendor": {"f1": 0.9}}}},              # fields is a mapping
    {"extraction": {"fields": [{"field": "vendor", "f1": 0.9}],
                    "by_noise_tier": "oops"}},                        # tiers is a string
])
def test_report_in_an_unexpected_shape_yields_no_metrics(tmp_path, payload):
    """Valid JSON, wrong structure. The landing page is the front door for logged-out
    visitors, so an old or hand-edited report must drop the numbers, not 500."""
    p = tmp_path / "r.json"; p.write_text(json.dumps(payload), encoding="utf-8")
    assert landing_metrics(p) is None


def test_optional_sections_are_omitted_not_invented(tmp_path):
    slim = {"extraction": {"fields": [{"field": "vendor", "f1": 0.9}]}}
    p = tmp_path / "r.json"; p.write_text(json.dumps(slim), encoding="utf-8")
    m = landing_metrics(p)
    assert m["total_f1"] is None and m["ece"] is None and m["arithmetic_pct"] is None


# ------------------------------------------------------------------- the page
def test_landing_renders_the_report_numbers(client, report_file):
    report_file(REPORT)
    html = client.get(reverse("landing")).content.decode()
    assert 'data-count="0.555"' in html and ">0.555<" in html                  # vendor F1
    assert 'data-count="81.2"' in html and "81.2%" in html                     # arithmetic reconcile
    assert "123 synthetic receipts" in html
    assert 'id="accuracy"' in html


def test_landing_without_a_report_drops_the_accuracy_section(client, report_file):
    html = client.get(reverse("landing")).content.decode()                      # no file written
    assert 'id="accuracy"' not in html and 'href="#accuracy"' not in html
    assert "Measured, not promised" not in html
    assert "Read every receipt." in html                                        # the rest still renders


def test_no_hardcoded_measurements_in_the_template():
    text = open(views.__file__.replace("views.py", "templates/expenses/landing.html"), encoding="utf-8").read()
    body = re.sub(r"\{[{%].*?[}%]\}", "", text, flags=re.S)                      # strip template tags
    assert not re.search(r"\b0\.\d{3}\b|\b\d{2}\.\d%", body), "measured numbers must come from the report"


@pytest.mark.django_db
def test_visitors_get_sign_up_and_log_in_everywhere(client, report_file):
    html = client.get(reverse("landing")).content.decode()
    assert html.count(">Sign up<i") == 4 and html.count(">Log in</a>") == 4      # nav, menu, hero, final panel
    assert reverse("signup") in html and reverse("login") in html
    assert "Open dashboard" not in html and "Open my claims" not in html
    assert "Sign in" not in html                                                   # one word per intent


@pytest.mark.django_db
def test_signed_in_employee_gets_a_single_button_to_their_claims(client, report_file):
    User.objects.create_user("someone", password="pw")
    client.login(username="someone", password="pw")
    html = client.get(reverse("landing")).content.decode()
    assert html.count(">Open my claims<i") == 4 and ">Sign up<i" not in html and ">Log in</a>" not in html
    assert f'href="{reverse("claim_list")}"' in html


@pytest.mark.django_db
def test_signed_in_reviewer_gets_the_dashboard(client, report_file):
    from django.contrib.auth.models import Group
    user = User.objects.create_user("boss", password="pw")
    user.groups.add(Group.objects.create(name="finance"))
    client.login(username="boss", password="pw")
    html = client.get(reverse("landing")).content.decode()
    assert html.count(">Open dashboard<i") == 4 and "Open my claims" not in html


def test_landing_copy_has_no_dashes_or_middle_dots(client, report_file):
    report_file(REPORT)
    html = client.get(reverse("landing")).content.decode()
    visible = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert not re.search("[—–·]", visible)


def test_every_preview_ships_a_dark_and_a_light_capture(client, report_file):
    from django.contrib.staticfiles import finders
    html = client.get(reverse("landing")).content.decode()
    paths = set(re.findall(r'/static/(expenses/landing/[\w.-]+\.webp)', html))
    stems = {re.sub(r"-(dark|light)\.webp$", "", p) for p in paths}
    assert len(stems) == 7, stems
    for stem in stems:                                                             # each stem has both themes on disk
        for theme in ("dark", "light"):
            assert finders.find(f"{stem}-{theme}.webp"), f"missing {stem}-{theme}.webp"
    assert html.count('data-theme="dark"') == 7 and html.count('data-theme="light"') == 7
    assert {p.rsplit("-", 1)[0] for p in paths if p.endswith("-dark.webp")} ==            {p.rsplit("-", 1)[0] for p in paths if p.endswith("-light.webp")}


def test_walkthrough_has_four_tabs_starting_with_log_in(client, report_file):
    html = client.get(reverse("landing")).content.decode()
    assert re.findall(r'class="tab-title">([^<]+)<', html) == ["Log in", "Submit", "Extract", "Review"]
    assert 'id="pane-login"' in html


def test_no_stale_single_theme_previews_left_on_disk():
    from pathlib import Path
    folder = Path(views.__file__).parent / "static" / "expenses" / "landing"
    assert all(re.search(r"-(dark|light)\.webp$", f.name) for f in folder.glob("*.webp"))


# ------------------------------------------------------------- block-wave background
def _static(name):
    from pathlib import Path
    return (Path(views.__file__).parent / "static" / "expenses" / name).read_text(encoding="utf-8")


def test_background_draws_shaded_blocks_not_characters():
    js = _static("landing.js")
    assert "fillRect" in js and "SHADE_ALPHA" in js and "SHADE_SIZE" in js
    assert "fillText" not in js                                   # no glyphs: identical in every font
    assert '" .:-=+*#%"' not in js and "RAMP" not in js            # the old punctuation ramp is gone


def test_each_theme_sets_its_own_block_colour():
    css = _static("landing.css")
    assert re.search(r"\.ascii \{[^}]*--block-rgb: 45, 212, 191", css)                      # dark: bright teal
    assert re.search(r'\[data-bs-theme="light"\] \.ascii \{ --block-rgb: 0, 92, 68', css)   # light: vivid green
    assert "opacity: 0.42" not in css                                                       # no per-theme opacity hacks


def test_blocks_are_masked_away_from_the_copy_on_every_screen_size():
    css = _static("landing.css")
    hero = css[css.index(".hero .ascii {"):]
    assert "mask-composite: intersect" in hero                     # desktop: calm left column
    assert "@media (max-width: 719px)" in css and "mask-composite: add" in css   # phones: calm at the top


def test_light_theme_lifts_its_lighter_shades():
    css, js = _static("landing.css"), _static("landing.js")
    assert re.search(r'\[data-bs-theme="light"\] \.ascii \{[^}]*--block-lift: 0\.4', css)   # mid shades darker in light mode
    assert "--block-lift" in js


def test_landing_describes_an_inr_only_product(client):
    """Foreign receipts are rejected at upload, so the page must not advertise multi-currency."""
    html = client.get(reverse("landing")).content.decode()
    assert "Multi-currency" not in html and "currencies are recognised" not in html
    assert not any(sym in html for sym in ("\u20ac", "\u00a3", "\u00a5"))
    assert "Made for Indian tax receipts" in html
    assert all(f"<span>{code}</span>" in html for code in ("GSTIN", "CGST", "SGST", "IGST"))
