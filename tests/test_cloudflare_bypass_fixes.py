"""Unit tests for the four CloudflareBypass fixes."""
import pytest

from src.scrapers.bypass.cloudflare import evaluate_challenge_status


def test_evaluate_promotes_solved_page():
    html = "<html><body>Welcome to the forum</body></html>"
    assert evaluate_challenge_status(403, html, "BobIsTheOilGuy Forums") == 200


def test_evaluate_keeps_status_when_still_challenged():
    html = "<html>Enable JavaScript and cookies to continue</html>"
    assert evaluate_challenge_status(403, html, "Just a moment...") == 403


def test_evaluate_keeps_status_on_cf_marker():
    html = "<div class='cf-challenge'>checking</div>"
    assert evaluate_challenge_status(403, html, "Attention Required!") == 403


def test_evaluate_passes_through_200():
    assert evaluate_challenge_status(200, "<html>ok</html>", "Forum") == 200
