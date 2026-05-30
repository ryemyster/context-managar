Check context-engine health and all three model statuses.

Steps:
1. Run: curl -s http://localhost:8088/health | python3 -m json.tool
2. Report:
   - code_model + code_model_available
   - reason_model + reason_model_available
   - embed_model + embed_model_available
   - vector_ready
   - overall status (ok / degraded / critical)
3. If any model is unavailable, suggest: ollama pull <model-name>
