# ADR-0262: The conf-parity exception set lives in the test

- **Date:** 2026-09-09
- **Status:** Accepted. Supersedes (partial)
  [ADR-0253](0253-web-ia-manifest-and-url-policy.md) §7 — its exception list.
- **Context:** §7's conf-parity guard shipped, and the set it ships with is
  not §7's list: `/assistant/wake/` was missing from that list, and the
  `/source` vs `/source/` mismatch was fixed rather than allowlisted.
- **Decision:** `_CONF_LOCATION_DIFF_ALLOWLIST` in
  `tests/test_landing_page_html.py` owns the set — `/assistant/wake/`,
  `/mic`, `/wake-corpus/`, `/wake/`, all wake-stack routes on `:80`.
- **Consequences:** One place says what the two confs may differ by, and it
  fails the day that drifts. §7's removal condition stands: the entries go
  the day streambox gains wake support.
