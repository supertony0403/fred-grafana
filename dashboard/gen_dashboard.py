#!/usr/bin/env python3
"""Erzeugt das Grafana-Dashboard "Fred · Command Center" (uid fred-command-center).

Ausgabe: JSON auf stdout. deploy.sh verpackt es in die ConfigMap grafana-dashboard-fred
(Label grafana_dashboard=1), die der Grafana-Sidecar automatisch lädt.

Datenquellen (uids):
  prometheus – kube-prometheus-stack
  fred-ddr   – MySQL DdR, Nutzer grafana_ro (nur SELECT auf ausgewählte Tabellen)
  infinity   – JSON-APIs (USGS, ISS, Jarvis-Reich), Hosts per allowedHosts begrenzt
"""
import json
import sys

PROM = {"type": "prometheus", "uid": "prometheus"}
SQL = {"type": "mysql", "uid": "fred-ddr"}
# grafana_ro liest nur die View fred_archiv_oeffentlich (sql/fred_archiv_oeffentlich.sql):
# ohne gelöschte Nachrichten, ohne Team-/Log-/Ticket-Kanäle, Text auf 300 Zeichen gekürzt.
DASH = {"type": "datasource", "uid": "-- Dashboard --"}
INF = {"type": "yesoreyeram-infinity-datasource", "uid": "infinity"}

FRED_ID = "1413591205047959674"
MENSCH = f"bot = 0 AND user_id <> {FRED_ID} AND geloescht IS NULL"

# Farbwelt: tiefes Nachtblau, Fred-Violett, Signal-Cyan.
VIOLETT = "#8b5cf6"
CYAN = "#22d3ee"
GRUEN = "#22c55e"
ORANGE = "#f59e0b"
ROT = "#ef4444"

_next_id = 0


def pid():
    global _next_id
    _next_id += 1
    return _next_id


def pos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def prom(expr, legend="", ref="A", instant=False, fmt="time_series"):
    t = {"datasource": PROM, "refId": ref, "expr": expr, "legendFormat": legend, "format": fmt}
    if instant:
        t["instant"] = True
        t["range"] = False
    return t


def sql(query, ref="A", fmt="table"):
    return {"datasource": SQL, "refId": ref, "rawQuery": True, "editorMode": "code",
            "format": fmt, "rawSql": " ".join(query.split())}


def infinity(url, columns, root="", ref="A", parser="backend", source="url", data=""):
    t = {"datasource": INF, "refId": ref, "type": "json", "source": source, "format": "table",
         "parser": parser, "root_selector": root,
         "columns": [{"selector": s, "text": n, "type": ty} for s, n, ty in columns],
         "filters": [], "url_options": {"method": "GET", "data": ""}}
    if source == "url":
        t["url"] = url
    else:
        t["data"] = data
    return t


def row(title, y, collapsed=False, panels=None):
    r = {"type": "row", "id": pid(), "title": title, "gridPos": pos(0, y, 24, 1),
         "collapsed": collapsed, "panels": panels or []}
    return r


def stat(title, targets, gp, unit="short", color=VIOLETT, mappings=None, thresholds=None,
         desc="", graph=True, decimals=None):
    d = {"unit": unit, "color": {"mode": "fixed", "fixedColor": color},
         "mappings": mappings or [],
         "thresholds": thresholds or {"mode": "absolute", "steps": [{"color": color, "value": None}]}}
    if decimals is not None:
        d["decimals"] = decimals
    if thresholds:
        d["color"] = {"mode": "thresholds"}
    return {"type": "stat", "id": pid(), "title": title, "description": desc, "gridPos": gp,
            "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background_solid" if thresholds else "value",
                        "graphMode": "area" if graph else "none", "justifyMode": "center",
                        "textMode": "value", "wideLayout": True, "showPercentChange": False}}


def timeseries(title, targets, gp, unit="short", stack=False, bars=False, colors=None,
               desc="", fill=18, interval=None, time_from=None, legend="bottom"):
    p = {"type": "timeseries", "id": pid(), "title": title, "description": desc, "gridPos": gp,
         "targets": targets,
         "fieldConfig": {"defaults": {
             "unit": unit, "color": {"mode": "palette-classic"},
             "custom": {"drawStyle": "bars" if bars else "line", "lineWidth": 2,
                        "fillOpacity": 70 if bars else fill, "gradientMode": "opacity",
                        "lineInterpolation": "smooth", "showPoints": "never",
                        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
                        "barAlignment": 0, "axisSoftMin": 0}},
             "overrides": [
                 {"matcher": {"id": "byName", "options": n},
                  "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                 for n, c in (colors or {}).items()]},
         "options": {"legend": {"displayMode": "list", "placement": legend, "showLegend": legend != "hidden"},
                     "tooltip": {"mode": "multi", "sort": "desc"}}}
    if interval:
        p["interval"] = interval
    if time_from:
        p["timeFrom"] = time_from
        p["hideTimeOverride"] = False
    return p


def bargauge(title, targets, gp, field, unit="short", scheme="continuous-BlPu", desc="", decimals=0):
    return {"type": "bargauge", "id": pid(), "title": title, "description": desc, "gridPos": gp,
            "targets": targets,
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "min": 0,
                                         "color": {"mode": scheme}}, "overrides": []},
            "options": {"reduceOptions": {"values": True, "calcs": [], "fields": f"/^{field}$/"},
                        "orientation": "horizontal", "displayMode": "gradient",
                        "valueMode": "color", "namePlacement": "left", "showUnfilled": True,
                        "sizing": "auto", "minVizHeight": 14, "maxVizHeight": 28}}


def table(title, targets, gp, desc="", overrides=None, transformations=None, sort=None):
    p = {"type": "table", "id": pid(), "title": title, "description": desc, "gridPos": gp,
         "targets": targets,
         "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                                 "inspect": True}},
                         "overrides": overrides or []},
         "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                     "sortBy": sort or []},
         "transformations": transformations or []}
    return p


def text(title, html, gp, transparent=False):
    return {"type": "text", "id": pid(), "title": title, "gridPos": gp, "transparent": transparent,
            "options": {"mode": "html", "content": html}}


def ov(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def gauge_node(title, expr, gp, unit="percent", maxv=100, steps=None):
    return {"type": "gauge", "id": pid(), "title": title, "gridPos": gp,
            "targets": [prom(expr, "{{nodename}}", instant=True)],
            "fieldConfig": {"defaults": {
                "unit": unit, "min": 0, "max": maxv, "decimals": 0,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps or [
                    {"color": GRUEN, "value": None}, {"color": ORANGE, "value": 70},
                    {"color": ROT, "value": 90}]}}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "showThresholdMarkers": True, "showThresholdLabels": False,
                        "sizing": "auto", "minVizWidth": 75, "minVizHeight": 75}}


# ---------------------------------------------------------------- Kopf
BANNER = f"""
<style>
.fcc{{position:relative;height:100%;display:flex;align-items:center;gap:22px;padding:0 22px;
 border-radius:10px;overflow:hidden;font-family:Inter,system-ui,sans-serif;
 background:radial-gradient(1200px 200px at 0% 0%,rgba(139,92,246,.35),transparent 60%),
 radial-gradient(900px 220px at 100% 100%,rgba(34,211,238,.22),transparent 60%),#0b1020;}}
.fcc:after{{content:"";position:absolute;inset:0;pointer-events:none;
 background:repeating-linear-gradient(0deg,rgba(255,255,255,.025) 0 1px,transparent 1px 3px);}}
.fcc .kern{{width:54px;height:54px;border-radius:50%;flex:none;
 background:radial-gradient(circle at 35% 35%,#c4b5fd,{VIOLETT} 45%,#312e81 80%);
 box-shadow:0 0 24px {VIOLETT},0 0 60px rgba(34,211,238,.35);animation:fccp 3.2s ease-in-out infinite;}}
@keyframes fccp{{0%,100%{{transform:scale(1);box-shadow:0 0 18px {VIOLETT},0 0 40px rgba(34,211,238,.25)}}
 50%{{transform:scale(1.07);box-shadow:0 0 34px {VIOLETT},0 0 80px rgba(34,211,238,.5)}}}}
.fcc h1{{margin:0;font-size:28px;letter-spacing:.14em;font-weight:800;color:#f5f3ff;line-height:1}}
.fcc h1 b{{color:{CYAN};font-weight:800}}
.fcc p{{margin:6px 0 0;color:#a5b4fc;font-size:13px;letter-spacing:.04em}}
.fcc nav{{margin-left:auto;display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}}
.fcc nav a{{color:#e0e7ff;text-decoration:none;font-size:12px;padding:6px 11px;border-radius:999px;
 border:1px solid rgba(165,180,252,.35);background:rgba(15,23,42,.6)}}
.fcc nav a:hover{{border-color:{CYAN};color:{CYAN}}}
@media (max-width:900px){{.fcc nav{{display:none}}.fcc h1{{font-size:20px}}}}
</style>
<div class="fcc"><div class="kern"></div>
<div><h1>FRED <b>·</b> COMMAND CENTER</h1>
<p>Dialog der Religionen · Discord · Weltlage · Kameras · Homelab · Jarvis</p></div>
<nav>
<a href="https://jarvis.benz-sw.de/jarvis" target="_blank" rel="noopener">🤖 Jarvis</a>
<a href="https://monitor.benz-sw.de" target="_blank" rel="noopener">🌍 Lagebild</a>
<a href="/d/sissyphus-aio" >🚀 Sissyphus CC</a>
<a href="/d/openziti-overlay">🛡️ Ziti</a>
<a href="https://headlamp.benz-sw.de" target="_blank" rel="noopener">☸️ Headlamp</a>
<a href="https://gitea.benz-sw.de" target="_blank" rel="noopener">🐙 Gitea</a>
</nav></div>
"""


def kopf(y):
    ps = [text("", BANNER, pos(0, y, 24, 4), transparent=True)]
    y += 4
    online_map = [{"type": "range", "options": {"from": 1, "to": 99,
                                                  "result": {"text": "ONLINE", "color": GRUEN}}},
                  {"type": "value", "options": {"0": {"text": "OFFLINE", "color": ROT}}}]
    ps.append(stat("Fred", [prom('max(kube_deployment_status_replicas_available{namespace="discord",deployment="ticket-bot"}) or vector(0)', instant=True)],
                   pos(0, y, 3, 4), mappings=online_map, graph=False,
                   thresholds={"mode": "absolute", "steps": [{"color": ROT, "value": None}, {"color": GRUEN, "value": 1}]},
                   desc="Läuft der Bot-Pod ticket-bot (ns discord)?"))
    ps.append(stat("Mitglieder", [prom("max(discord_members_total)")], pos(3, y, 3, 4), color=VIOLETT,
                   desc="Discord-REST approximate_member_count (fred-relay)"))
    ps.append(stat("Online", [prom("max(discord_online_total)")], pos(6, y, 3, 4), color=GRUEN,
                   desc="Discord-REST approximate_presence_count"))
    ps.append(stat("Nachrichten 24 h", [sql(f"SELECT COUNT(*) AS n FROM fred_archiv_oeffentlich WHERE zeit > UTC_TIMESTAMP() - INTERVAL 1 DAY AND {MENSCH}")],
                   pos(9, y, 3, 4), color=CYAN, graph=False, desc="Menschen, ohne Bots, ohne gelöschte"))
    ps.append(stat("Fred antwortet 24 h", [sql(f"SELECT COUNT(*) AS n FROM fred_archiv_oeffentlich WHERE zeit > UTC_TIMESTAMP() - INTERVAL 1 DAY AND user_id = {FRED_ID}")],
                   pos(12, y, 3, 4), color=VIOLETT, graph=False))
    ps.append(stat("Aktive Köpfe 24 h", [sql(f"SELECT COUNT(DISTINCT user_id) AS n FROM fred_archiv_oeffentlich WHERE zeit > UTC_TIMESTAMP() - INTERVAL 1 DAY AND {MENSCH}")],
                   pos(15, y, 3, 4), color=ORANGE, graph=False))
    ps.append(stat("Im Voice jetzt", [sql("SELECT COUNT(*) AS n FROM fred_voice_sitzungen WHERE ende IS NULL AND zuletzt > UTC_TIMESTAMP() - INTERVAL 5 MINUTE")],
                   pos(18, y, 3, 4), color=GRUEN, graph=False))
    ps.append(stat("Archiv gesamt", [prom("max(fred_archiv_nachrichten)")],
                   pos(21, y, 3, 4), color="#94a3b8", graph=False, desc="Alle archivierten Nachrichten seit 2023"))
    return ps, y + 4


# ---------------------------------------------------------------- Discord
def discord(y):
    ps = [row("💬 Discord · Dialog der Religionen · Fred", y)]
    y += 1
    ps.append(timeseries(
        "Nachrichten pro Stunde",
        [sql(f"""SELECT $__timeGroupAlias(zeit, '1h'),
                 SUM(bot = 0 AND user_id <> {FRED_ID}) AS Menschen,
                 SUM(user_id = {FRED_ID}) AS Fred
                 FROM fred_archiv_oeffentlich WHERE $__timeFilter(zeit) AND geloescht IS NULL
                 GROUP BY 1 ORDER BY 1""", fmt="time_series")],
        pos(0, y, 12, 9), bars=True, stack=True, colors={"Menschen": CYAN, "Fred": VIOLETT}))
    ps.append(timeseries(
        "Nachrichten pro Tag · 90 Tage",
        [sql(f"""SELECT $__timeGroupAlias(zeit, '1d'), COUNT(*) AS Nachrichten,
                 COUNT(DISTINCT user_id) AS Köpfe
                 FROM fred_archiv_oeffentlich WHERE $__timeFilter(zeit) AND {MENSCH}
                 GROUP BY 1 ORDER BY 1""", fmt="time_series")],
        pos(12, y, 12, 9), bars=True, time_from="90d", colors={"Nachrichten": VIOLETT, "Köpfe": ORANGE}))
    y += 9
    ps.append(bargauge("🏆 Top-Schreiber (Zeitraum)",
                       [sql(f"""SELECT MAX(name) AS Name, COUNT(*) AS Nachrichten FROM fred_archiv_oeffentlich
                                WHERE $__timeFilter(zeit) AND {MENSCH}
                                GROUP BY user_id ORDER BY Nachrichten DESC LIMIT 12""")],
                       pos(0, y, 6, 13), "Nachrichten", scheme="continuous-BlPu"))
    ps.append(bargauge("📢 Aktivste Kanäle (Zeitraum)",
                       [sql(f"""SELECT MAX(kanal) AS Kanal, COUNT(*) AS Nachrichten FROM fred_archiv_oeffentlich
                                WHERE $__timeFilter(zeit) AND {MENSCH}
                                GROUP BY kanal_id ORDER BY Nachrichten DESC LIMIT 12""")],
                       pos(6, y, 6, 13), "Nachrichten", scheme="continuous-GrYlRd"))
    tage = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    spalten = ", ".join(f"SUM(WEEKDAY(t) = {i}) AS {d}" for i, d in enumerate(tage))
    punch = table(
        "🔥 Wann ist Betrieb? · 30 Tage, Ortszeit",
        [sql(f"""SELECT LPAD(HOUR(t), 2, '0') AS Uhr, {spalten} FROM
                 (SELECT CONVERT_TZ(zeit, '+00:00', 'Europe/Berlin') AS t FROM fred_archiv_oeffentlich
                  WHERE zeit > UTC_TIMESTAMP() - INTERVAL 30 DAY AND {MENSCH}) x
                 GROUP BY 1 ORDER BY 1""")],
        pos(12, y, 12, 13),
        desc="Nachrichten je Wochentag und Stunde der letzten 30 Tage (Europe/Berlin).")
    punch["fieldConfig"]["defaults"]["custom"]["cellOptions"] = {"type": "color-background", "mode": "gradient"}
    punch["fieldConfig"]["defaults"]["custom"]["align"] = "center"
    punch["fieldConfig"]["defaults"]["custom"]["minWidth"] = 40
    punch["fieldConfig"]["defaults"]["custom"]["width"] = 96
    punch["fieldConfig"]["defaults"]["color"] = {"mode": "continuous-purples"}
    punch["fieldConfig"]["overrides"] = [ov("Uhr", [
        {"id": "custom.cellOptions", "value": {"type": "auto"}}, {"id": "custom.width", "value": 52}])]
    ps.append(punch)
    y += 13
    ps.append(table("🎙️ Gerade im Voice",
                    [sql("""SELECT name AS Wer, kanal AS Kanal,
                            TIMESTAMPDIFF(MINUTE, beginn, UTC_TIMESTAMP()) AS Minuten
                            FROM fred_voice_sitzungen
                            WHERE ende IS NULL AND zuletzt > UTC_TIMESTAMP() - INTERVAL 5 MINUTE
                            ORDER BY beginn""")],
                    pos(0, y, 8, 9),
                    overrides=[ov("Minuten", [{"id": "unit", "value": "m"},
                                              {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "lcd"}},
                                              {"id": "max", "value": 240},
                                              {"id": "color", "value": {"mode": "continuous-GrYlRd"}}])]))
    ps.append(bargauge("⏱️ Voice-Zeit gesamt · Top 10",
                       [sql("""SELECT name AS Name, ROUND(sekunden / 3600, 1) AS Stunden
                               FROM fred_voice_summe ORDER BY sekunden DESC LIMIT 10""")],
                       pos(8, y, 8, 9), "Stunden", unit="suffix: h", scheme="continuous-GrYlRd", decimals=1))
    ps.append(table("⭐ Level-Rangliste",
                    [sql("""SELECT username AS Name, level AS Level, xp AS XP, message_count AS Nachrichten
                            FROM mee6_levels ORDER BY xp DESC LIMIT 15""")],
                    pos(16, y, 8, 9),
                    overrides=[ov("XP", [{"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}},
                                         {"id": "color", "value": {"mode": "continuous-BlPu"}}]),
                               ov("Level", [{"id": "custom.width", "value": 60}])]))
    y += 9
    ps.append(table("📡 Live-Ticker · letzte Nachrichten",
                    [sql(f"""SELECT zeit AS Zeit, name AS Wer, kanal AS Kanal,
                            LEFT(REPLACE(REPLACE(text, '\\n', ' '), '\\r', ' '), 180) AS Nachricht
                            FROM fred_archiv_oeffentlich WHERE geloescht IS NULL
                            AND (bot = 0 OR (user_id = {FRED_ID} AND kanal NOT LIKE '%log%'))
                            ORDER BY zeit DESC LIMIT 25""")],
                    pos(0, y, 16, 11),
                    overrides=[ov("Zeit", [{"id": "unit", "value": "dateTimeFromNow"}, {"id": "custom.width", "value": 110}]),
                               ov("Wer", [{"id": "custom.width", "value": 150}]),
                               ov("Kanal", [{"id": "custom.width", "value": 190}])]))
    ps.append(table("🧾 Bot-Ereignisse",
                    [sql("""SELECT timestamp AS Zeit, event_type AS Typ, severity AS Stufe,
                            description AS Beschreibung FROM system_events ORDER BY id DESC LIMIT 15""")],
                    pos(16, y, 8, 11),
                    overrides=[ov("Zeit", [{"id": "unit", "value": "dateTimeFromNow"}, {"id": "custom.width", "value": 100}]),
                               ov("Stufe", [{"id": "custom.width", "value": 70},
                                            {"id": "mappings", "value": [{"type": "value", "options": {
                                                "INFO": {"color": CYAN, "index": 0},
                                                "WARNING": {"color": ORANGE, "index": 1},
                                                "ERROR": {"color": ROT, "index": 2}}}]},
                                            {"id": "custom.cellOptions", "value": {"type": "color-text"}}])]))
    y += 11
    ps.append(timeseries("Discord live · Mitglieder & Online",
                         [prom("max(discord_members_total)", "Mitglieder"),
                          prom("max(discord_online_total)", "Online", ref="B")],
                         pos(0, y, 12, 7), colors={"Mitglieder": VIOLETT, "Online": GRUEN},
                         desc="Zeitreihe aus fred-relay (Prometheus, 60 s)"))
    ps.append(timeseries("Nachrichten / Minute (Archiv-Zuwachs)",
                         # COUNT(*) ist kein Zähler: "Vergessen" löscht Zeilen – rate() läse das als Reset.
                         [prom("clamp_min(deriv(max(fred_archiv_nachrichten)[15m:1m]), 0) * 60", "pro Minute")],
                         pos(12, y, 12, 7), colors={"pro Minute": CYAN}))
    return ps, y + 7


# ---------------------------------------------------------------- Weltkarte
def weltkarte(y):
    ps = [row("🌍 Weltkarte · Erdbeben · ISS · Ziti-Geräte", y)]
    y += 1
    beben = infinity(
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson",
        # Infinity-Backend-Parser: Array-Index per Punkt (coordinates.1), kein JSONata.
        [("geometry.coordinates.1", "lat", "number"), ("geometry.coordinates.0", "lon", "number"),
         ("properties.mag", "Magnitude", "number"), ("properties.place", "Ort", "string"),
         ("properties.time", "Zeit", "timestamp_epoch")],
        root="features", ref="BEBEN")
    iss = infinity("https://api.wheretheiss.at/v1/satellites/25544",
                   [("latitude", "lat", "number"), ("longitude", "lon", "number"),
                    ("velocity", "km/h", "number"), ("altitude", "Höhe km", "number")],
                   ref="ISS")
    heim = infinity("", [("name", "Ort", "string"), ("lat", "lat", "number"), ("lon", "lon", "number")],
                    ref="HEIM", source="inline",
                    # bewusst nur Ortsmitte (2 Nachkommastellen) – das Repo ist öffentlich
                    data=json.dumps([{"name": "Heim · Bad Königshofen", "lat": 50.30, "lon": 10.47}]))
    ziti = prom("max by (identity, lat, lon, loc) (ziti_identity_online)", ref="ZITI", instant=True, fmt="table")

    def schicht(name, ref, symbol, farbe=None, groesse=None, feld_farbe=None, text=None):
        style = {"symbol": {"mode": "fixed", "fixed": f"img/icons/marker/{symbol}.svg"},
                 "opacity": 0.75, "rotation": {"fixed": 0, "mode": "mod", "min": -360, "max": 360},
                 "symbolAlign": {"horizontal": "center", "vertical": "center"}}
        style["size"] = groesse or {"fixed": 9, "min": 4, "max": 15}
        style["color"] = {"field": feld_farbe} if feld_farbe else {"fixed": farbe}
        if text:
            style["text"] = {"mode": "field", "field": text, "fixed": ""}
            style["textConfig"] = {"fontSize": 11, "offsetX": 0, "offsetY": -14,
                                   "textAlign": "center", "textBaseline": "middle"}
        return {"type": "markers", "name": name, "config": {"style": style, "showLegend": False},
                "location": {"mode": "coords", "latitude": "lat", "longitude": "lon"},
                "filterData": {"id": "byRefId", "options": ref}, "tooltip": True, "opacity": 1}

    karten_id = pid()
    geteilt = lambda ref: ({"datasource": DASH, "refId": "A", "panelId": karten_id, "withTransforms": False},
                           {"id": "filterByRefId", "options": {"include": ref}})
    karte = {
        "type": "geomap", "id": karten_id, "title": "Lage der Welt · live", "gridPos": pos(0, y, 16, 17),
        "targets": [beben, iss, heim, ziti],
        # Ziti liefert lat/lon als Prometheus-Labels -> erst zu Spalten, dann zu Zahlen.
        "transformations": [{"id": "labelsToFields", "options": {"mode": "columns"}},
                            {"id": "convertFieldType", "options": {"conversions": [
            {"targetField": "lat", "destinationType": "number"},
            {"targetField": "lon", "destinationType": "number"}], "fields": {}}}],
        "fieldConfig": {"defaults": {"color": {"mode": "continuous-YlRd"}, "min": 2.5, "max": 7,
                                     "custom": {"hideFrom": {"legend": False, "tooltip": False, "viz": False}}},
                        "overrides": []},
        "options": {
            "view": {"id": "coords", "lat": 30, "lon": 12, "zoom": 1.7, "allLayers": True},
            "controls": {"showZoom": True, "mouseWheelZoom": True, "showAttribution": True,
                         "showScale": False, "showMeasure": False, "showDebug": False},
            "tooltip": {"mode": "details"},
            # Carto zeigt ohne API-Key ein Wasserzeichen – ArcGIS Dark Gray ist frei nutzbar.
            "basemap": {"type": "xyz", "name": "Nacht", "config": {
                "url": "https://services.arcgisonline.com/arcgis/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
                "attribution": "Tiles © Esri — Esri, DeLorme, NAVTEQ"}},
            "layers": [
                {**schicht("Erdbeben (24 h, ≥ M2.5)", "BEBEN", "circle", feld_farbe="Magnitude",
                           groesse={"field": "Magnitude", "fixed": 6, "min": 4, "max": 30})},
                schicht("ISS", "ISS", "star", farbe=CYAN, groesse={"fixed": 18, "min": 4, "max": 30}, text="lat"),
                schicht("Heim", "HEIM", "triangle", farbe=VIOLETT, groesse={"fixed": 14, "min": 4, "max": 20}, text="Ort"),
                schicht("Ziti-Geräte", "ZITI", "square", farbe=GRUEN, groesse={"fixed": 10, "min": 4, "max": 20}),
            ]}}
    # ISS-Beschriftung: der Name ist sprechender als die Breite
    karte["options"]["layers"][1]["config"]["style"].pop("text")
    karte["options"]["layers"][1]["config"]["style"].pop("textConfig")
    ps.append(karte)
    ps.append(table("🌋 Stärkste Beben · 24 h",
                    [geteilt("BEBEN")[0]],
                    pos(16, y, 8, 11),
                    transformations=[geteilt("BEBEN")[1], {"id": "organize", "options": {"excludeByName": {"lat": True, "lon": True}}},
                                     {"id": "sortBy", "options": {"sort": [{"field": "Magnitude", "desc": True}]}},
                                     {"id": "limit", "options": {"limitField": 12}}],
                    overrides=[ov("Magnitude", [{"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "gradient"}},
                                                {"id": "color", "value": {"mode": "continuous-YlRd"}},
                                                {"id": "min", "value": 2.5}, {"id": "max", "value": 7},
                                                {"id": "decimals", "value": 1}, {"id": "custom.width", "value": 90}]),
                               ov("Zeit", [{"id": "unit", "value": "dateTimeFromNow"}, {"id": "custom.width", "value": 110}])]))
    iss_stat = stat("🛰️ ISS · Geschwindigkeit", [geteilt("ISS")[0]], pos(16, y + 11, 4, 6),
                    unit="velocitykmh", color=CYAN, graph=False, decimals=0)
    iss_stat["options"]["reduceOptions"]["fields"] = "/^km\\/h$/"
    iss_stat["transformations"] = [geteilt("ISS")[1]]
    ps.append(iss_stat)
    hoehe = stat("🛰️ ISS · Höhe", [geteilt("ISS")[0]], pos(20, y + 11, 4, 6),
                 unit="lengthkm", color=VIOLETT, graph=False, decimals=0)
    hoehe["options"]["reduceOptions"]["fields"] = "/^Höhe km$/"
    hoehe["transformations"] = [geteilt("ISS")[1]]
    ps.append(hoehe)
    return ps, y + 17


# ---------------------------------------------------------------- Kameras
KAMERAS = [("draussen", "🌳 Draußen · Garten"), ("fisheye", "🐟 Fisheye"),
           ("balkon", "🌙 Balkon"), ("drucker", "🖨️ 3D-Drucker")]


def kameras(y):
    ps = [row("📷 Kameras · Standbild jede Minute (nur mit Login sichtbar)", y)]
    y += 1
    for i, (name, titel) in enumerate(KAMERAS):
        html = (f'<div style="height:100%;display:flex;align-items:center;justify-content:center;'
                f'background:#05070d;border-radius:8px;overflow:hidden">'
                f'<a href="/kamera/{name}.jpg?t=${{__to}}" target="_blank" rel="noopener" '
                f'style="display:block;height:100%;width:100%">'
                f'<img src="/kamera/{name}.jpg?t=${{__to}}" alt="{titel}" loading="lazy" '
                f'style="width:100%;height:100%;object-fit:contain"></a></div>')
        ps.append(text(titel, html, pos(i * 6, y, 6, 11)))
    y += 11
    alter = {"type": "bargauge", "id": pid(), "title": "Bildalter je Kamera", "gridPos": pos(0, y, 24, 4),
             "targets": [prom("max by (kamera) (fred_kamera_alter_sekunden)", "{{kamera}}", instant=True)],
             "fieldConfig": {"defaults": {"unit": "s", "min": 0, "max": 600, "decimals": 0,
                                          "color": {"mode": "thresholds"},
                                          "thresholds": {"mode": "absolute", "steps": [
                                              {"color": GRUEN, "value": None}, {"color": ORANGE, "value": 150},
                                              {"color": ROT, "value": 300}]}}, "overrides": []},
             "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                         "orientation": "vertical", "displayMode": "lcd", "valueMode": "color",
                         "namePlacement": "auto", "showUnfilled": True, "sizing": "auto"}}
    ps.append(alter)
    return ps, y + 4


# ---------------------------------------------------------------- Homelab
NODE = "* on(instance) group_left(nodename) node_uname_info"


def homelab(y):
    ps = [row("🏠 Homelab · k3s · Dienste", y)]
    y += 1
    ps.append(gauge_node("CPU", f'(100 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[5m])) * 100) {NODE}',
                         pos(0, y, 8, 6)))
    ps.append(gauge_node("RAM", f"((1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100) {NODE}",
                         pos(8, y, 8, 6)))
    ps.append(gauge_node("Platte /", f'((1 - node_filesystem_avail_bytes{{mountpoint="/"}} / node_filesystem_size_bytes{{mountpoint="/"}}) * 100) {NODE}',
                         pos(16, y, 8, 6)))
    y += 6
    dienste = {"type": "state-timeline", "id": pid(), "title": "Dienste erreichbar? (TCP, 60 s)", "gridPos": pos(0, y, 16, 12),
               "targets": [prom('max by (instance) (probe_success{job="homelab-dienste"})', "{{instance}}")],
               "transformations": [{"id": "renameByRegex", "options": {"regex": "^([^.]+)\\.([^.]+)\\..*$", "renamePattern": "$1 · $2"}}],
               "fieldConfig": {"defaults": {"color": {"mode": "thresholds"},
                                            "thresholds": {"mode": "absolute", "steps": [{"color": ROT, "value": None}, {"color": GRUEN, "value": 1}]},
                                            "mappings": [{"type": "value", "options": {"0": {"text": "weg", "color": ROT},
                                                                                        "1": {"text": "da", "color": GRUEN}}}],
                                            "custom": {"fillOpacity": 80, "lineWidth": 0}}, "overrides": []},
               "options": {"showValue": "never", "mergeValues": True, "alignValue": "left", "rowHeight": 0.85,
                           "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}}
    ps.append(dienste)
    ps.append(stat("Pods laufen", [prom('sum(kube_pod_status_phase{phase="Running"})')], pos(16, y, 4, 4), color=GRUEN))
    ps.append(stat("Pods hängen", [prom('sum(kube_pod_status_phase{phase=~"Pending|Failed|Unknown"}) or vector(0)')], pos(20, y, 4, 4),
                   thresholds={"mode": "absolute", "steps": [{"color": GRUEN, "value": None}, {"color": ORANGE, "value": 1}, {"color": ROT, "value": 3}]}))
    ps.append(stat("Neustarts 24 h", [prom("sum(increase(kube_pod_container_status_restarts_total[24h]))")], pos(16, y + 4, 4, 4),
                   thresholds={"mode": "absolute", "steps": [{"color": GRUEN, "value": None}, {"color": ORANGE, "value": 3}, {"color": ROT, "value": 10}]},
                   decimals=0))
    ps.append(stat("Ziti online", [prom("max(ziti_identities_online_total)")], pos(20, y + 4, 4, 4), color=CYAN))
    ps.append(stat("Dienste down", [prom('count(probe_success{job="homelab-dienste"} == 0) or vector(0)')], pos(16, y + 8, 4, 4),
                   thresholds={"mode": "absolute", "steps": [{"color": GRUEN, "value": None}, {"color": ROT, "value": 1}]}))
    ps.append(stat("Internet", [prom('avg(probe_duration_seconds{job="internet"})')], pos(20, y + 8, 4, 4), unit="s",
                   color=VIOLETT, decimals=2))
    y += 12
    ps.append(timeseries("CPU je Knoten", [prom(f'(100 - avg by (instance) (rate(node_cpu_seconds_total{{mode="idle"}}[5m])) * 100) {NODE}', "{{nodename}}")],
                         pos(0, y, 8, 8), unit="percent"))
    ps.append(timeseries("Netz rein/raus je Knoten",
                         [prom(f'sum by (instance) (rate(node_network_receive_bytes_total{{device!~"lo|veth.*|cni.*|flannel.*"}}[5m])) {NODE}', "⬇ {{nodename}}"),
                          prom(f'-sum by (instance) (rate(node_network_transmit_bytes_total{{device!~"lo|veth.*|cni.*|flannel.*"}}[5m])) {NODE}', "⬆ {{nodename}}", ref="B")],
                         pos(8, y, 8, 8), unit="Bps", fill=10))
    ps.append(timeseries("Temperatur je Knoten", [prom(f"max by (instance) (node_hwmon_temp_celsius) {NODE}", "{{nodename}}")],
                         pos(16, y, 8, 8), unit="celsius", fill=5))
    y += 8
    ps.append(timeseries("MySQL Abfragen / s", [prom("sum(rate(mysql_global_status_queries[5m]))", "Abfragen")],
                         pos(0, y, 8, 7), colors={"Abfragen": ORANGE}))
    ps.append(timeseries("RAM je Namespace (Top 8)",
                         [prom('topk(8, sum by (namespace) (container_memory_working_set_bytes{container!="", image!=""}))', "{{namespace}}")],
                         pos(8, y, 16, 7), unit="bytes", stack=True, fill=35, legend="right"))
    return ps, y + 7


# ---------------------------------------------------------------- Jarvis
JARVIS_IFRAME = ('<iframe src="https://jarvis.benz-sw.de/jarvis" title="Jarvis" '
                 'allow="microphone; autoplay; clipboard-write" '
                 'style="width:100%;height:100%;border:0;border-radius:10px;background:#05070d"></iframe>')


def jarvis(y):
    ps = [row("🤖 Jarvis · Sissyphus", y)]
    y += 1
    reich = infinity("https://jarvis-proxy.sissyphus.svc.cluster.local/jarvis/api/reich",
                     [("label", "Bereich", "string"), ("wert", "Stand", "string"), ("zustand", "Zustand", "string")],
                     root="chips", ref="A")
    ps.append(table("Jarvis' Reich · live", [reich], pos(0, y, 8, 9),
                    overrides=[ov("Zustand", [{"id": "custom.width", "value": 90},
                                              {"id": "custom.cellOptions", "value": {"type": "color-background", "mode": "basic"}},
                                              {"id": "mappings", "value": [{"type": "value", "options": {
                                                  "ok": {"text": "● ok", "color": GRUEN, "index": 0},
                                                  "warn": {"text": "● Hinweis", "color": ORANGE, "index": 1},
                                                  "aus": {"text": "○ aus", "color": "#475569", "index": 2},
                                                  "fehler": {"text": "● Fehler", "color": ROT, "index": 3}}}]}]),
                               ov("Bereich", [{"id": "custom.width", "value": 130}])]))
    ps.append(stat("Jarvis erreichbar",
                   [prom('max(probe_success{job="homelab-dienste", instance=~"jarvis-proxy.*"}) or vector(0)', instant=True)],
                   pos(8, y, 4, 4), graph=False,
                   mappings=[{"type": "value", "options": {"1": {"text": "WACH", "color": GRUEN}, "0": {"text": "STUMM", "color": ROT}}}],
                   thresholds={"mode": "absolute", "steps": [{"color": ROT, "value": None}, {"color": GRUEN, "value": 1}]}))
    ps.append(stat("Jarvis-Browser",
                   [prom('max(kube_deployment_status_replicas_available{namespace="sissyphus", deployment="jarvis-browser"}) or vector(0)', instant=True)],
                   pos(12, y, 4, 4), graph=False,
                   mappings=[{"type": "range", "options": {"from": 1, "to": 99, "result": {"text": "BEREIT", "color": GRUEN}}},
                             {"type": "value", "options": {"0": {"text": "AUS", "color": ROT}}}],
                   thresholds={"mode": "absolute", "steps": [{"color": ROT, "value": None}, {"color": GRUEN, "value": 1}]}))
    ps.append(timeseries("Sissyphus · CPU", [prom('sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="sissyphus", container!=""}[5m]))', "{{pod}}")],
                         pos(16, y, 8, 9), unit="short", legend="hidden"))
    ps.append(timeseries("Sissyphus · RAM", [prom('sum by (pod) (container_memory_working_set_bytes{namespace="sissyphus", container!="", image!=""})', "{{pod}}")],
                         pos(8, y + 4, 8, 5), unit="bytes", legend="hidden"))
    y += 9
    # Das Gesicht selbst: WebGL-Blase mit 60k Punkten – eingeklappt, damit das Dashboard leicht bleibt.
    gesicht = text("Jarvis · Gesicht & Stimme (Mikrofon im Browser freigeben)", JARVIS_IFRAME, pos(0, y + 1, 24, 22))
    ps.append(row("🟣 Jarvis öffnen – mit ihm sprechen (Klick zum Aufklappen)", y, collapsed=True, panels=[gesicht]))
    return ps, y + 1


def dashboard():
    panels = []
    y = 0
    for teil in (kopf, discord, weltkarte, kameras, homelab, jarvis):
        ps, y = teil(y)
        panels.extend(ps)
    return {
        "uid": "fred-command-center",
        "title": "🟣 Fred · Command Center",
        "description": "Discord-Statistik, Weltkarte, Kameras, Homelab und Jarvis auf einen Blick.",
        "tags": ["fred", "discord", "homelab", "jarvis", "kameras"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "refresh": "1m",
        "time": {"from": "now-24h", "to": "now"},
        "timepicker": {"refresh_intervals": ["30s", "1m", "5m", "15m"]},
        "fiscalYearStartMonth": 0,
        "liveNow": False,
        "schemaVersion": 41,
        "version": 1,
        "links": [],
        "annotations": {"list": []},
        "templating": {"list": []},
        "panels": panels,
    }


if __name__ == "__main__":
    json.dump(dashboard(), sys.stdout, ensure_ascii=False, indent=1)
