# Fred · Command Center (Grafana)

Ein Dashboard auf **https://grafana.benz-sw.de/d/fred-command-center** für Discord-Statistik
(Server „Dialog der Religionen“, Bot Fred), Weltkarte, Kameras, Homelab und Jarvis.

| Bereich | Quelle |
|---|---|
| Kopfzeile, Discord-Statistik | MySQL `DdR` über die View `fred_archiv_oeffentlich` (Nutzer `grafana_ro`) + `fred-relay`-Metriken |
| Weltkarte | Infinity: USGS-Erdbeben (24 h, ≥ M2.5), ISS-Position; Prometheus: Ziti-Identitäten |
| Kameras | Home Assistant → `fred-relay` → `/kamera/<name>.jpg` (nur mit Grafana-Login) |
| Homelab | node-exporter, kube-state-metrics, Blackbox-Probe `homelab-dienste` |
| Jarvis | Infinity: `jarvis-proxy/jarvis/api/reich`; eingeklappte Zeile mit Jarvis als iframe |

## Aufbau

```
relay/            fred_relay.py – Exporter (/metrics) + Kamera-Relay (/kamera/…), Tests
dashboard/        gen_dashboard.py → fred-command-center.json (ConfigMap grafana-dashboard-fred)
                  check_queries.py – erzeugt je Panel eine /api/ds/query-Anfrage zum Durchtesten
k8s/              fred-relay.yaml, homelab-dienste-probe.yaml, grafana-proxy-nginx.conf
deploy.sh         Tests → ConfigMaps/Manifeste → Rollout (--neustart startet Relay + Proxy neu)
```

## Kamera-Kette

1. HA-Automation **„Grafana: Kamera-Schnappschüsse“** (`automation.grafana_kamera_schnappschusse`)
   schreibt jede Minute `camera.snapshot` nach `/config/www/<geheimer-ordner>/<name>.jpg`.
2. `fred-relay` holt die Bilder über Nabu Casa (`/local/<geheimer-ordner>/…`), cached 20 s.
3. `grafana-proxy` (nginx) gibt `/kamera/` nur frei, wenn `auth_request` gegen Grafana
   `/api/user` klappt – ohne Login 401.

Der Ordnername wirkt wie ein Passwort: wer ihn kennt, sieht die Bilder über Nabu Casa.
Er steht nur im Secret `monitoring/fred-relay-env` (`HA_KAMERA_ORDNER`) und in der HA-Automation.
**Wechseln:** neuen Namen in der Automation und im Secret setzen, alten Ordner in HA löschen.

## Datenschutz

- `grafana_ro` liest **nicht** `fred_archiv`, sondern nur die View `fred_archiv_oeffentlich`
  (`sql/fred_archiv_oeffentlich.sql`): ohne gelöschte Nachrichten, ohne Team-/Log-/Ticket-Kanäle
  und private Willkommens-Threads, Text auf 300 Zeichen gekürzt. Neuer interner Kanal → Kanal-ID
  in der View ergänzen und das SQL erneut ausführen.
- NetworkPolicy: das Relay nimmt nur Verbindungen vom Grafana-Proxy und von Prometheus an.
- Offen (außerhalb dieses Repos): In Grafana wird jeder SSO-Nutzer Admin
  (`GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH='Admin'`) und `ALLOW_INSECURE_EMAIL_LOOKUP` ist an.
  Wer sich bei Grafana anmelden kann, sieht Kameras und Archiv und darf wegen
  `disable_sanitize_html` HTML/Skripte in Panels setzen. Empfehlung: Rolle aus einer Authentik-Gruppe
  ableiten und den Zugang zur Grafana-App in Authentik auf diese Gruppe beschränken.

## Secrets (nicht im Repo)

- `monitoring/fred-relay-env`: `DISCORD_TOKEN` (Fred-Bot), `MYSQL_USER=fred_exporter`,
  `MYSQL_PASSWORD`, `HA_BASE`, `HA_KAMERA_ORDNER`
- `monitoring/grafana-ds-fred` (Label `grafana_datasource=1`): Datenquellen `fred-ddr` + `infinity`

## Änderungen außerhalb dieses Repos

- ConfigMap `kps-grafana`: Plugin `yesoreyeram-infinity-datasource`, `[panels] disable_sanitize_html = true`
  (für Kamera-`<img>` und Jarvis-`<iframe>`). **Nicht helm-fest** – ein `helm upgrade` von kps setzt beides zurück.
- MySQL `DdR`: Nutzer `grafana_ro` (View + Voice/Level/Ereignisse) und `fred_exporter` (nur SELECT), Index `fred_archiv_zeit (zeit)`, View `fred_archiv_oeffentlich`.

## Ausrollen

```bash
./deploy.sh            # Tests, Code, Manifeste, Dashboard, Proxy
# Code- oder nginx-Änderungen starten Relay/Proxy über Prüfsummen-Annotationen selbst neu;
# die nginx-Konfiguration wird vorher per `nginx -t` (Docker) geprüft.
```
