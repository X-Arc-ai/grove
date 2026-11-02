#!/usr/bin/env bash
# The checks after a deploy, from the box or from any machine: smoke.sh https://<host> [<sha>]
#
# The health probe answers with the store up and, given a sha, with that build. The
# protected-resource metadata names <base>/mcp at both paths, also for Meta's fetcher, and an
# unauthenticated /mcp call gets 401 with the metadata URL.
set -euo pipefail

[ $# -ge 1 ] || {
    echo "usage: smoke.sh https://<host> [<sha>]" >&2
    exit 2
}
base=${1%/}
sha=${2:-}
tries=${GROVE_SMOKE_TRIES:-30}
fetcher_agent="meta-webindexer/1.1"
work=$(mktemp -d)
trap 'rm -rf "${work:?}"' EXIT

# fetch <method> <url> <user agent>: prints the status; body and headers land in $work.
fetch() {
    curl --silent --show-error --max-time 10 --request "$1" --user-agent "$3" \
        --output "$work/body" --dump-header "$work/headers" --write-out '%{http_code}' "$2"
}

# check_json <python expression> [values...]: the body as JSON in d, the values in a. The values
# travel as arguments, so a quote in one cannot change the expression.
check_json() {
    local expression=$1
    shift
    python3 -c 'import json, sys; d = json.load(open(sys.argv[1])); a = sys.argv[3:]; sys.exit(0 if eval(sys.argv[2]) else 1)' \
        "$work/body" "$expression" "$@" 2>/dev/null
}

health_ok=0
for _ in $(seq "$tries"); do
    status=$(fetch GET "$base/healthz" grove-smoke || true)
    if [ "$status" = 200 ] && check_json "d['status'] == 'ok' and d['store'] == 'ok'"; then
        if [ -z "$sha" ] || check_json "d['build'] == a[0]" "$sha"; then
            health_ok=1
            break
        fi
    fi
    sleep "${GROVE_POLL_SECONDS:-2}"
done
[ "$health_ok" = 1 ] || {
    echo "smoke: $base/healthz did not answer ok${sha:+ for build $sha} (last status ${status:-none})" >&2
    exit 1
}
echo "ok healthz${sha:+ build=$sha}"

for path in /.well-known/oauth-protected-resource /.well-known/oauth-protected-resource/mcp; do
    for agent in grove-smoke "$fetcher_agent"; do
        status=$(fetch GET "$base$path" "$agent")
        [ "$status" = 200 ] || {
            echo "smoke: $path answered $status to $agent" >&2
            exit 1
        }
        check_json "d['resource'] == a[0] + '/mcp' and d['authorization_servers'] == [a[0]]" "$base" || {
            echo "smoke: $path does not name $base/mcp" >&2
            exit 1
        }
    done
    echo "ok $path"
done

status=$(fetch POST "$base/mcp" grove-smoke)
[ "$status" = 401 ] || {
    echo "smoke: an unauthenticated /mcp call answered $status, not 401" >&2
    exit 1
}
grep -qi "^www-authenticate: Bearer resource_metadata=\"$base/.well-known/oauth-protected-resource\"" \
    "$work/headers" || {
    echo "smoke: the 401 from /mcp has no resource_metadata for $base" >&2
    exit 1
}
echo "ok /mcp 401"
