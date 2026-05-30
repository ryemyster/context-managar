Run the context-engine scout for a task in this repo before implementing it.

Usage: /context <task description>
Example: /context add a new /analyze endpoint using the reasoning model

Steps:
1. Call the context engine:
   curl -s -X POST http://localhost:8088/context \
     -H "Content-Type: application/json" \
     -d "{\"task\": \"$ARGUMENTS\", \"paths\": [\"ryemyster/local-model/context-engine/app\"], \"focus\": [\"endpoints\", \"models\", \"ollama\"]}"
2. Read the output file: ./ai-context/context-bundle.md
3. Report: files found, risks flagged, suggested files to verify
