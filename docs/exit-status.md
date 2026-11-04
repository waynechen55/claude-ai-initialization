# Exit status

`0` success, `1` a phase failed or the configuration was rejected, `2` argparse
refused the command line (or a partly successful run when
`run.partial_is_failure = true`), `130` interrupted. `--json` puts the
machine-readable report on stdout and nothing else. Partial success is not a
failure unless you set `run.partial_is_failure = true`.

The verdict printed at the end of each phase is derived from its steps, not
declared separately:

- the worst step status wins — `failed` beats `partial` beats `skipped` beats
  `changed` beats `ok`;
- a phase where something failed **and** something worked is `PARTIALLY PASSED`,
  never `FAILED`, because the distinction is what the exit-code policy acts on;
- one skipped step among completed ones does not make the phase `SKIPPED` —
  that reading would hide the work it actually did.

A phase that
[works through a list](usage.md#a-phase-that-works-through-a-list-is-counted-on-the-list)
applies those same three rules to its *subjects* rather than to its steps — the
steps are how it looked, the subjects are what it found. Its steps still count
for one thing: a run-level failure makes the phase worse, and a run-level change
promotes an otherwise clean pass to `PASSED WITH CHANGES`.

The run's overall result is derived from its phases by those same three rules —
so one idle phase among three that did the work does not report the run as
`SKIPPED`, and one failed phase among several that succeeded reports
`PARTIALLY PASSED`.

There is one exception, at both levels. A phase that stopped because a required
value was nowhere to be found is `FAILED` however many of its steps passed
first, and it makes the whole run `FAILED` and the exit status `1` however well
the other phases went — see
[When there is nothing to use, the run stops](configuration.md#when-there-is-nothing-to-use-the-run-stops).
It is marked `"fatal": true` in the JSON report.

The safety assertion is *not* derived from the status; each phase computes it
from what it knows about its own dependants (see
[Is it safe to go ahead?](usage.md#is-it-safe-to-go-ahead)), and the run takes the least
safe of them.
