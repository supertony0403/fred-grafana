#!/usr/bin/env bash
# Rollt Fred-Command-Center aus: Relay-Code, Relay-Manifest, Dienst-Proben, Proxy, Dashboard.
# Secrets (fred-relay-env, grafana-ds-fred) verwaltet dieses Skript NICHT – siehe README.
# Code- und nginx-Aenderungen loesen ueber Pruefsummen-Annotationen selbst einen Rollout aus.
set -euo pipefail
cd "$(dirname "$0")"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/new-cluster.yaml}"
NS=monitoring

( cd relay && python3 -m unittest -q )

# nginx-Konfiguration vor dem Einspielen pruefen (Grafana-Host lokal nur als Attrappe).
docker run --rm --add-host kps-grafana.monitoring.svc.cluster.local:127.0.0.1 \
  -v "$PWD/k8s/grafana-proxy-nginx.conf:/etc/nginx/nginx.conf:ro" nginx:alpine sh -c \
  'mkdir -p /certs && apk add -q openssl >/dev/null 2>&1 && openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=t -keyout /certs/tls.key -out /certs/tls.crt -days 1 2>/dev/null && nginx -t -q'

kubectl -n $NS create configmap fred-relay-code --from-file=fred_relay.py=relay/fred_relay.py \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/fred-relay.yaml
kubectl apply -f k8s/homelab-dienste-probe.yaml

python3 dashboard/gen_dashboard.py > dashboard/fred-command-center.json
kubectl -n $NS create configmap grafana-dashboard-fred \
  --from-file=fred-command-center.json=dashboard/fred-command-center.json \
  --dry-run=client -o yaml | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply -f -

# kamera.benz-sw.de (Secret kamera-web-htpasswd verwaltet dieses Skript nicht, siehe README)
kubectl -n $NS get secret kamera-web-htpasswd >/dev/null || {
  echo "Secret kamera-web-htpasswd fehlt – siehe README." >&2; exit 1; }
docker run --rm \
  -v "$PWD/kamera-web/nginx.conf:/etc/nginx/nginx.conf:ro" nginx:1.27-alpine sh -c \
  'mkdir -p /certs /auth && touch /auth/htpasswd && apk add -q openssl >/dev/null 2>&1 && openssl req -x509 -newkey rsa:2048 -nodes -subj /CN=t -keyout /certs/tls.key -out /certs/tls.crt -days 1 2>/dev/null && nginx -t -q'
kubectl -n $NS create configmap kamera-web-conf \
  --from-file=nginx.conf=kamera-web/nginx.conf --from-file=index.html=kamera-web/index.html \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/kamera-web.yaml

kubectl -n $NS create configmap grafana-proxy-conf --from-file=nginx.conf=k8s/grafana-proxy-nginx.conf \
  --dry-run=client -o yaml | kubectl apply -f -

summe() { sha256sum "$1" | cut -c1-16; }
kubectl -n $NS patch deploy/fred-relay --type merge -p \
  "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"fred/code-sha\":\"$(summe relay/fred_relay.py)\"}}}}}"
kubectl -n $NS patch deploy/grafana-proxy --type merge -p \
  "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"fred/nginx-sha\":\"$(summe k8s/grafana-proxy-nginx.conf)\"}}}}}"

kubectl -n $NS patch deploy/kamera-web --type merge -p \
  "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"fred/kamera-sha\":\"$(cat kamera-web/nginx.conf kamera-web/index.html | sha256sum | cut -c1-16)\"}}}}}"

kubectl -n $NS rollout status deploy/fred-relay --timeout=180s
kubectl -n $NS rollout status deploy/kamera-web --timeout=180s
kubectl -n $NS rollout status deploy/grafana-proxy --timeout=180s
echo "fertig: https://grafana.benz-sw.de/d/fred-command-center"
