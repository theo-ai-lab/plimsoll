# Plimsoll MCP governor demo tape for charmbracelet/vhs.
# The committed demo/mcp-governor.gif is rendered from this tape; regenerate it after
# changing the scripted session with:  vhs demo/mcp-governor.tape
# The active environment needs the optional MCP extra: pip install "plimsoll[mcp]"

Output demo/mcp-governor.gif

Set Shell "bash"
Set FontSize 18
Set Width 1200
Set Height 480
Set Theme "Dracula"
Set Padding 18
Set TypingSpeed 50ms

# Drive the real plimsoll-governor stdio MCP server through the scripted deterministic
# session: one allow, one deny (tool_order), one budget-exceeded (max_tokens).
Type "python scripts/build_mcp_governor_session.py --out /tmp/mcp-governor-transcript.jsonl"
Enter
Sleep 6s
