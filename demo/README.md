# Demo

`plimsoll.tape` records a short terminal demo of Plimsoll gating two agent traces against the same policy and baseline: a clean trace passes with exit code 0, then a regressed trace fails with exit code 1 and a breakdown of findings by severity. The committed `demo.gif` is rendered from the `.tape` script with [charmbracelet/vhs](https://github.com/charmbracelet/vhs); regenerate it with `vhs demo/plimsoll.tape`. The recording is reproducible from the checked-in example fixtures.
