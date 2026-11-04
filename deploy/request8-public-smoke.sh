#!/usr/bin/env bash
set -euo pipefail
[ "$#" -eq 1 ] || exit 2
python3 - "$1" <<'PYSMOKE'
import hashlib,json,re,sys,time,urllib.error,urllib.request
sha=sys.argv[1]
if not re.fullmatch(r"[0-9a-f]{40}",sha): raise SystemExit(2)
base="https://app.grove.x-arc.ai"
deadline=time.monotonic()+105
checks=[]
def get(path,expected,cap=2097152):
    remaining=deadline-time.monotonic()
    if remaining<=0: raise RuntimeError("deadline")
    req=urllib.request.Request(base+path,headers={"User-Agent":"grove-request8-smoke"})
    try: response=urllib.request.urlopen(req,timeout=min(10,remaining))
    except urllib.error.HTTPError as exc: response=exc
    with response:
        body=response.read(cap+1)
        if response.status!=expected or len(body)>cap: raise RuntimeError("response")
        checks.append({"path":path,"status":response.status})
        return dict((k.lower(),v) for k,v in response.headers.items()),body
try:
    headers,body=get("/healthz",200,4096)
    health=json.loads(body)
    if health!={"status":"ok","store":"ok","build":sha}: raise RuntimeError("health")
    assets=set()
    for path in ("/login","/signup","/signin","/logout"):
        headers,body=get(path,200)
        if headers.get("cache-control")!="no-store" or not headers.get("content-type","").startswith("text/html"): raise RuntimeError("page")
        assets.update(re.findall(rb"/assets/grove/([a-f0-9]{64}\.(?:js|css|woff2?))",body))
    if not assets: raise RuntimeError("assets_absent")
    for name in sorted(assets):
        name=name.decode("ascii")
        headers,body=get("/assets/grove/"+name,200,4194304)
        if hashlib.sha256(body).hexdigest()!=name.split(".")[0]: raise RuntimeError("asset_hash")
        if headers.get("cache-control")!="public, max-age=31536000, immutable": raise RuntimeError("asset_cache")
    for path,status in (("/v1/views/session",401),("/v1/views/pipedream/accounts",401),("/assets/grove/manifest.json",404),("/assets/grove/",404)):
        get(path,status,65536)
    print(json.dumps({"state":"pass","target":sha,"checks":checks,"asset_count":len(assets)},sort_keys=True))
except Exception:
    print(json.dumps({"state":"fail","target":sha,"completed_checks":checks},sort_keys=True))
    raise SystemExit(1)
PYSMOKE
