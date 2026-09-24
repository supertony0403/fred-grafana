"""Unit tests for fred_relay. No real network or database access:
Discord/HTTP/MySQL are replaced by fakes; the HTTP server tests bind to
127.0.0.1 on an ephemeral port only.

Run inside this folder:  python3 -m unittest -v
"""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import os
import re
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime
from unittest import mock
from typing import Any
from collections.abc import Mapping

import fred_relay as fr

GEHEIM = "GeheimOrdner-7f3a9c"
TOKEN = "Bot.TOKEN.xyz-123456"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeUhr:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeHttp:
    """Records calls; ``antworten`` maps a URL substring to a response, an
    exception, or a callable returning either."""

    def __init__(self, antworten: dict[str, Any] | None = None) -> None:
        self.antworten = antworten or {}
        self.aufrufe: list[tuple[str, dict[str, str]]] = []
        self._lock = threading.Lock()

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float, max_bytes: int) -> fr.HttpAntwort:
        with self._lock:
            self.aufrufe.append((url, dict(headers)))
        for teil, antwort in self.antworten.items():
            if teil in url:
                if callable(antwort) and not isinstance(antwort, fr.HttpAntwort):
                    antwort = antwort()
                if isinstance(antwort, BaseException):
                    raise antwort
                return antwort
        return fr.HttpAntwort(404, {}, b"")


def ok(body: bytes) -> fr.HttpAntwort:
    return fr.HttpAntwort(200, {}, body)


def ok_json(daten: Any) -> fr.HttpAntwort:
    return ok(json.dumps(daten).encode())


class FakeCursor:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self._ergebnis: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.db.ausgefuehrt.append((sql, params))
        for teil, ergebnis in self.db.antworten.items():
            if teil in sql:
                if isinstance(ergebnis, BaseException):
                    raise ergebnis
                self._ergebnis = list(ergebnis)
                return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._ergebnis[0] if self._ergebnis else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._ergebnis

    def close(self) -> None:
        pass


class FakeDb:
    def __init__(self, antworten: dict[str, Any]) -> None:
        self.antworten = antworten
        self.ausgefuehrt: list[tuple[str, tuple[Any, ...]]] = []
        self.geschlossen = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.geschlossen = True


def standard_db() -> FakeDb:
    return FakeDb({
        "SELECT COUNT(*) FROM fred_archiv": [(415_123,)],
        "INTERVAL 24 HOUR": [(1200, 42, 77, 31)],
        "MAX(zeit)": [(datetime(2026, 1, 1, 0, 0, 0),)],
        "GROUP BY kanal": [("Lounge", 3), ("Gaming", 1), (None, 1)],
        "SUM(dauer_s)": [(36000,)],
        "fred_verdacht": [(2,)],
        "fred_profile": [(512,)],
        "fred_gehirn_personen": [(300,)],
        "fred_gehirn_kanaele": [(25,)],
        "mee6_levels": [(1, 'Evil "Name"\\x\nzwei', 9000), (2, None, 8000)],
    })


def werte(text: str) -> dict[str, float]:
    """Parse exposition text into {'name{labels}': value}."""
    ergebnis: dict[str, float] = {}
    for zeile in text.splitlines():
        if not zeile or zeile.startswith("#"):
            continue
        schluessel, _, wert = zeile.rpartition(" ")
        ergebnis[schluessel] = float(wert)
    return ergebnis


def cfg(**over: Any) -> fr.Config:
    basis = dict(discord_token=TOKEN, mysql_user="fred", mysql_password="pw-secret-99",
                 ha_base="https://ha.example.test", ha_kamera_ordner=GEHEIM)
    basis.update(over)
    return fr.Config(**basis)


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #


class TestLabelEscaping(unittest.TestCase):
    def test_escapes_backslash_quote_newline(self) -> None:
        self.assertEqual(fr.label_escape('a\\b"c\nd'), 'a\\\\b\\"c\\nd')

    def test_backslash_escaped_first(self) -> None:
        # a literal backslash-n must become \\n, not \n
        self.assertEqual(fr.label_escape("x\\n"), "x\\\\n")

    def test_carriage_return_removed(self) -> None:
        self.assertEqual(fr.label_escape("a\rb"), "a b")

    def test_rendered_line_is_single_line(self) -> None:
        fam = fr.gauge("m", "h").add(1, name='böse"\n\\')
        text = fr.rendern([fam])
        self.assertEqual(text, '# HELP m h\n# TYPE m gauge\nm{name="böse\\"\\n\\\\"} 1\n')


class TestRendering(unittest.TestCase):
    def test_format(self) -> None:
        fams = [
            fr.gauge("a_total", "Help A", 5),
            fr.Familie("b_errors_total", "Help B", "counter").add(2, quelle="x").add(0, quelle="y"),
            fr.gauge("leer", "no samples -> omitted"),
            fr.gauge("c", "float", 1.5),
        ]
        self.assertEqual(
            fr.rendern(fams),
            "# HELP a_total Help A\n# TYPE a_total gauge\na_total 5\n"
            "# HELP b_errors_total Help B\n# TYPE b_errors_total counter\n"
            'b_errors_total{quelle="x"} 2\nb_errors_total{quelle="y"} 0\n'
            "# HELP c float\n# TYPE c gauge\nc 1.5\n",
        )

    def test_special_values(self) -> None:
        self.assertEqual(fr.zahl_format(float("nan")), "NaN")
        self.assertEqual(fr.zahl_format(float("inf")), "+Inf")
        self.assertEqual(fr.zahl_format(1767225600.0), "1767225600")
        self.assertEqual(fr.zahl_format(True), "1")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


class TestConfig(unittest.TestCase):
    def test_defaults(self) -> None:
        c = fr.Config.aus_env({})
        self.assertEqual(c.port, 8080)
        self.assertEqual(c.discord_guild_id, "1059960364613251132")
        self.assertEqual(c.mysql_host, "mysql.database.svc.cluster.local")
        self.assertEqual(c.mysql_db, "DdR")
        self.assertEqual(c.fred_user_id, 1413591205047959674)

    def test_guild_must_be_numeric(self) -> None:
        with self.assertRaises(ValueError):
            fr.Config.aus_env({"DISCORD_GUILD_ID": "123/../../users/@me"})

    def test_secrets_not_in_repr(self) -> None:
        text = repr(cfg())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(GEHEIM, text)
        self.assertNotIn("pw-secret-99", text)


# --------------------------------------------------------------------------- #
# Camera store
# --------------------------------------------------------------------------- #


class TestKameraStore(unittest.TestCase):
    def setUp(self) -> None:
        self.uhr = FakeUhr()
        self.http = FakeHttp({"draussen.jpg": ok(JPEG)})
        self.store = fr.KameraStore("https://ha.example.test/", GEHEIM, http_get=self.http, uhr=self.uhr)

    def test_url_built_from_base_and_folder(self) -> None:
        self.store.holen("draussen")
        self.assertEqual(self.http.aufrufe[0][0], f"https://ha.example.test/local/{GEHEIM}/draussen.jpg")

    def test_cache_within_20s(self) -> None:
        self.assertEqual(self.store.holen("draussen"), JPEG)
        self.uhr.t += 19.9
        self.assertEqual(self.store.holen("draussen"), JPEG)
        self.assertEqual(len(self.http.aufrufe), 1)

    def test_refetch_after_20s(self) -> None:
        self.store.holen("draussen")
        self.uhr.t += 20.0
        self.store.holen("draussen")
        self.assertEqual(len(self.http.aufrufe), 2)

    def test_parallel_requests_fetch_once(self) -> None:
        freigabe = threading.Event()

        def langsam() -> fr.HttpAntwort:
            freigabe.wait(5)
            return ok(JPEG)

        http = FakeHttp({"balkon.jpg": langsam})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=FakeUhr())
        ergebnisse: list[bytes | None] = []
        threads = [threading.Thread(target=lambda: ergebnisse.append(store.holen("balkon"))) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(0.2)  # let all threads queue on the per-camera lock
        freigabe.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(http.aufrufe), 1)
        self.assertEqual(ergebnisse, [JPEG] * 8)

    def test_other_camera_not_blocked_by_slow_fetch(self) -> None:
        freigabe = threading.Event()
        http = FakeHttp({"balkon.jpg": lambda: (freigabe.wait(5), ok(JPEG))[1], "drucker.jpg": ok(JPEG)})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=FakeUhr())
        t = threading.Thread(target=store.holen, args=("balkon",))
        t.start()
        time.sleep(0.1)
        start = time.monotonic()
        self.assertEqual(store.holen("drucker"), JPEG)
        store.status()  # must not block either
        store.familien()
        self.assertLess(time.monotonic() - start, 1.0)
        freigabe.set()
        t.join(5)

    def test_error_without_previous_image_returns_none(self) -> None:
        http = FakeHttp({"fisheye.jpg": urllib.error.URLError("boom")})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=self.uhr)
        self.assertIsNone(store.holen("fisheye"))
        self.assertFalse(store.ok("fisheye"))

    def test_error_serves_last_good_image(self) -> None:
        self.store.holen("draussen")
        self.http.antworten["draussen.jpg"] = fr.HttpAntwort(502, {}, b"bad gateway")
        self.uhr.t += 30
        self.assertEqual(self.store.holen("draussen"), JPEG)
        self.assertFalse(self.store.ok("draussen"))
        self.assertEqual(self.store.status()["draussen"], {"alter_s": 30.0, "ok": False})

    def test_failed_attempt_is_cached_too(self) -> None:
        self.http.antworten["draussen.jpg"] = fr.HttpAntwort(500, {}, b"")
        self.store.holen("draussen")
        self.uhr.t += 5
        self.store.holen("draussen")
        self.assertEqual(len(self.http.aufrufe), 1)

    def test_non_jpeg_rejected(self) -> None:
        self.http.antworten["draussen.jpg"] = ok(b"<html>login</html>")
        self.assertIsNone(self.store.holen("draussen"))

    def test_png_rejected(self) -> None:
        self.http.antworten["draussen.jpg"] = ok(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        self.assertIsNone(self.store.holen("draussen"))

    def test_oversized_rejected(self) -> None:
        store = fr.KameraStore("https://ha", GEHEIM, http_get=FakeHttp({"draussen": ok(JPEG + b"x" * 100)}),
                               uhr=self.uhr, max_bytes=50)
        self.assertIsNone(store.holen("draussen"))

    def test_exactly_max_size_accepted(self) -> None:
        store = fr.KameraStore("https://ha", GEHEIM, http_get=FakeHttp({"draussen": ok(JPEG)}),
                               uhr=self.uhr, max_bytes=len(JPEG))
        self.assertEqual(store.holen("draussen"), JPEG)

    def test_unknown_camera_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.store.holen("../etc/passwd")
        self.assertEqual(self.http.aufrufe, [])

    def test_not_configured_never_calls_upstream(self) -> None:
        http = FakeHttp()
        store = fr.KameraStore("", "", http_get=http, uhr=self.uhr)
        self.assertIsNone(store.holen("draussen"))
        self.assertEqual(http.aufrufe, [])
        self.assertFalse(store.konfiguriert)

    def test_metrics(self) -> None:
        self.store.holen("draussen")
        self.uhr.t += 7
        w = werte(fr.rendern(self.store.familien()))
        self.assertEqual(w['fred_kamera_alter_sekunden{kamera="draussen"}'], 7)
        self.assertEqual(w['fred_kamera_bytes{kamera="draussen"}'], len(JPEG))
        self.assertEqual(w['fred_kamera_ok{kamera="draussen"}'], 1)
        self.assertEqual(w['fred_kamera_ok{kamera="balkon"}'], 0)
        self.assertNotIn('fred_kamera_alter_sekunden{kamera="balkon"}', w)

    def test_upstream_error_log_hides_folder(self) -> None:
        http = FakeHttp({"draussen": urllib.error.URLError(f"cannot reach /local/{GEHEIM}/draussen.jpg")})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=self.uhr)
        with self.assertLogs("fred_relay", level="WARNING") as cm:
            store.holen("draussen")
        self.assertNotIn(GEHEIM, "\n".join(cm.output))


class TestPlatzhalter(unittest.TestCase):
    def test_svg(self) -> None:
        svg = fr.platzhalter_svg("balkon").decode()
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("Kamera offline", svg)
        self.assertIn("balkon", svg)

    def test_name_escaped(self) -> None:
        self.assertNotIn("<script>", fr.platzhalter_svg("<script>").decode())


# --------------------------------------------------------------------------- #
# Discord
# --------------------------------------------------------------------------- #

GUILD = {
    "approximate_member_count": 250,
    "approximate_presence_count": 40,
    "premium_subscription_count": 7,
    "premium_tier": 2,
    "roles": [{}] * 35,
    "emojis": [{}] * 12,
}
KANAELE = [{"type": t} for t in (0, 0, 0, 2, 2, 4, 15, 13, 5)]
THREADS = {"threads": [{}, {}, {}], "members": []}


def discord_http() -> FakeHttp:
    return FakeHttp({
        "/threads/active": ok_json(THREADS),
        "/channels": ok_json(KANAELE),
        "with_counts=true": ok_json(GUILD),
    })


class TestDiscord(unittest.TestCase):
    def test_metrics(self) -> None:
        q = fr.DiscordQuelle(TOKEN, "1059960364613251132", http_get=discord_http(), warten=lambda s: False)
        erg = q.sammeln()
        self.assertEqual(erg.fehler, 0)
        w = werte(fr.rendern(f for g in erg.gruppen.values() for f in g))
        self.assertEqual(w["discord_members_total"], 250)
        self.assertEqual(w["discord_online_total"], 40)
        self.assertEqual(w["discord_boosts_total"], 7)
        self.assertEqual(w["discord_boost_tier"], 2)
        self.assertEqual(w["discord_roles_total"], 35)
        self.assertEqual(w["discord_emojis_total"], 12)
        self.assertEqual(w['discord_channels_total{typ="text"}'], 3)
        self.assertEqual(w['discord_channels_total{typ="voice"}'], 2)
        self.assertEqual(w['discord_channels_total{typ="category"}'], 1)
        self.assertEqual(w['discord_channels_total{typ="forum"}'], 1)
        self.assertEqual(w['discord_channels_total{typ="stage"}'], 1)
        self.assertEqual(w['discord_channels_total{typ="sonstige"}'], 1)
        self.assertEqual(w["discord_threads_active"], 3)

    def test_request_urls_and_headers(self) -> None:
        http = discord_http()
        fr.DiscordQuelle(TOKEN, "42", http_get=http, warten=lambda s: False).sammeln()
        urls = [u for u, _ in http.aufrufe]
        self.assertEqual(urls, [
            "https://discord.com/api/v10/guilds/42?with_counts=true",
            "https://discord.com/api/v10/guilds/42/channels",
            "https://discord.com/api/v10/guilds/42/threads/active",
        ])
        for _, kopf in http.aufrufe:
            self.assertEqual(kopf["Authorization"], f"Bot {TOKEN}")
            self.assertEqual(kopf["User-Agent"], "DiscordBot (https://benz-sw.de, 1.0)")

    def test_429_respected(self) -> None:
        antworten = [fr.HttpAntwort(429, {}, b'{"retry_after": 1.5, "global": false}'), ok_json(GUILD)]
        http = FakeHttp({"with_counts": lambda: antworten.pop(0), "/channels": ok_json([]),
                         "/threads": ok_json(THREADS)})
        gewartet: list[float] = []
        q = fr.DiscordQuelle(TOKEN, "1", http_get=http, warten=lambda s: gewartet.append(s) or False)
        erg = q.sammeln()
        self.assertEqual(gewartet, [1.5])
        self.assertEqual(erg.fehler, 0)
        self.assertIn("guild", erg.gruppen)

    def test_429_header_fallback(self) -> None:
        antworten = [fr.HttpAntwort(429, {"retry-after": "2"}, b"not json"), ok_json(GUILD)]
        http = FakeHttp({"with_counts": lambda: antworten.pop(0), "/channels": ok_json([]),
                         "/threads": ok_json(THREADS)})
        gewartet: list[float] = []
        fr.DiscordQuelle(TOKEN, "1", http_get=http, warten=lambda s: gewartet.append(s) or False).sammeln()
        self.assertEqual(gewartet, [2.0])

    def test_long_rate_limit_gives_up_without_sleeping(self) -> None:
        http = FakeHttp({"with_counts": fr.HttpAntwort(429, {}, b'{"retry_after": 3600}'),
                         "/channels": ok_json([]), "/threads": ok_json(THREADS)})
        gewartet: list[float] = []
        with self.assertLogs("fred_relay", level="WARNING"):
            erg = fr.DiscordQuelle(TOKEN, "1", http_get=http,
                                   warten=lambda s: gewartet.append(s) or False).sammeln()
        self.assertEqual(gewartet, [])
        self.assertEqual(erg.fehler, 1)
        self.assertNotIn("guild", erg.gruppen)
        self.assertIn("channels", erg.gruppen)

    def test_shutdown_aborts_wait(self) -> None:
        http = FakeHttp({"with_counts": fr.HttpAntwort(429, {}, b'{"retry_after": 1}'),
                         "/channels": ok_json([]), "/threads": ok_json(THREADS)})
        with self.assertLogs("fred_relay", level="WARNING"):
            erg = fr.DiscordQuelle(TOKEN, "1", http_get=http, warten=lambda s: True).sammeln()
        self.assertEqual(len([u for u, _ in http.aufrufe if "with_counts" in u]), 1)
        self.assertEqual(erg.fehler, 1)

    def test_http_error_isolated_and_token_not_logged(self) -> None:
        http = discord_http()
        http.antworten["/channels"] = fr.HttpAntwort(403, {}, b'{"message":"Missing Access"}')
        with self.assertLogs("fred_relay", level="WARNING") as cm:
            erg = fr.DiscordQuelle(TOKEN, "1", http_get=http, warten=lambda s: False).sammeln()
        self.assertEqual(erg.fehler, 1)
        self.assertEqual(set(erg.gruppen), {"guild", "threads"})
        self.assertIn("HTTP 403", "\n".join(cm.output))
        self.assertNotIn(TOKEN, "\n".join(cm.output))

    def test_not_configured(self) -> None:
        http = FakeHttp()
        erg = fr.DiscordQuelle("", "1", http_get=http).sammeln()
        self.assertFalse(erg.konfiguriert)
        self.assertEqual(http.aufrufe, [])


# --------------------------------------------------------------------------- #
# MySQL
# --------------------------------------------------------------------------- #


class TestMysql(unittest.TestCase):
    def test_metrics(self) -> None:
        db = standard_db()
        erg = fr.MysqlQuelle(cfg(), verbinden=lambda: db).sammeln()
        self.assertEqual(erg.fehler, 0)
        self.assertTrue(db.geschlossen)
        w = werte(fr.rendern(f for g in erg.gruppen.values() for f in g))
        self.assertEqual(w["fred_archiv_nachrichten"], 415_123)
        self.assertNotIn("fred_archiv_nachrichten_total", w)
        self.assertEqual(w["fred_nachrichten_24h"], 1200)
        self.assertEqual(w["fred_nachrichten_1h"], 42)
        self.assertEqual(w["fred_antworten_24h"], 77)
        self.assertEqual(w["fred_aktive_nutzer_24h"], 31)
        self.assertEqual(w["fred_voice_aktiv"], 5)
        self.assertEqual(w['fred_voice_aktiv_kanal{kanal="Lounge"}'], 3)
        self.assertEqual(w['fred_voice_aktiv_kanal{kanal=""}'], 1)
        self.assertEqual(w["fred_voice_stunden_7d"], 10)
        self.assertEqual(w["fred_verdacht_gesperrt"], 2)
        self.assertEqual(w["fred_profile_total"], 512)
        self.assertEqual(w["fred_gehirn_personen_total"], 300)
        self.assertEqual(w['fred_levels_top{rang="2",name="2"}'], 8000)

    def test_last_message_timestamp_is_utc_epoch(self) -> None:
        erg = fr.MysqlQuelle(cfg(), verbinden=standard_db).sammeln()
        w = werte(fr.rendern(erg.gruppen["letzte_nachricht"]))
        # 2026-01-01T00:00:00Z, independent of the local timezone of the host
        self.assertEqual(w["fred_letzte_nachricht_timestamp_seconds"], 1767225600)

    def test_level_label_escaped(self) -> None:
        erg = fr.MysqlQuelle(cfg(), verbinden=standard_db).sammeln()
        text = fr.rendern(erg.gruppen["levels"])
        self.assertIn('fred_levels_top{rang="1",name="Evil \\"Name\\"\\\\x\\nzwei"} 9000', text)
        self.assertEqual(len([z for z in text.splitlines() if z.startswith("fred_levels_top")]), 2)

    def test_fred_user_id_passed_as_parameter(self) -> None:
        db = standard_db()
        fr.MysqlQuelle(cfg(fred_user_id=99), verbinden=lambda: db).sammeln()
        treffer = [(sql, p) for sql, p in db.ausgefuehrt if "INTERVAL 24 HOUR" in sql]
        self.assertEqual(len(treffer), 1)
        sql, params = treffer[0]
        self.assertEqual(params, (99,) * sql.count("%s"))
        self.assertNotIn("99", sql)  # never interpolated into the SQL text

    def test_uses_utc_timestamp(self) -> None:
        db = standard_db()
        fr.MysqlQuelle(cfg(), verbinden=lambda: db).sammeln()
        for sql, _ in db.ausgefuehrt:
            self.assertNotIn("NOW()", sql.upper())

    def test_failing_group_isolated(self) -> None:
        db = standard_db()
        db.antworten["fred_gehirn_personen"] = RuntimeError("Table doesn't exist")
        with self.assertLogs("fred_relay", level="WARNING"):
            erg = fr.MysqlQuelle(cfg(), verbinden=lambda: db).sammeln()
        self.assertEqual(erg.fehler, 1)
        self.assertNotIn("gehirn_personen", erg.gruppen)
        self.assertIn("levels", erg.gruppen)

    def test_connect_failure(self) -> None:
        def kaputt() -> Any:
            raise OSError("connection refused")

        with self.assertLogs("fred_relay", level="WARNING"):
            erg = fr.MysqlQuelle(cfg(), verbinden=kaputt).sammeln()
        self.assertEqual((erg.fehler, erg.gruppen), (1, {}))

    def test_empty_archive_has_no_timestamp(self) -> None:
        db = standard_db()
        db.antworten["MAX(zeit)"] = [(None,)]
        erg = fr.MysqlQuelle(cfg(), verbinden=lambda: db).sammeln()
        self.assertEqual(erg.gruppen["letzte_nachricht"], [])

    def test_not_configured(self) -> None:
        aufgerufen: list[int] = []
        erg = fr.MysqlQuelle(cfg(mysql_user=""), verbinden=lambda: aufgerufen.append(1)).sammeln()
        self.assertFalse(erg.konfiguriert)
        self.assertEqual(aufgerufen, [])


# --------------------------------------------------------------------------- #
# Cache / collector
# --------------------------------------------------------------------------- #


class StubQuelle:
    def __init__(self, name: str, ergebnisse: list[Any]) -> None:
        self.name = name
        self.ergebnisse = ergebnisse

    def sammeln(self) -> fr.SammelErgebnis:
        e = self.ergebnisse.pop(0)
        if isinstance(e, BaseException):
            raise e
        return e


class TestCacheUndSammler(unittest.TestCase):
    def app(self, quellen: list[Any]) -> fr.App:
        store = fr.KameraStore("", "", http_get=FakeHttp())
        return fr.App(quellen, store, fr.MetrikCache([q.name for q in quellen], uhr=lambda: 5000.0))

    def test_errors_keep_old_values(self) -> None:
        q = StubQuelle("discord", [
            fr.SammelErgebnis(gruppen={"guild": [fr.gauge("discord_members_total", "h", 250)]}),
            fr.SammelErgebnis(fehler=1),
            RuntimeError("crash"),
            fr.SammelErgebnis(gruppen={"guild": [fr.gauge("discord_members_total", "h", 251)]}),
        ])
        app = self.app([q])
        app.sammeln_einmal()
        w = werte(app.metriken_text())
        self.assertEqual(w["discord_members_total"], 250)
        self.assertEqual(w['fred_quelle_up{quelle="discord"}'], 1)
        self.assertEqual(w['fred_scrape_errors_total{quelle="discord"}'], 0)
        self.assertEqual(w['fred_quelle_letzter_erfolg_timestamp_seconds{quelle="discord"}'], 5000)

        app.sammeln_einmal()
        w = werte(app.metriken_text())
        self.assertEqual(w["discord_members_total"], 250)
        self.assertEqual(w['fred_quelle_up{quelle="discord"}'], 0)
        self.assertEqual(w['fred_scrape_errors_total{quelle="discord"}'], 1)

        with self.assertLogs("fred_relay", level="WARNING"):
            app.sammeln_einmal()
        w = werte(app.metriken_text())
        self.assertEqual(w["discord_members_total"], 250)
        self.assertEqual(w['fred_scrape_errors_total{quelle="discord"}'], 2)

        app.sammeln_einmal()
        w = werte(app.metriken_text())
        self.assertEqual(w["discord_members_total"], 251)
        self.assertEqual(w['fred_quelle_up{quelle="discord"}'], 1)
        self.assertEqual(w['fred_scrape_errors_total{quelle="discord"}'], 2)

    def test_not_configured_is_down_without_errors(self) -> None:
        app = self.app([StubQuelle("mysql", [fr.SammelErgebnis(konfiguriert=False)])])
        app.sammeln_einmal()
        w = werte(app.metriken_text())
        self.assertEqual(w['fred_quelle_up{quelle="mysql"}'], 0)
        self.assertEqual(w['fred_scrape_errors_total{quelle="mysql"}'], 0)

    def test_render_failure_returns_last_text(self) -> None:
        app = self.app([StubQuelle("discord", [fr.SammelErgebnis()])])
        app.sammeln_einmal()
        gut = app.metriken_text()
        app.cache.familien = lambda: 1 / 0  # type: ignore[method-assign]
        with self.assertLogs("fred_relay", level="ERROR"):
            self.assertEqual(app.metriken_text(), gut)

    def test_kamera_quelle(self) -> None:
        http = FakeHttp({"draussen": ok(JPEG), "fisheye": ok(JPEG), "balkon": ok(JPEG)})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=FakeUhr())
        with self.assertLogs("fred_relay", level="WARNING"):
            erg = fr.KameraQuelle(store).sammeln()
        self.assertEqual(erg.fehler, 1)  # drucker -> 404

    def test_collector_loop_stops(self) -> None:
        zaehler = StubQuelle("discord", [fr.SammelErgebnis()] * 100)
        app = self.app([zaehler])
        stop = threading.Event()
        t = threading.Thread(target=app.sammel_schleife, args=(stop, 60.0))
        t.start()
        time.sleep(0.1)
        stop.set()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(zaehler.ergebnisse), 99)  # exactly one round ran


# --------------------------------------------------------------------------- #
# HTTP server (loopback only)
# --------------------------------------------------------------------------- #


class TestHttpServer(unittest.TestCase):
    def setUp(self) -> None:
        self.http = FakeHttp({
            "draussen.jpg": ok(JPEG),
            "fisheye.jpg": urllib.error.URLError(f"https://ha/local/{GEHEIM}/fisheye.jpg unreachable"),
            "balkon.jpg": ok(b"<html>" + GEHEIM.encode() + b"</html>"),
            "drucker.jpg": fr.HttpAntwort(404, {}, GEHEIM.encode()),
        })
        self.store = fr.KameraStore("https://ha", GEHEIM, http_get=self.http)
        cache = fr.MetrikCache()
        cache.uebernehmen("discord", fr.SammelErgebnis(gruppen={"g": [fr.gauge("discord_members_total", "h", 3)]}))
        self.app = fr.App([], self.store, cache)
        self.server = fr._Server(("127.0.0.1", 0), fr.handler_klasse(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()
        self.logger = logging.getLogger("fred_relay")
        self.schwaerzer = fr.Schwaerzer(cfg().geheimnisse())
        self.logger.addFilter(self.schwaerzer)

    def tearDown(self) -> None:
        self.logger.removeFilter(self.schwaerzer)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def get(self, pfad: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request("GET", pfad)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def test_healthz(self) -> None:
        self.assertEqual(self.get("/healthz")[::2], (200, b"ok"))

    def test_metrics(self) -> None:
        status, kopf, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(kopf["content-type"], "text/plain; version=0.0.4; charset=utf-8")
        text = body.decode()
        self.assertIn("# TYPE fred_scrape_errors_total counter\n", text)
        self.assertIn('fred_quelle_up{quelle="discord"} 1\n', text)
        self.assertIn('fred_quelle_up{quelle="kamera"} 0\n', text)
        self.assertIn("discord_members_total 3\n", text)
        self.assertTrue(text.endswith("\n"))

    def test_camera_jpeg(self) -> None:
        status, kopf, body = self.get("/kamera/draussen.jpg")
        self.assertEqual((status, body), (200, JPEG))
        self.assertEqual(kopf["content-type"], "image/jpeg")
        self.assertEqual(kopf["cache-control"], "private, max-age=15")

    def test_camera_placeholder_on_error(self) -> None:
        for name in ("fisheye", "balkon", "drucker"):
            with self.subTest(name=name), self.assertLogs("fred_relay", level="WARNING"):
                status, kopf, body = self.get(f"/kamera/{name}.jpg")
                self.assertEqual(status, 200)
                self.assertEqual(kopf["content-type"], "image/svg+xml")
                self.assertIn(b"Kamera offline", body)

    def test_whitelist_and_traversal(self) -> None:
        for pfad in ("/kamera/foo.jpg", "/kamera/../x.jpg", "/kamera/%2e%2e/x.jpg",
                     "/kamera/draussen.jpg/../../etc/passwd", "/kamera/draussen.png",
                     "/kamera/DRAUSSEN.jpg", "/kamera/draussen", "/kamera/draussen.jpg%00",
                     "/kamera//draussen.jpg", "/kamera/..%2fdraussen.jpg",
                     "/", "/metrics/", "/etc/passwd"):
            with self.subTest(pfad=pfad):
                status, _, _ = self.get(pfad)
                self.assertEqual(status, 404)
        self.assertEqual(self.http.aufrufe, [])

    def test_query_string_ignored(self) -> None:
        self.assertEqual(self.get("/kamera/draussen.jpg?t=123")[0], 200)
        self.assertEqual(self.get("/metrics?x=1")[0], 200)

    def test_camera_overview_json(self) -> None:
        self.get("/kamera/draussen.jpg")
        status, kopf, body = self.get("/kamera/")
        self.assertEqual(status, 200)
        self.assertTrue(kopf["content-type"].startswith("application/json"))
        daten = json.loads(body)
        self.assertEqual(set(daten), set(fr.KAMERAS))
        self.assertTrue(daten["draussen"]["ok"])
        self.assertIsInstance(daten["draussen"]["alter_s"], float)
        self.assertEqual(daten["balkon"], {"alter_s": None, "ok": False})

    def test_secret_folder_never_leaks(self) -> None:
        antworten: list[bytes] = []
        with self.assertLogs("fred_relay", level="DEBUG") as cm:
            logging.getLogger("fred_relay").debug("start")
            for pfad in ("/kamera/draussen.jpg", "/kamera/fisheye.jpg", "/kamera/balkon.jpg",
                         "/kamera/drucker.jpg", "/kamera/", "/kamera/nope.jpg", "/metrics",
                         "/healthz", "/kamera/../" + GEHEIM):
                status, kopf, body = self.get(pfad)
                antworten.append(body)
                antworten.append(json.dumps(kopf).encode())
        alles = b"\n".join(antworten)
        self.assertNotIn(GEHEIM.encode(), alles)
        self.assertNotIn(GEHEIM, "\n".join(cm.output))

    def test_handler_exception_gives_500_not_crash(self) -> None:
        self.app.store.status = lambda: 1 / 0  # type: ignore[method-assign]
        with self.assertLogs("fred_relay", level="ERROR"):
            self.assertEqual(self.get("/kamera/")[0], 500)
        self.assertEqual(self.get("/healthz")[0], 200)


class TestSchwaerzer(unittest.TestCase):
    def test_redacts_secrets_and_quoted_form(self) -> None:
        logger = logging.getLogger("fred_relay")
        f = fr.Schwaerzer([TOKEN, "a b/c-geheim"])
        logger.addFilter(f)
        try:
            with self.assertLogs("fred_relay", level="WARNING") as cm:
                logger.warning("x %s y %s", TOKEN, "a%20b%2Fc-geheim")
        finally:
            logger.removeFilter(f)
        self.assertEqual(cm.records[0].getMessage(), "x *** y ***")


# --------------------------------------------------------------------------- #
# Review findings: M2, N1-N3, N5, N10 and the surviving mutants
# --------------------------------------------------------------------------- #


class TestKameraNichtWarten(unittest.TestCase):
    """M2: a request must not queue behind a running fetch if an older image exists."""

    def test_old_image_served_immediately_while_fetch_runs(self) -> None:
        uhr = FakeUhr()
        http = FakeHttp({"balkon.jpg": ok(JPEG)})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=uhr)
        self.assertEqual(store.holen("balkon"), JPEG)
        uhr.t += 30  # cache expired
        gestartet = threading.Event()
        neu = JPEG + b"neu"

        def langsam() -> fr.HttpAntwort:  # dummy upstream that needs 2 s
            gestartet.set()
            time.sleep(2.0)
            return ok(neu)

        http.antworten["balkon.jpg"] = langsam
        t = threading.Thread(target=store.holen, args=("balkon",))
        t.start()
        self.assertTrue(gestartet.wait(2))
        start = time.monotonic()
        bild = store.holen("balkon")
        dauer = time.monotonic() - start
        t.join(5)
        self.assertEqual(bild, JPEG)
        self.assertLess(dauer, 0.3)
        self.assertEqual(len(http.aufrufe), 2)  # no second upstream request
        self.assertEqual(store.holen("balkon"), neu)  # the running fetch still lands

    def test_lock_released_after_upstream_exception(self) -> None:
        store = fr.KameraStore("https://ha", GEHEIM, uhr=FakeUhr(),
                               http_get=FakeHttp({"balkon": RuntimeError("x")}))
        with self.assertLogs("fred_relay", level="WARNING"):
            store.holen("balkon")
        self.assertFalse(store._eintraege["balkon"].hol_lock.locked())


class TestParalleleSammlung(unittest.TestCase):
    """N3: cameras in parallel, sources independent of each other."""

    def test_cameras_fetched_in_parallel(self) -> None:
        def langsam() -> fr.HttpAntwort:
            time.sleep(0.4)
            return ok(JPEG)

        http = FakeHttp({f"{n}.jpg": langsam for n in fr.KAMERAS})
        store = fr.KameraStore("https://ha", GEHEIM, http_get=http, uhr=FakeUhr())
        start = time.monotonic()
        erg = fr.KameraQuelle(store).sammeln()
        dauer = time.monotonic() - start
        self.assertEqual(erg.fehler, 0)
        self.assertEqual(len(http.aufrufe), len(fr.KAMERAS))
        self.assertLess(dauer, 1.0)  # serial would be 4 * 0.4 s

    def test_hanging_source_does_not_delay_others(self) -> None:
        freigabe = threading.Event()

        class Haengt:
            name = "kamera"

            def sammeln(self) -> fr.SammelErgebnis:
                freigabe.wait(5)
                return fr.SammelErgebnis()

        schnell = StubQuelle("discord", [fr.SammelErgebnis(
            gruppen={"g": [fr.gauge("discord_members_total", "h", 7)]})])
        quellen: list[Any] = [Haengt(), schnell]
        app = fr.App(quellen, fr.KameraStore("", "", http_get=FakeHttp()),
                     fr.MetrikCache([q.name for q in quellen]))
        t = threading.Thread(target=app.sammeln_einmal)
        t.start()
        try:
            ende = time.monotonic() + 2
            w: dict[str, float] = {}
            while time.monotonic() < ende:
                w = werte(app.metriken_text())
                if w.get("discord_members_total") == 7:
                    break
                time.sleep(0.02)
            self.assertEqual(w.get("discord_members_total"), 7)
            self.assertEqual(w['fred_quelle_up{quelle="discord"}'], 1)
            self.assertTrue(t.is_alive())  # the hanging source is still running
        finally:
            freigabe.set()
            t.join(5)
        self.assertEqual(werte(app.metriken_text())['fred_quelle_up{quelle="kamera"}'], 1)


class TestMenschDefinition(unittest.TestCase):
    """N5: one definition of a human message for all human metrics."""

    def test_same_condition_in_all_human_metrics(self) -> None:
        self.assertEqual(fr.MENSCH_BEDINGUNG, "bot = 0 AND user_id <> %s AND geloescht IS NULL")
        db = standard_db()
        fr.MysqlQuelle(cfg(), verbinden=lambda: db).sammeln()
        (sql,) = [q for q, _ in db.ausgefuehrt if "INTERVAL 24 HOUR" in q]
        self.assertEqual(sql.count(fr.MENSCH_BEDINGUNG), 3)
        self.assertIn(f"COUNT(DISTINCT CASE WHEN {fr.MENSCH_BEDINGUNG} THEN user_id END)", sql)
        self.assertEqual(sql.count(f"SUM({fr.MENSCH_BEDINGUNG}"), 2)


class TestHaBasisHttps(unittest.TestCase):
    """N10: the camera URL (with the secret folder) only travels over https."""

    def test_non_https_base_disables_cameras(self) -> None:
        for basis in ("http://ha.example.test", "ftp://ha.example.test", "ha.example.test",
                      "https://", "//ha.example.test"):
            with self.subTest(basis=basis):
                http = FakeHttp({"jpg": ok(JPEG)})
                with self.assertLogs("fred_relay", level="WARNING") as cm:
                    app = fr.App.aus_config(cfg(ha_base=basis), threading.Event(), http_get=http)
                self.assertFalse(app.store.konfiguriert)
                self.assertFalse(fr.KameraQuelle(app.store).sammeln().konfiguriert)
                self.assertIsNone(app.store.holen("draussen"))
                self.assertEqual(http.aufrufe, [])
                ausgabe = "\n".join(cm.output)
                self.assertIn("HA_BASE", ausgabe)
                self.assertNotIn(GEHEIM, ausgabe)

    def test_https_base_accepted_without_warning(self) -> None:
        for basis in ("https://ha.example.test", "HTTPS://ha.example.test"):
            with self.subTest(basis=basis):
                http = FakeHttp({"jpg": ok(JPEG)})
                app = fr.App.aus_config(cfg(ha_base=basis), threading.Event(), http_get=http)
                self.assertTrue(app.store.konfiguriert)
                self.assertEqual(app.store.holen("draussen"), JPEG)

    def test_empty_base_is_silent(self) -> None:
        self.assertEqual(fr.ha_basis_pruefen(""), "")


class TestEinrichten(unittest.TestCase):
    """main() wiring without starting a server."""

    ENV = {"DISCORD_TOKEN": TOKEN, "MYSQL_USER": "fred", "MYSQL_PASSWORD": "pw-secret-99",
           "HA_BASE": "https://ha.example.test", "HA_KAMERA_ORDNER": GEHEIM}

    def test_redacting_filter_installed_on_logger(self) -> None:
        aufbau = fr.einrichten(self.ENV, http_get=FakeHttp())
        self.addCleanup(fr.log.removeFilter, aufbau.schwaerzer)
        self.assertIn(aufbau.schwaerzer, fr.log.filters)
        with self.assertLogs("fred_relay", level="WARNING") as cm:
            fr.log.warning("leak %s %s %s", TOKEN, GEHEIM, "pw-secret-99")
        self.assertEqual(cm.records[0].getMessage(), "leak *** *** ***")
        self.assertTrue(aufbau.app.store.konfiguriert)
        self.assertEqual([q.name for q in aufbau.app.quellen], ["discord", "mysql", "kamera"])

    def test_missing_sources_warned(self) -> None:
        with self.assertLogs("fred_relay", level="WARNING") as cm:
            aufbau = fr.einrichten({}, http_get=FakeHttp())
        self.addCleanup(fr.log.removeFilter, aufbau.schwaerzer)
        ausgabe = "\n".join(cm.output)
        for name in ("discord", "mysql", "kamera"):
            self.assertIn(f"source {name} not configured", ausgabe)

    def test_main_returns_2_on_bad_config(self) -> None:
        vorher = list(logging.getLogger().handlers)

        def aufraeumen() -> None:
            for h in logging.getLogger().handlers[:]:
                if h not in vorher:
                    logging.getLogger().removeHandler(h)

        self.addCleanup(aufraeumen)
        with mock.patch.dict(os.environ, {"DISCORD_GUILD_ID": "1/../x"}), \
                mock.patch.object(fr, "_Server") as server, \
                self.assertLogs("fred_relay", level="ERROR"):
            self.assertEqual(fr.main(), 2)
        server.assert_not_called()


class TestGeheimnisse(unittest.TestCase):
    def test_all_secrets_listed(self) -> None:
        g = cfg().geheimnisse()
        self.assertIn(GEHEIM, g)
        self.assertIn(TOKEN, g)
        self.assertIn("pw-secret-99", g)

    def test_exception_traceback_redacted(self) -> None:
        logger = logging.getLogger("fred_relay")
        f = fr.Schwaerzer([GEHEIM])
        logger.addFilter(f)
        try:
            with self.assertLogs("fred_relay", level="ERROR") as cm:
                try:
                    raise RuntimeError(f"GET https://ha/local/{GEHEIM}/x.jpg failed")
                except RuntimeError:
                    logger.exception("upstream broken")
        finally:
            logger.removeFilter(f)
        ausgabe = "\n".join(cm.output)
        self.assertIn("Traceback", ausgabe)
        self.assertIn("RuntimeError", ausgabe)
        self.assertNotIn(GEHEIM, ausgabe)
        self.assertIn("/local/***/x.jpg", cm.records[0].exc_text or "")


class TestKleinigkeiten(unittest.TestCase):
    def test_folder_quoted_in_upstream_url(self) -> None:
        http = FakeHttp({"draussen": ok(JPEG)})
        store = fr.KameraStore("https://ha", "a b/c?d#e", http_get=http, uhr=FakeUhr())
        store.holen("draussen")
        self.assertEqual(http.aufrufe[0][0], "https://ha/local/a%20b%2Fc%3Fd%23e/draussen.jpg")

    def test_help_line_escaped(self) -> None:
        text = fr.rendern([fr.gauge("m", "a\\b\nc", 1)])
        self.assertEqual(text, "# HELP m a\\\\b\\nc\n# TYPE m gauge\nm 1\n")

    def test_handler_has_socket_timeout(self) -> None:
        app = fr.App([], fr.KameraStore("", "", http_get=FakeHttp()), fr.MetrikCache([]))
        self.assertEqual(fr.handler_klasse(app).timeout, 15)


# --------------------------------------------------------------------------- #
# urllib_get against a real local http.server (loopback only)
# --------------------------------------------------------------------------- #


class TestLesenMitFrist(unittest.TestCase):
    """Deadline fallback when no socket is reachable under the response."""

    class Tropfen:
        def __init__(self) -> None:
            self.aufrufe = 0

        def read1(self, n: int) -> bytes:
            self.aufrufe += 1
            time.sleep(0.02)
            return b"x"

    def test_deadline_without_socket(self) -> None:
        resp = self.Tropfen()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            fr._lesen_mit_frist(resp, 10_000, time.monotonic() + 0.2)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertLess(resp.aufrufe, 50)

    def test_expired_deadline_reads_nothing(self) -> None:
        resp = self.Tropfen()
        with self.assertRaises(TimeoutError):
            fr._lesen_mit_frist(resp, 10, time.monotonic() - 1)
        self.assertEqual(resp.aufrufe, 0)


class _LokalServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _LokalHandler)
        self.routen: dict[str, Any] = {}
        self.pfade: list[str] = []

    @property
    def basis(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _LokalHandler(http.server.BaseHTTPRequestHandler):
    server: _LokalServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def antworten(self, status: int, body: bytes, typ: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self.server.pfade.append(self.path)
        route = self.server.routen.get(urllib.parse.urlsplit(self.path).path)
        if route is None:
            self.antworten(404, b"nope")
        else:
            route(self)


def _tropfen(h: _LokalHandler) -> None:
    """Announce 1000 bytes, then drip one byte every 50 ms for at most 3 s."""
    h.send_response(200)
    h.send_header("Content-Length", "1000")
    h.end_headers()
    try:
        for _ in range(60):
            h.wfile.write(b"x")
            h.wfile.flush()
            time.sleep(0.05)
    except OSError:
        pass  # client gave up - that is the point


class TestUrllibGetLokal(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _LokalServer()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_404_is_reported_as_404(self) -> None:
        antwort = fr.urllib_get(self.server.basis + "/fehlt", {}, 5, 1000)
        self.assertEqual(antwort.status, 404)
        self.assertEqual(antwort.body, b"nope")

    def test_200_and_headers(self) -> None:
        self.server.routen["/da"] = lambda h: h.antworten(200, b"hallo", "text/plain")
        antwort = fr.urllib_get(self.server.basis + "/da", {}, 5, 1000)
        self.assertEqual((antwort.status, antwort.body), (200, b"hallo"))
        self.assertEqual(antwort.headers["content-type"], "text/plain")

    def test_size_limit_reads_at_most_max_plus_one(self) -> None:
        self.server.routen["/gross"] = lambda h: h.antworten(200, b"y" * 100_000)
        antwort = fr.urllib_get(self.server.basis + "/gross", {}, 5, 100)
        self.assertEqual(len(antwort.body), 101)

    def test_dripping_body_hits_total_deadline(self) -> None:
        self.server.routen["/tropf"] = _tropfen
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            fr.urllib_get(self.server.basis + "/tropf", {}, 0.5, 10_000)
        self.assertLess(time.monotonic() - start, 1.5)

    def test_deadline_counts_from_request_start(self) -> None:
        """Headers arrive late (0.35 s, under the per-read timeout), then the
        body stalls. The total deadline of 0.5 s must still hold: it starts
        before connecting, and the socket timeout shrinks to the rest."""
        def spaet(h: _LokalHandler) -> None:
            time.sleep(0.35)
            h.send_response(200)
            h.send_header("Content-Length", "10")
            h.end_headers()
            h.wfile.flush()
            time.sleep(2)
            try:
                h.wfile.write(b"x" * 10)
            except OSError:
                pass

        self.server.routen["/spaet"] = spaet
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            fr.urllib_get(self.server.basis + "/spaet", {}, 0.5, 1000)
        self.assertLess(time.monotonic() - start, 0.75)  # without the fix: ~0.85 s

    def test_camera_end_to_end(self) -> None:
        pfad = "/local/ord%20ner/"
        self.server.routen[pfad + "draussen.jpg"] = lambda h: h.antworten(200, JPEG + b"x" * 1000)
        self.server.routen[pfad + "balkon.jpg"] = lambda h: h.antworten(200, JPEG)
        self.server.routen[pfad + "drucker.jpg"] = _tropfen
        # fisheye -> 404
        store = fr.KameraStore(self.server.basis, "ord ner", http_get=fr.urllib_get, uhr=FakeUhr(),
                               max_bytes=200)
        with self.assertLogs("fred_relay", level="WARNING") as cm:
            self.assertIsNone(store.holen("draussen"))  # too large
            self.assertIsNone(store.holen("fisheye"))   # 404 is not a success
        self.assertEqual(store.holen("balkon"), JPEG)
        ausgabe = "\n".join(cm.output)
        self.assertIn("larger than 200 bytes", ausgabe)
        self.assertIn("upstream HTTP 404", ausgabe)
        self.assertFalse(store.ok("fisheye"))
        self.assertIn(pfad + "balkon.jpg", self.server.pfade)

    def test_discord_end_to_end(self) -> None:
        basis = "/api/guilds/1"
        self.server.routen[basis] = lambda h: h.antworten(200, json.dumps(
            {**GUILD, "roles": [{"name": "r" * 50}] * 20}).encode())
        self.server.routen[basis + "/threads/active"] = lambda h: h.antworten(200, json.dumps(THREADS).encode())
        # /channels -> 404
        q = fr.DiscordQuelle(TOKEN, "1", http_get=fr.urllib_get, warten=lambda s: False,
                             api_basis=self.server.basis + "/api", max_bytes=500)
        with self.assertLogs("fred_relay", level="WARNING") as cm:
            erg = q.sammeln()
        self.assertEqual(erg.fehler, 2)
        self.assertEqual(set(erg.gruppen), {"threads"})
        ausgabe = "\n".join(cm.output)
        self.assertIn("response too large", ausgabe)
        self.assertIn("HTTP 404", ausgabe)
        self.assertNotIn(TOKEN, ausgabe)


# --------------------------------------------------------------------------- #
# HTTP server: headers, HEAD
# --------------------------------------------------------------------------- #


class TestHttpKoepfe(TestHttpServer):
    """Runs on the TestHttpServer fixture (its tests are inherited and re-run)."""

    def anfrage(self, methode: str, pfad: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        try:
            conn.request(methode, pfad)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def roh(self, anfrage: bytes) -> bytes:
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=5) as s:
            s.sendall(anfrage)
            teile = []
            while True:
                stueck = s.recv(65536)
                if not stueck:
                    break
                teile.append(stueck)
        return b"".join(teile)

    def test_metrics_no_store(self) -> None:
        self.assertEqual(self.get("/metrics")[1]["cache-control"], "no-store")

    def test_placeholder_headers(self) -> None:
        with self.assertLogs("fred_relay", level="WARNING"):
            status, kopf, _ = self.get("/kamera/fisheye.jpg")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", kopf["content-security-policy"])
        cc = kopf["cache-control"]
        self.assertNotIn("public", cc)
        max_age = re.search(r"max-age=(\d+)", cc)
        self.assertIsNotNone(max_age)
        self.assertLessEqual(int(max_age.group(1)), 15)  # type: ignore[union-attr]
        self.assertEqual(kopf["x-content-type-options"], "nosniff")

    def test_head_same_headers_as_get_without_body(self) -> None:
        for pfad in ("/metrics", "/healthz", "/kamera/", "/kamera/draussen.jpg", "/gibtsnicht"):
            with self.subTest(pfad=pfad):
                g_status, g_kopf, g_body = self.anfrage("GET", pfad)
                h_status, h_kopf, h_body = self.anfrage("HEAD", pfad)
                self.assertEqual(h_status, g_status)
                self.assertEqual(h_body, b"")
                for k in ("content-type", "content-length", "cache-control", "x-content-type-options"):
                    self.assertEqual(h_kopf.get(k), g_kopf.get(k), k)
                if pfad != "/kamera/":  # JSON contains live ages, length may vary
                    self.assertEqual(int(h_kopf["content-length"]), len(g_body))

    def test_head_sends_no_body_bytes(self) -> None:
        antwort = self.roh(b"HEAD /healthz HTTP/1.0\r\nHost: x\r\n\r\n")
        kopf, _, rest = antwort.partition(b"\r\n\r\n")
        self.assertTrue(kopf.startswith(b"HTTP/1.0 200"))
        self.assertIn(b"Content-Length: 2", kopf)
        self.assertEqual(rest, b"")


if __name__ == "__main__":
    unittest.main()
