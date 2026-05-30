Tail context-engine container logs to debug errors or slow requests.

Steps:
1. Run: docker compose logs context-engine --tail 50
2. Look for: ERROR lines, slow request warnings (>5000ms), model timeout messages
3. Report any errors found. If a model timeout appears, note which endpoint triggered it.
