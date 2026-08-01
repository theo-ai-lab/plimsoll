# Plimsoll MCP governor demo tape for charmbracelet/vhs.
# The committed demo/mcp-governor.gif is rendered from this tape; regenerate it after
# changing the scripted session with:  vhs demo/mcp-governor.tape
# The active environment needs the optional MCP extra (from a clone):
#   python -m pip install -e '.[mcp]'

Output demo/mcp-governor.gif

Set Shell "bash"
Set FontSize 16
Set Width 1200
Set Height 540
Set Theme "Dracula"
Set Padding 18
Set TypingSpeed 50ms

# Drive the real plimsoll-governor stdio MCP server through the scripted deterministic
# session: the server opens the gate session and keeps the history, two allows, a deny
# (tool_order) of the goal action, that same call retried with a forged history and refused
# (session_history_mismatch), the real approvals, and a budget block (max_tokens).
Type "python scripts/build_mcp_governor_session.py --out /tmp/mcp-governor-transcript.jsonl"
Enter
Sleep 8s
