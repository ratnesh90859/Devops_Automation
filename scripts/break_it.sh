#!/bin/bash
# Usage: ./scripts/break_it.sh <type>
# type: oom | timeout

URL=${CLOUD_RUN_SERVICE_URL:-"http://localhost:8080"}
TYPE=$1

case $TYPE in
  oom)
    echo "Sending 5 parallel /heavy requests to spike memory..."
    # cost-optimised: 5 parallel (down from 15) — still triggers OOM alert
    for i in {1..5}; do curl -s "$URL/heavy" & done
    wait
    ;;
  timeout)
    echo "Sending /slow requests to trigger latency alert..."
    # cost-optimised: 2 parallel (down from 3) — still triggers latency alert
    for i in {1..2}; do curl -s "$URL/slow" & done
    wait
    ;;
  *)
    echo "Usage: ./break_it.sh oom|timeout"
    exit 1
    ;;
esac

echo ""
echo "Done. Watch Grafana at http://localhost:3000"
echo "Alert fires in ~1-2 minutes."
echo ""
echo "Or trigger agent manually now:"
echo "curl -X POST http://localhost:8000/webhook \\"
echo "  -H 'X-Token: your-secret' \\"
echo "  -H 'Content-Type: application/json' \\"
echo "  -d '{\"source\":\"manual\",\"service_url\":\"$URL\"}'"
