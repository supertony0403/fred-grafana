"""fred-relay: Prometheus exporter and camera relay for the Fred Grafana dashboard.

Runs as a single pod (python:3.12-slim) in the k3s namespace ``monitoring``.
Only the standard library plus ``pymysql`` (imported lazily) is used.

Endpoints (port 8080 by default):

* ``GET /metrics``            Prometheus text format 0.0.4, served from a cache
                              that a background thread refreshes every 60 s.
                              A scrape never blocks on upstreams and never 500s.
* ``GET /kamera/<name>.jpg``  Whitelisted camera relay (draussen, fisheye,
                              balkon, drucker) with a 20 s in-memory cache and
                              one upstream fetch per camera at a time. Falls
                              back to the last good image, else to an SVG
                              "Kamera offline" placeholder (status 200).
* ``GET /kamera/``            JSON overview ``{name: {alter_s, ok}}``.
* ``GET /healthz``            ``ok``.

``HEAD`` is answered for every route with the same status and headers as
``GET`` but without a body.

All configuration comes from environment variables (see ``Config.aus_env``).
Secrets (Discord token, MySQL password, HA camera folder) are never logged and
never appear in any response: a logging filter redacts them as a last line of
defence, and error paths only log camera names / status codes.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from collections.abc import Callable, Iterable, Mapping

log = logging.getLogger("fred_relay")

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

KAMERAS: tuple[str, ...] = ("draussen", "fisheye", "balkon", "drucker")
KAMERA_CACHE_S = 20.0
KAMERA_MAX_BYTES = 5 * 1024 * 1024
JPEG_MAGIC = b"\xff\xd8"

HTTP_TIMEOUT_S = 10.0
DISCORD_API = "https://discord.com/api/v10"
DISCORD_UA = "DiscordBot (https://benz-sw.de, 1.0)"
DISCORD_MAX_BYTES = 4 * 1024 * 1024
DISCORD_MAX_WARTEN_S = 30.0  # longer rate limits: give up this round
DISCORD_MAX_VERSUCHE = 3

QUELLEN: tuple[str, ...] = ("discord", "mysql", "kamera")
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Discord channel type ids -> label values
KANALTYPEN: dict[int, str] = {0: "text", 2: "voice", 4: "category", 15: "forum", 13: "stage"}
KANALTYP_LABELS: tuple[str, ...] = ("text", "voice", "category", "forum", "stage", "sonstige")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    """Runtime configuration, read exclusively from environment variables."""

    port: int = 8080
    intervall_s: float = 60.0
    discord_token: str = field(default="", repr=False)
    discord_guild_id: str = "1059960364613251132"
    mysql_host: str = "mysql.database.svc.cluster.local"
    mysql_port: int = 3306
    mysql_user: str = ""
    mysql_password: str = field(default="", repr=False)
    mysql_db: str = "DdR"
    fred_user_id: int = 1413591205047959674
    ha_base: str = ""
    ha_kamera_ordner: str = field(default="", repr=False)

    @classmethod
    def aus_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build the config from ``env`` (defaults to ``os.environ``).

        Raises:
            ValueError: if a numeric setting is malformed.
        """
        e = os.environ if env is None else env
        d = cls()

        def text(name: str, default: str) -> str:
            return e.get(name, default).strip()

        def ganzzahl(name: str, default: int) -> int:
            roh = e.get(name, "").strip()
            if not roh:
                return default
            if not roh.isdigit():
                raise ValueError(f"{name} must be a non-negative integer")
            return int(roh)

        guild = text("DISCORD_GUILD_ID", d.discord_guild_id)
        if not guild.isdigit():
            # also prevents path injection into the Discord URL
            raise ValueError("DISCORD_GUILD_ID must be numeric")

        intervall_roh = text("SAMMEL_INTERVALL_S", "")
        try:
            intervall = float(intervall_roh) if intervall_roh else d.intervall_s
        except ValueError as exc:
            raise ValueError("SAMMEL_INTERVALL_S must be a number") from exc
        if intervall < 5:
            raise ValueError("SAMMEL_INTERVALL_S must be >= 5")

        return cls(
            port=ganzzahl("PORT", d.port),
            intervall_s=intervall,
            discord_token=text("DISCORD_TOKEN", ""),
            discord_guild_id=guild,
            mysql_host=text("MYSQL_HOST", d.mysql_host),
            mysql_port=ganzzahl("MYSQL_PORT", d.mysql_port),
            mysql_user=text("MYSQL_USER", ""),
            mysql_password=e.get("MYSQL_PASSWORD", ""),
            mysql_db=text("MYSQL_DB", d.mysql_db),
            fred_user_id=ganzzahl("FRED_USER_ID", d.fred_user_id),
            ha_base=text("HA_BASE", "").rstrip("/"),
            ha_kamera_ordner=text("HA_KAMERA_ORDNER", "").strip("/"),
        )

    def geheimnisse(self) -> list[str]:
        """All secret values that must never show up in logs."""
        return [s for s in (self.discord_token, self.mysql_password, self.ha_kamera_ordner) if s]


class Schwaerzer(logging.Filter):
    """Logging filter that replaces secret strings in every record with ``***``.

    This is defence in depth: code paths already avoid logging secrets, but an
    unexpected exception text (e.g. containing a URL) must not leak them.
    """

    def __init__(self, geheimnisse: Iterable[str]) -> None:
        super().__init__()
        # very short "secrets" would redact random text; ignore them
        self._geheimnisse = sorted({g for g in geheimnisse if len(g) >= 4}, key=len, reverse=True)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._geheimnisse:
            return True
        try:
            text = record.getMessage()
        except Exception:  # malformed format args: leave record untouched
            return True
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        neu, exc_text = text, record.exc_text
        for g in self._geheimnisse:
            neu = neu.replace(g, "***")
            quoted = urllib.parse.quote(g, safe="")
            if quoted != g:
                neu = neu.replace(quoted, "***")
            if exc_text:
                exc_text = exc_text.replace(g, "***").replace(quoted, "***")
        record.msg, record.args, record.exc_text = neu, None, exc_text
        return True


# --------------------------------------------------------------------------- #
# HTTP client seam (mocked in tests)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HttpAntwort:
    """Minimal HTTP response. Header keys are lower-case."""

    status: int
    headers: Mapping[str, str]
    body: bytes


HttpGet = Callable[[str, Mapping[str, str], float, int], HttpAntwort]


def _roh_socket(resp: Any) -> Any:
    """Best-effort access to the socket under an urllib response (or None).

    ``HTTPResponse.fp`` is a ``BufferedReader`` over ``socket.SocketIO``; an
    ``HTTPError`` wraps the ``HTTPResponse`` one level deeper.
    """
    fp = getattr(resp, "fp", None)
    if isinstance(resp, urllib.error.HTTPError):
        fp = getattr(fp, "fp", None)
    return getattr(getattr(fp, "raw", None), "_sock", None)


def _lesen_mit_frist(resp: Any, max_bytes: int, ende: float) -> bytes:
    """Read at most ``max_bytes + 1`` bytes, finishing before ``ende``
    (a ``time.monotonic()`` deadline).

    ``urlopen(timeout=...)`` only bounds each single socket read, and
    ``HTTPResponse.read(n)`` loops over many socket reads until ``n`` bytes
    arrived. A slowly dripping upstream could therefore hold a thread for
    much longer than the timeout. Hence: ``read1`` (at most one socket read
    per call) and, before every read, the socket timeout is shrunk to the
    remaining time so a single blocking read cannot overshoot either.
    """
    lesen = getattr(resp, "read1", None) or resp.read
    sock = _roh_socket(resp)
    teile: list[bytes] = []
    gelesen = 0
    while gelesen <= max_bytes:
        rest = ende - time.monotonic()
        if rest <= 0:
            raise TimeoutError("total read deadline exceeded")
        if sock is not None:
            try:
                sock.settimeout(rest)
            except OSError:
                pass
        stueck = lesen(min(65536, max_bytes + 1 - gelesen))
        if not stueck:
            break
        teile.append(stueck)
        gelesen += len(stueck)
    return b"".join(teile)


def urllib_get(url: str, headers: Mapping[str, str], timeout: float, max_bytes: int) -> HttpAntwort:
    """GET ``url`` via urllib. Never raises for HTTP status codes, only for
    transport errors (including ``TimeoutError`` when the whole request takes
    longer than ``timeout``). Reads at most ``max_bytes + 1`` bytes so callers
    can detect oversized bodies."""
    ende = time.monotonic() + timeout  # total deadline: connect + headers + body
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = _lesen_mit_frist(resp, max_bytes, ende)
            kopf = {k.lower(): v for k, v in resp.headers.items()}
            return HttpAntwort(resp.status, kopf, body)
    except urllib.error.HTTPError as err:
        try:
            body = _lesen_mit_frist(err, max_bytes, ende)
        except Exception:
            body = b""
        finally:
            err.close()
        kopf = {k.lower(): v for k, v in err.headers.items()} if err.headers else {}
        return HttpAntwort(err.code, kopf, body)


def _fehlertext(exc: BaseException) -> str:
    """Short, log-safe description of an exception (class + message)."""
    nachricht = str(exc).strip()
    return f"{type(exc).__name__}: {nachricht}" if nachricht else type(exc).__name__


# --------------------------------------------------------------------------- #
# Prometheus metric model and text rendering
# --------------------------------------------------------------------------- #


def label_escape(wert: object) -> str:
    """Escape a label value for the Prometheus text format.

    Backslash, double quote and newline must be escaped; carriage returns are
    replaced by a space because the text format knows no escape for them.
    """
    s = str(wert)
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"').replace("\r", " ")


def zahl_format(wert: float) -> str:
    """Format a sample value: integers without decimals, NaN/Inf per spec."""
    if isinstance(wert, bool):
        return "1" if wert else "0"
    f = float(wert)
    if f != f:
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "+Inf" if f > 0 else "-Inf"
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return repr(f)


@dataclass(frozen=True)
class Probe:
    """One sample: label pairs (in output order) and a value."""

    labels: tuple[tuple[str, str], ...]
    wert: float


@dataclass
class Familie:
    """A metric family (one ``# HELP`` / ``# TYPE`` block)."""

    name: str
    hilfe: str
    typ: str = "gauge"
    proben: list[Probe] = field(default_factory=list)

    def add(self, wert: float, **labels: object) -> Familie:
        self.proben.append(Probe(tuple((k, str(v)) for k, v in labels.items()), float(wert)))
        return self


def gauge(name: str, hilfe: str, wert: float | None = None) -> Familie:
    """Convenience: gauge family, optionally with one unlabelled sample."""
    fam = Familie(name, hilfe, "gauge")
    if wert is not None:
        fam.add(wert)
    return fam


def rendern(familien: Iterable[Familie]) -> str:
    """Render families in Prometheus text exposition format 0.0.4."""
    zeilen: list[str] = []
    for fam in familien:
        if not fam.proben:
            continue
        hilfe = fam.hilfe.replace("\\", "\\\\").replace("\n", "\\n")
        zeilen.append(f"# HELP {fam.name} {hilfe}")
        zeilen.append(f"# TYPE {fam.name} {fam.typ}")
        for p in fam.proben:
            if p.labels:
                lbl = ",".join(f'{k}="{label_escape(v)}"' for k, v in p.labels)
                zeilen.append(f"{fam.name}{{{lbl}}} {zahl_format(p.wert)}")
            else:
                zeilen.append(f"{fam.name} {zahl_format(p.wert)}")
    return "\n".join(zeilen) + "\n"


# --------------------------------------------------------------------------- #
# Metric cache (written by the collector thread, read by scrapes)
# --------------------------------------------------------------------------- #


@dataclass
class SammelErgebnis:
    """Result of one collection round of a source.

    ``gruppen`` maps a group name to its families. Groups that failed are simply
    absent, so the cache keeps their previous values. ``fehler`` counts failed
    groups. ``konfiguriert=False`` means the source is switched off (up=0, no
    error counted).
    """

    gruppen: dict[str, list[Familie]] = field(default_factory=dict)
    fehler: int = 0
    konfiguriert: bool = True


class MetrikCache:
    """Thread-safe store of the last good values per source and group."""

    def __init__(self, quellen: Iterable[str] = QUELLEN, uhr: Callable[[], float] = time.time) -> None:
        self._lock = threading.Lock()
        self._uhr = uhr
        self._quellen = tuple(quellen)
        self._gruppen: dict[str, dict[str, list[Familie]]] = {q: {} for q in self._quellen}
        self._up: dict[str, int] = {q: 0 for q in self._quellen}
        self._fehler: dict[str, int] = {q: 0 for q in self._quellen}
        self._erfolg: dict[str, float | None] = {q: None for q in self._quellen}

    def uebernehmen(self, quelle: str, erg: SammelErgebnis) -> None:
        """Merge a round's result: replace successful groups, keep the rest."""
        with self._lock:
            self._gruppen[quelle].update(erg.gruppen)
            self._fehler[quelle] += erg.fehler
            ok = erg.konfiguriert and erg.fehler == 0
            self._up[quelle] = 1 if ok else 0
            if ok:
                self._erfolg[quelle] = self._uhr()

    def fehlschlag(self, quelle: str) -> None:
        """The whole source crashed: keep all values, count one error."""
        with self._lock:
            self._fehler[quelle] += 1
            self._up[quelle] = 0

    def familien(self) -> list[Familie]:
        """Snapshot of all cached families plus the per-source health metrics."""
        with self._lock:
            up = gauge("fred_quelle_up", "1 if the last collection of the source fully succeeded, else 0")
            fehler = Familie("fred_scrape_errors_total", "Failed collection steps per source since start", "counter")
            erfolg = gauge("fred_quelle_letzter_erfolg_timestamp_seconds",
                           "Unix time of the last fully successful collection per source")
            for q in self._quellen:
                up.add(self._up[q], quelle=q)
                fehler.add(self._fehler[q], quelle=q)
                zeitpunkt = self._erfolg[q]
                if zeitpunkt is not None:
                    erfolg.add(zeitpunkt, quelle=q)
            daten = [fam for q in self._quellen for grp in self._gruppen[q].values() for fam in grp]
        return [up, fehler, erfolg, *daten]


class Quelle(Protocol):
    """A metric source collected by the background thread."""

    name: str

    def sammeln(self) -> SammelErgebnis: ...


# --------------------------------------------------------------------------- #
# Source: Discord REST API
# --------------------------------------------------------------------------- #


class DiscordFehler(Exception):
    """Discord request failed (message is log-safe, never contains the token)."""


class DiscordQuelle:
    """Collects guild statistics via the Discord REST API (bot token)."""

    name = "discord"

    def __init__(
        self,
        token: str,
        guild_id: str,
        http_get: HttpGet = urllib_get,
        warten: Callable[[float], bool] | None = None,
        api_basis: str = DISCORD_API,
        max_bytes: int = DISCORD_MAX_BYTES,
    ) -> None:
        self._token = token
        self._guild_id = guild_id
        self._http = http_get
        self._api = api_basis.rstrip("/")
        self._max_bytes = max_bytes
        # warten(s) sleeps up to s seconds and returns True if shutdown was requested
        self._warten = warten or (lambda s: threading.Event().wait(s))

    def _kopf(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "User-Agent": DISCORD_UA,
            "Accept": "application/json",
        }

    @staticmethod
    def _retry_after(antwort: HttpAntwort) -> float:
        try:
            wert = float(json.loads(antwort.body or b"{}").get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            try:
                wert = float(antwort.headers.get("retry-after", "1"))
            except ValueError:
                wert = 1.0
        return max(0.0, wert)

    def _get_json(self, pfad: str) -> Any:
        url = self._api + pfad
        for versuch in range(1, DISCORD_MAX_VERSUCHE + 1):
            antwort = self._http(url, self._kopf(), HTTP_TIMEOUT_S, self._max_bytes)
            if antwort.status == 429:
                warte = self._retry_after(antwort)
                if warte > DISCORD_MAX_WARTEN_S or versuch == DISCORD_MAX_VERSUCHE:
                    raise DiscordFehler(f"{pfad}: rate limited (retry_after={warte:.1f}s)")
                if self._warten(warte):
                    raise DiscordFehler(f"{pfad}: aborted during shutdown")
                continue
            if antwort.status != 200:
                raise DiscordFehler(f"{pfad}: HTTP {antwort.status}")
            if len(antwort.body) > self._max_bytes:
                raise DiscordFehler(f"{pfad}: response too large")
            return json.loads(antwort.body)
        raise DiscordFehler(f"{pfad}: no attempts left")  # pragma: no cover

    def _guild_info(self) -> list[Familie]:
        g = self._get_json(f"/guilds/{self._guild_id}?with_counts=true")
        fams: list[Familie] = []

        def opt(name: str, hilfe: str, schluessel: str) -> None:
            wert = g.get(schluessel)
            if isinstance(wert, (int, float)) and not isinstance(wert, bool):
                fams.append(gauge(name, hilfe, wert))

        opt("discord_members_total", "Approximate member count of the guild", "approximate_member_count")
        opt("discord_online_total", "Approximate online (presence) count of the guild", "approximate_presence_count")
        opt("discord_boosts_total", "Number of server boosts", "premium_subscription_count")
        opt("discord_boost_tier", "Server boost tier (0-3)", "premium_tier")
        fams.append(gauge("discord_roles_total", "Number of roles", len(g.get("roles") or [])))
        fams.append(gauge("discord_emojis_total", "Number of custom emojis", len(g.get("emojis") or [])))
        return fams

    def _kanaele(self) -> list[Familie]:
        kanaele = self._get_json(f"/guilds/{self._guild_id}/channels")
        zaehler = dict.fromkeys(KANALTYP_LABELS, 0)
        for k in kanaele if isinstance(kanaele, list) else []:
            zaehler[KANALTYPEN.get(k.get("type"), "sonstige")] += 1
        fam = gauge("discord_channels_total", "Number of channels by type")
        for typ in KANALTYP_LABELS:
            fam.add(zaehler[typ], typ=typ)
        return [fam]

    def _threads(self) -> list[Familie]:
        daten = self._get_json(f"/guilds/{self._guild_id}/threads/active")
        anzahl = len(daten.get("threads") or []) if isinstance(daten, dict) else 0
        return [gauge("discord_threads_active", "Number of active threads", anzahl)]

    def sammeln(self) -> SammelErgebnis:
        if not self._token:
            return SammelErgebnis(konfiguriert=False)
        erg = SammelErgebnis()
        for gruppe, fn in (("guild", self._guild_info), ("channels", self._kanaele), ("threads", self._threads)):
            try:
                erg.gruppen[gruppe] = fn()
            except Exception as exc:
                erg.fehler += 1
                log.warning("discord %s: %s", gruppe, _fehlertext(exc))
        return erg


# --------------------------------------------------------------------------- #
# Source: MySQL (DdR)
# --------------------------------------------------------------------------- #


# One definition of "a human message" for every human metric: not a bot, not
# Fred himself (in case his rows are not flagged as bot) and not deleted.
# Contains one %s placeholder for Fred's user id.
MENSCH_BEDINGUNG = "bot = 0 AND user_id <> %s AND geloescht IS NULL"


def _zahl(wert: Any) -> float:
    """DB value (int/Decimal/None) -> float."""
    return float(wert) if wert is not None else 0.0


def utc_epoch(wert: datetime) -> float:
    """Naive DB datetime (stored as UTC) -> Unix epoch seconds."""
    if wert.tzinfo is None:
        wert = wert.replace(tzinfo=timezone.utc)
    return wert.timestamp()


class MysqlQuelle:
    """Collects Fred statistics from the DdR MySQL database.

    Every group runs its own small query; a failing group (e.g. a missing
    table) only freezes its own metrics.
    """

    name = "mysql"

    def __init__(self, cfg: Config, verbinden: Callable[[], Any] | None = None) -> None:
        self._cfg = cfg
        self._verbinden = verbinden or self._pymysql_verbinden

    def _pymysql_verbinden(self) -> Any:
        import pymysql  # type: ignore[import-untyped]  # lazy: only needed in the pod

        c = self._cfg
        return pymysql.connect(
            host=c.mysql_host,
            port=c.mysql_port,
            user=c.mysql_user,
            password=c.mysql_password,
            database=c.mysql_db,
            charset="utf8mb4",
            connect_timeout=5,
            read_timeout=20,
            write_timeout=20,
            autocommit=True,
        )

    # -- groups ------------------------------------------------------------ #

    @staticmethod
    def _eine(cur: Any, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
        cur.execute(sql, params)
        zeile = cur.fetchone()
        if zeile is None:
            raise RuntimeError("query returned no row")
        return tuple(zeile)

    def _archiv_gesamt(self, cur: Any) -> list[Familie]:
        (n,) = self._eine(cur, "SELECT COUNT(*) FROM fred_archiv")
        return [gauge("fred_archiv_nachrichten",
                      "Rows currently in fred_archiv (may shrink when rows are purged)", _zahl(n))]

    def _archiv_24h(self, cur: Any) -> list[Familie]:
        fid = self._cfg.fred_user_id
        menschen, menschen_1h, fred, aktive = self._eine(
            cur,
            "SELECT "
            f" COALESCE(SUM({MENSCH_BEDINGUNG}), 0),"
            f" COALESCE(SUM({MENSCH_BEDINGUNG}"
            "              AND zeit > UTC_TIMESTAMP() - INTERVAL 1 HOUR), 0),"
            " COALESCE(SUM(user_id = %s), 0),"
            f" COUNT(DISTINCT CASE WHEN {MENSCH_BEDINGUNG} THEN user_id END)"
            " FROM fred_archiv WHERE zeit > UTC_TIMESTAMP() - INTERVAL 24 HOUR",
            (fid, fid, fid, fid),
        )
        return [
            gauge("fred_nachrichten_24h", "Human messages (no bot, not Fred, not deleted) in the last 24 h", _zahl(menschen)),
            gauge("fred_nachrichten_1h", "Human messages (no bot, not Fred, not deleted) in the last hour", _zahl(menschen_1h)),
            gauge("fred_antworten_24h", "Messages written by Fred in the last 24 h", _zahl(fred)),
            gauge("fred_aktive_nutzer_24h", "Distinct human authors (no bot, not Fred, not deleted) in the last 24 h", _zahl(aktive)),
        ]

    def _letzte_nachricht(self, cur: Any) -> list[Familie]:
        (zeit,) = self._eine(cur, "SELECT MAX(zeit) FROM fred_archiv")
        if zeit is None:
            return []
        return [gauge("fred_letzte_nachricht_timestamp_seconds",
                      "Unix time (UTC) of the newest archived message", utc_epoch(zeit))]

    def _voice_aktiv(self, cur: Any) -> list[Familie]:
        cur.execute(
            "SELECT kanal, COUNT(*) FROM fred_voice_sitzungen"
            " WHERE ende IS NULL AND zuletzt > UTC_TIMESTAMP() - INTERVAL 5 MINUTE"
            " GROUP BY kanal"
        )
        je_kanal = gauge("fred_voice_aktiv_kanal", "Running voice sessions per channel")
        gesamt = 0.0
        for kanal, anzahl in cur.fetchall():
            je_kanal.add(_zahl(anzahl), kanal=kanal if kanal is not None else "")
            gesamt += _zahl(anzahl)
        return [gauge("fred_voice_aktiv", "Running voice sessions", gesamt), je_kanal]

    def _voice_7d(self, cur: Any) -> list[Familie]:
        (summe,) = self._eine(
            cur,
            "SELECT COALESCE(SUM(dauer_s), 0) FROM fred_voice_sitzungen"
            " WHERE ende IS NOT NULL AND ende > UTC_TIMESTAMP() - INTERVAL 7 DAY",
        )
        return [gauge("fred_voice_stunden_7d", "Voice hours of sessions ended in the last 7 days",
                      _zahl(summe) / 3600.0)]

    def _verdacht(self, cur: Any) -> list[Familie]:
        (n,) = self._eine(cur, "SELECT COUNT(*) FROM fred_verdacht WHERE gesperrt_bis > UTC_TIMESTAMP()")
        return [gauge("fred_verdacht_gesperrt", "Currently blocked suspects", _zahl(n))]

    def _profile(self, cur: Any) -> list[Familie]:
        (n,) = self._eine(cur, "SELECT COUNT(*) FROM fred_profile")
        return [gauge("fred_profile_total", "Stored user profiles", _zahl(n))]

    def _gehirn_personen(self, cur: Any) -> list[Familie]:
        (n,) = self._eine(cur, "SELECT COUNT(*) FROM fred_gehirn_personen")
        return [gauge("fred_gehirn_personen_total", "Persons in Fred's brain", _zahl(n))]

    def _gehirn_kanaele(self, cur: Any) -> list[Familie]:
        (n,) = self._eine(cur, "SELECT COUNT(*) FROM fred_gehirn_kanaele")
        return [gauge("fred_gehirn_kanaele_total", "Channels in Fred's brain", _zahl(n))]

    def _levels(self, cur: Any) -> list[Familie]:
        cur.execute("SELECT user_id, username, xp FROM mee6_levels ORDER BY xp DESC LIMIT 10")
        fam = gauge("fred_levels_top", "XP of the top 10 members (mee6_levels)")
        for rang, (user_id, name, xp) in enumerate(cur.fetchall(), start=1):
            fam.add(_zahl(xp), rang=rang, name=name if name else str(user_id))
        return [fam]

    def _gruppen(self) -> tuple[tuple[str, Callable[[Any], list[Familie]]], ...]:
        return (
            ("archiv_gesamt", self._archiv_gesamt),
            ("archiv_24h", self._archiv_24h),
            ("letzte_nachricht", self._letzte_nachricht),
            ("voice_aktiv", self._voice_aktiv),
            ("voice_7d", self._voice_7d),
            ("verdacht", self._verdacht),
            ("profile", self._profile),
            ("gehirn_personen", self._gehirn_personen),
            ("gehirn_kanaele", self._gehirn_kanaele),
            ("levels", self._levels),
        )

    def sammeln(self) -> SammelErgebnis:
        if not self._cfg.mysql_user:
            return SammelErgebnis(konfiguriert=False)
        try:
            conn = self._verbinden()
        except Exception as exc:
            log.warning("mysql connect: %s", _fehlertext(exc))
            return SammelErgebnis(fehler=1)
        erg = SammelErgebnis()
        try:
            for gruppe, fn in self._gruppen():
                try:
                    cur = conn.cursor()
                    try:
                        erg.gruppen[gruppe] = fn(cur)
                    finally:
                        cur.close()
                except Exception as exc:
                    erg.fehler += 1
                    log.warning("mysql %s: %s", gruppe, _fehlertext(exc))
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return erg


# --------------------------------------------------------------------------- #
# Camera relay
# --------------------------------------------------------------------------- #


@dataclass
class _KameraEintrag:
    bild: bytes | None = None
    geholt: float | None = None   # monotonic time of last successful fetch
    versuch: float | None = None  # monotonic time of last attempt (success or not)
    ok: bool = False
    hol_lock: threading.Lock = field(default_factory=threading.Lock)


def platzhalter_svg(name: str) -> bytes:
    """Small "Kamera offline" placeholder so the Grafana panel stays tidy."""
    n = html.escape(name, quote=True)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360" viewBox="0 0 640 360">'
        '<rect width="640" height="360" fill="#1f2328"/>'
        '<text x="320" y="172" fill="#c9d1d9" font-family="sans-serif" font-size="30" '
        'text-anchor="middle">Kamera offline</text>'
        f'<text x="320" y="212" fill="#8b949e" font-family="sans-serif" font-size="18" '
        f'text-anchor="middle">{n}</text></svg>'
    ).encode()


class KameraStore:
    """In-memory camera cache with one upstream fetch per camera at a time.

    Two locks per design: a global, short-held ``_zustand`` lock guards the
    cached data (so scrapes and the JSON overview never wait on the network),
    and a per-camera ``hol_lock`` serialises upstream fetches. Failed attempts
    are cached too (negative caching), so a dead upstream is asked at most
    once per ``cache_s``.
    """

    def __init__(
        self,
        ha_base: str,
        ordner: str,
        http_get: HttpGet = urllib_get,
        uhr: Callable[[], float] = time.monotonic,
        cache_s: float = KAMERA_CACHE_S,
        max_bytes: int = KAMERA_MAX_BYTES,
    ) -> None:
        self._basis = ha_base.rstrip("/")
        self._ordner = ordner.strip("/")
        self._http = http_get
        self._uhr = uhr
        self._cache_s = cache_s
        self._max_bytes = max_bytes
        self._zustand = threading.Lock()
        self._eintraege = {name: _KameraEintrag() for name in KAMERAS}

    @property
    def konfiguriert(self) -> bool:
        return bool(self._basis and self._ordner)

    def _url(self, name: str) -> str:
        return f"{self._basis}/local/{urllib.parse.quote(self._ordner, safe='')}/{name}.jpg"

    def _frisch(self, e: _KameraEintrag, jetzt: float) -> bool:
        return e.versuch is not None and jetzt - e.versuch < self._cache_s

    def _upstream(self, name: str) -> bytes | None:
        """Fetch one camera image; returns validated JPEG bytes or None.

        Log lines only ever contain the camera name and a status/reason –
        never the URL, which contains the secret folder name.
        """
        if not self.konfiguriert:
            return None
        try:
            antwort = self._http(self._url(name), {"User-Agent": "fred-relay/1.0"},
                                 HTTP_TIMEOUT_S, self._max_bytes)
        except Exception as exc:
            log.warning("kamera %s: upstream error (%s)", name, type(exc).__name__)
            return None
        if antwort.status != 200:
            log.warning("kamera %s: upstream HTTP %d", name, antwort.status)
            return None
        if len(antwort.body) > self._max_bytes:
            log.warning("kamera %s: image larger than %d bytes", name, self._max_bytes)
            return None
        if not antwort.body.startswith(JPEG_MAGIC):
            log.warning("kamera %s: upstream did not return a JPEG", name)
            return None
        return antwort.body

    def holen(self, name: str) -> bytes | None:
        """Return the current JPEG for ``name`` (possibly stale) or None.

        Raises:
            KeyError: if ``name`` is not a whitelisted camera.
        """
        e = self._eintraege[name]  # KeyError for anything off the whitelist
        with self._zustand:
            if self._frisch(e, self._uhr()):
                return e.bild
            hat_altbild = e.bild is not None
        # A fetch is already running: with an older image at hand, serve that
        # immediately instead of queueing behind a possibly hanging upstream.
        # Only without any image is it worth waiting for the running fetch.
        if not e.hol_lock.acquire(blocking=not hat_altbild):
            with self._zustand:
                return e.bild
        try:
            with self._zustand:  # someone else may have fetched meanwhile
                if self._frisch(e, self._uhr()):
                    return e.bild
            bild = self._upstream(name)
            with self._zustand:
                jetzt = self._uhr()
                e.versuch = jetzt
                e.ok = bild is not None
                if bild is not None:
                    e.bild, e.geholt = bild, jetzt
                return e.bild
        finally:
            e.hol_lock.release()

    def ok(self, name: str) -> bool:
        with self._zustand:
            return self._eintraege[name].ok

    def status(self) -> dict[str, dict[str, Any]]:
        """JSON-able overview ``{name: {alter_s, ok}}`` (never blocks on I/O)."""
        with self._zustand:
            jetzt = self._uhr()
            return {
                name: {
                    "alter_s": round(jetzt - e.geholt, 1) if e.geholt is not None else None,
                    "ok": e.ok,
                }
                for name, e in self._eintraege.items()
            }

    def familien(self) -> list[Familie]:
        """Live camera freshness metrics (computed at scrape time)."""
        alter = gauge("fred_kamera_alter_sekunden", "Age of the last successfully fetched camera image")
        groesse = gauge("fred_kamera_bytes", "Size of the last successfully fetched camera image")
        ok = gauge("fred_kamera_ok", "1 if the last fetch attempt of the camera succeeded")
        with self._zustand:
            jetzt = self._uhr()
            for name, e in self._eintraege.items():
                ok.add(1 if e.ok else 0, kamera=name)
                if e.geholt is not None and e.bild is not None:
                    alter.add(max(0.0, jetzt - e.geholt), kamera=name)
                    groesse.add(len(e.bild), kamera=name)
        return [alter, groesse, ok]


class KameraQuelle:
    """Background refresh of all cameras so freshness metrics stay meaningful."""

    name = "kamera"

    def __init__(self, store: KameraStore) -> None:
        self._store = store

    def sammeln(self) -> SammelErgebnis:
        """Refresh all cameras in parallel: one hanging camera (or a hanging
        Home Assistant) costs one fetch timeout per round, not one per camera."""
        if not self._store.konfiguriert:
            return SammelErgebnis(konfiguriert=False)
        with ThreadPoolExecutor(max_workers=len(KAMERAS), thread_name_prefix="kamera") as pool:
            list(pool.map(self._store.holen, KAMERAS))  # holen() never raises for whitelisted names
        fehler = sum(1 for name in KAMERAS if not self._store.ok(name))
        return SammelErgebnis(fehler=fehler)


# --------------------------------------------------------------------------- #
# Application wiring
# --------------------------------------------------------------------------- #


def ha_basis_pruefen(basis: str) -> str:
    """Return ``basis`` if it is an ``https://`` URL with a host, else ``""``.

    The camera URL carries the secret folder name, so it must never travel
    in clear text. A non-https ``HA_BASE`` switches the camera source off
    (reported as not configured) and is logged once - without the value.
    """
    if not basis:
        return ""
    teile = urllib.parse.urlsplit(basis)
    if teile.scheme.lower() == "https" and teile.netloc:
        return basis
    log.warning("HA_BASE ignored: it must be an https:// URL; camera source disabled")
    return ""


class App:
    """Glue between sources, caches and the HTTP handler."""

    def __init__(self, quellen: list[Quelle], store: KameraStore, cache: MetrikCache) -> None:
        self.quellen = quellen
        self.store = store
        self.cache = cache
        self._letzter_text = "# fred-relay: no data yet\n"
        self._text_lock = threading.Lock()

    @classmethod
    def aus_config(cls, cfg: Config, stop: threading.Event,
                   http_get: HttpGet = urllib_get) -> App:
        store = KameraStore(ha_basis_pruefen(cfg.ha_base), cfg.ha_kamera_ordner, http_get=http_get)
        quellen: list[Quelle] = [
            DiscordQuelle(cfg.discord_token, cfg.discord_guild_id, http_get=http_get, warten=stop.wait),
            MysqlQuelle(cfg),
            KameraQuelle(store),
        ]
        return cls(quellen, store, MetrikCache(q.name for q in quellen))

    def metriken_text(self) -> str:
        """Current exposition text. Never raises: on a rendering bug the last
        good text is returned so a scrape never fails."""
        try:
            text = rendern(self.cache.familien() + self.store.familien())
        except Exception as exc:
            log.error("metrics render failed: %s", _fehlertext(exc))
            with self._text_lock:
                return self._letzter_text
        with self._text_lock:
            self._letzter_text = text
        return text

    def _sammeln_quelle(self, q: Quelle) -> None:
        """Collect one source and publish its result right away; never raises."""
        try:
            erg = q.sammeln()
        except Exception as exc:
            log.warning("%s: collection crashed: %s", q.name, _fehlertext(exc))
            self.cache.fehlschlag(q.name)
        else:
            self.cache.uebernehmen(q.name, erg)

    def sammeln_einmal(self) -> None:
        """One collection round over all sources; never raises.

        Sources run concurrently and each publishes as soon as it is done, so a
        hanging upstream (e.g. Home Assistant) never delays the Discord or
        MySQL values. The round itself ends when the slowest source is done.
        """
        if not self.quellen:
            return
        with ThreadPoolExecutor(max_workers=len(self.quellen), thread_name_prefix="quelle") as pool:
            list(pool.map(self._sammeln_quelle, self.quellen))

    def sammel_schleife(self, stop: threading.Event, intervall_s: float) -> None:
        """Collector thread: collect immediately, then every ``intervall_s``."""
        while not stop.is_set():
            self.sammeln_einmal()
            stop.wait(intervall_s)


_KAMERA_PFAD = re.compile(r"^/kamera/([a-z]+)\.jpg$")


def handler_klasse(app: App) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class bound to ``app``."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "fred-relay/1.0"
        sys_version = ""
        # socket timeout per connection: a client that sends its request line
        # or headers byte by byte (slowloris) cannot pin a thread forever
        timeout = 15
        _nur_kopf = False  # True while answering HEAD: headers only, no body

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # access log disabled; errors are logged explicitly

        def _senden(self, status: int, typ: str, body: bytes,
                    extra: Mapping[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if not self._nur_kopf:
                self.wfile.write(body)

        def _nicht_gefunden(self) -> None:
            self._senden(404, "text/plain; charset=utf-8", b"not found\n")

        def do_HEAD(self) -> None:  # noqa: N802 (http.server API)
            """Same status and headers as GET (incl. Content-Length), no body."""
            self._nur_kopf = True
            try:
                self.do_GET()
            finally:
                self._nur_kopf = False

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            # Raw path without query; deliberately NOT unquoted or normalised,
            # so "/kamera/../x.jpg" or "%2e%2e" simply do not match any route.
            pfad = self.path.split("?", 1)[0].split("#", 1)[0]
            try:
                if pfad == "/metrics":
                    self._senden(200, METRICS_CONTENT_TYPE, app.metriken_text().encode("utf-8"),
                                 {"Cache-Control": "no-store"})
                elif pfad == "/healthz":
                    self._senden(200, "text/plain; charset=utf-8", b"ok")
                elif pfad in ("/kamera", "/kamera/"):
                    body = json.dumps(app.store.status(), ensure_ascii=False).encode("utf-8")
                    self._senden(200, "application/json; charset=utf-8", body,
                                 {"Cache-Control": "no-store"})
                else:
                    treffer = _KAMERA_PFAD.match(pfad)
                    if treffer is None or treffer.group(1) not in KAMERAS:
                        self._nicht_gefunden()
                        return
                    self._kamera(treffer.group(1))
            except (BrokenPipeError, ConnectionResetError):
                pass  # client went away
            except Exception as exc:
                log.error("request %s failed: %s", pfad[:64], type(exc).__name__)
                try:
                    self._senden(500, "text/plain; charset=utf-8", b"internal error\n")
                except Exception:
                    pass

        def _kamera(self, name: str) -> None:
            bild = app.store.holen(name)
            if bild is not None:
                self._senden(200, "image/jpeg", bild, {"Cache-Control": "private, max-age=15"})
            else:
                self._senden(200, "image/svg+xml", platzhalter_svg(name), {
                    "Cache-Control": "private, max-age=5",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
                })

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@dataclass
class Aufbau:
    """Everything ``main()`` needs besides the listening socket."""

    cfg: Config
    app: App
    stop: threading.Event
    schwaerzer: Schwaerzer


def einrichten(env: Mapping[str, str] | None = None, http_get: HttpGet = urllib_get) -> Aufbau:
    """Read the config, install the secret-redacting log filter and build the
    app. Split from ``main()`` so the wiring is testable without a server.

    Raises:
        ValueError: on invalid configuration (message is log-safe).
    """
    cfg = Config.aus_env(env)
    schwaerzer = Schwaerzer(cfg.geheimnisse())
    log.addFilter(schwaerzer)  # before anything else can log a secret
    stop = threading.Event()
    app = App.aus_config(cfg, stop, http_get=http_get)
    for name, aktiv in (("discord", bool(cfg.discord_token)), ("mysql", bool(cfg.mysql_user)),
                        ("kamera", app.store.konfiguriert)):
        if not aktiv:
            log.warning("source %s not configured (missing env), reported as down", name)
    return Aufbau(cfg, app, stop, schwaerzer)


def main() -> int:
    logging.basicConfig(
        stream=sys.stdout,
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        aufbau = einrichten()
    except ValueError as exc:
        log.error("configuration error: %s", exc)
        return 2
    cfg, app, stop = aufbau.cfg, aufbau.app, aufbau.stop

    server = _Server(("0.0.0.0", cfg.port), handler_klasse(app))
    sammler = threading.Thread(target=app.sammel_schleife, args=(stop, cfg.intervall_s),
                               name="sammler", daemon=True)

    def beenden(signum: int, _frame: Any) -> None:
        log.info("signal %d received, shutting down", signum)
        stop.set()
        # shutdown() blocks until serve_forever returns -> must not run in the
        # thread that executes serve_forever (the main thread, here)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, beenden)
    signal.signal(signal.SIGINT, beenden)

    sammler.start()
    log.info("fred-relay listening on :%d (interval %.0fs)", cfg.port, cfg.intervall_s)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        sammler.join(timeout=15)
    log.info("fred-relay stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
