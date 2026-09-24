"""Erzeugt aus dem Dashboard-JSON je Panel-Abfrage einen /api/ds/query-Body (JSON auf stdout)."""
import json, sys
d = json.load(open(sys.argv[1]))
def alle(ps):
    for p in ps:
        yield p
        yield from alle(p.get("panels", []))
out = []
for p in alle(d["panels"]):
    for t in p.get("targets", []):
        q = dict(t); q.setdefault("intervalMs", 60000); q.setdefault("maxDataPoints", 500)
        out.append({"titel": p.get("title", ""), "ref": t["refId"],
                    "body": {"queries": [q], "from": "now-24h", "to": "now"}})
json.dump(out, sys.stdout)
