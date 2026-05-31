# Demo

`plimsoll.tape` records a short terminal demo of Plimsoll gating two agent traces against the same policy and baseline: a clean trace passes with exit code 0, then a regressed trace fails with exit code 1 and a breakdown of findings by severity. The `.tape` script is the committed source of truth; generate the GIF locally with `vhs demo/plimsoll.tape` (requires [charmbracelet/vhs](https://github.com/charmbracelet/vhs)). The recording is reproducible from the checked-in example fixtures; the rendered GIF is produced on demand and is not committed.
