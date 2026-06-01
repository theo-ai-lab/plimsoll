# Plimsoll demo tape for charmbracelet/vhs.
# The committed demo/demo.gif is rendered from this tape; regenerate it after
# changing the demo with:  vhs demo/plimsoll.tape

Output demo/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1200
Set Height 700
Set Theme "Dracula"
Set Padding 18
Set TypingSpeed 50ms

# Show the version.
Type "python -m plimsoll --version"
Enter
Sleep 1.5s

# 1. Clean trace passes the policy gate (exit 0).
Type "python -m plimsoll run --input examples/traces/current_ticket_triage.json --baseline examples/traces/baseline_ticket_triage.json --policy examples/policies/default_policy.json --out /tmp/clean"
Enter
Sleep 2.5s

Type "clear"
Enter
Sleep 500ms

# 2. A regression the gate catches (exit 1, findings reported).
Type "python -m plimsoll run --input examples/traces/regressed_ticket_triage.json --baseline examples/traces/baseline_ticket_triage.json --policy examples/policies/default_policy.json --out /tmp/regression"
Enter
Sleep 3s
