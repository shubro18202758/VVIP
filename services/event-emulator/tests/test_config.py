"""Config tests: deck-origin parsing fails closed; flags and seeds parse."""

from __future__ import annotations

import pytest

from event_emulator.config import get_settings, parse_deck_origins, parse_flag


class TestDeckOrigins:
    @pytest.mark.parametrize("raw, expected", [
        ("http://127.0.0.1:5273", ("http://127.0.0.1:5273",)),
        ("https://deck.example.org", ("https://deck.example.org",)),
        ("http://127.0.0.1:5273, https://deck.example.org/",
         ("http://127.0.0.1:5273", "https://deck.example.org")),
    ])
    def test_accepted(self, raw, expected):
        assert parse_deck_origins(raw) == expected

    @pytest.mark.parametrize("raw", [
        "*",
        "http://x:1/path",
        "ftp://x",
        "http://u:p@x:1",
        " , ",
        "http://h:notaport",
        "http://h:99999",
        "http://h:-1",
        "http://h:",
        "http://h:0",
        "http://127.0.0.1:5273,*",
    ])
    def test_refused(self, raw):
        with pytest.raises(ValueError):
            parse_deck_origins(raw)


class TestEnv:
    @pytest.mark.parametrize("raw, value", [
        ("1", True), ("true", True), ("on", True), ("0", False), ("", False),
        (None, False), ("no", False),
    ])
    def test_flag(self, raw, value):
        assert parse_flag(raw) is value

    def test_living_city_and_seed(self, monkeypatch):
        monkeypatch.setenv("EMULATOR_LIVING_CITY", "1")
        monkeypatch.setenv("EMULATOR_CITY_SEED", "2026")
        s = get_settings()
        assert s.living_city is True and s.city_seed == 2026
        monkeypatch.setenv("EMULATOR_LIVING_CITY", "0")
        monkeypatch.delenv("EMULATOR_CITY_SEED")
        s = get_settings()
        assert s.living_city is False and s.city_seed == 1411

    def test_living_violators_default_on(self, monkeypatch):
        monkeypatch.delenv("EMULATOR_LIVING_VIOLATORS", raising=False)
        assert get_settings().living_violators is True
        monkeypatch.setenv("EMULATOR_LIVING_VIOLATORS", "0")
        assert get_settings().living_violators is False
        monkeypatch.setenv("EMULATOR_LIVING_VIOLATORS", " ")     # blank: the default
        assert get_settings().living_violators is True

    def test_bad_deck_origins_fail_closed(self, monkeypatch):
        monkeypatch.setenv("EMULATOR_DECK_ORIGINS", "http://h:notaport")
        with pytest.raises(ValueError):
            get_settings()
