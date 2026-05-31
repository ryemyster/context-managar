Restart the context-engine service and confirm health. Run this after editing any Python file.

Steps:
1. Run: launchctl unload ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
2. Run: launchctl load ~/Library/LaunchAgents/life.ascendvent.context-manager.plist
3. Wait 3s, then check: curl -s http://localhost:8088/health | python3 -m json.tool
4. Report: code_model, reason_model, embed_model availability
5. If status is not "ok", check logs: tail -30 ~/Library/Logs/context-manager.log and report errors
