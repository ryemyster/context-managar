Run the context-engine scout for a task before implementing it.

Usage: /context <task description>
Example: /context add stripe plan enforcement to check-in generation

Steps:
1. Run the context script:
   bash ~/Repos/ryemyster/local-model/scripts/context.sh "$ARGUMENTS" "src/app,src/lib,supabase" ""
2. Read the output: ./ai-context/context-bundle.md
3. Report: files found, risks flagged, vector hits, suggested files to verify
4. Ask the user what to implement next
