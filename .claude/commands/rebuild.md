Rebuild and restart the context-engine container. Run this after editing any Python file.

Steps:
1. Run: docker compose up --build -d
2. Wait 5s, then check: curl -s http://localhost:8088/health | python3 -m json.tool
3. Report: code_model, reason_model, embed_model availability
4. If status is not "ok", run: docker compose logs context-engine --tail 30 and report errors
