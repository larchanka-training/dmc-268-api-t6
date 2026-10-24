# Time

## One clock

Code in `app/` reads the current time only through `app.common.clock.utcnow()`.
Tests freeze that function, so a direct `datetime.now()` call, with or without a
timezone, leaves the expiry logic it feeds untested against the frozen clock.
Report such a call as a correctness problem.

# Security

SQL that includes user input must be parameterized.
