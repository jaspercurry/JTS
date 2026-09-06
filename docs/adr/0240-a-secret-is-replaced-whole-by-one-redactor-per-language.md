# ADR-0240: A secret is replaced whole, by one redactor per language

- **Date:** 2026-09-06
- **Status:** Accepted. Supersedes the redaction paragraph in the
  Consequences section of
  [ADR-0215](0215-a-broken-cloud-connection-is-announced-once-and-only-when-a-human-must-act.md)
  ("Redaction is now load-bearing…"); the rest of ADR-0215 stands.
- **Context:** ADR-0215 put redaction on a path that reaches both the journal
  and `/state`, and described it as *masking* three live provider key
  prefixes. A mask keeps a head and a tail (`sk-p…6789`), and a live key's
  head and tail are still credential material once they are published on an
  unauthenticated LAN endpoint — they also cut the search space for the
  middle. Three other Python scrubbers had meanwhile grown beside it — the
  transit URL scrub, the Wi-Fi wizard's `password <arg>` regex and the
  voice wizard's mask-by-literal — each with its own placeholder and its
  own gaps
  ([#4193](https://github.com/jaspercurry/JTS/issues/4193)).
- **Decision:** A credential-shaped span is **replaced whole** with the one
  placeholder `<redacted>` — never masked to a tail, never truncated to one.
  There is exactly one Python redactor,
  `jasper.secret_redaction.redact_secrets`, and exactly one bash redactor,
  `redact_jasper_diagnostics` in `scripts/_diagnostic_redaction.sh` (it exists
  because the support bundle has to redact when the Python venv is the broken
  thing being diagnosed). Both are pattern-based, so no caller needs the
  secret in hand, and both are pinned by one case table —
  `CASES` in `tests/test_secret_redaction.py`, whose rows carry the flag
  saying which shapes sit inside the bash redactor's mandate. A caller that
  does hold the literal secret runs a value pass **in addition to** the
  redactor, never instead of it: a key in an unrecognised shape is removable
  only by its literal.
- **Consequences:** One vocabulary on every surface, so a shape learned from
  an incident is added once and both languages get it, and a rule the two
  languages disagree about cannot merge. Over-redaction is the accepted
  failure direction; where a rule eats prose, the table records it as a
  positive row rather than narrowing until a real secret slips through.
  Given up: recognising a masked key in a log — a wizard still renders a
  masked key beside the field that owns it, which is where that affordance
  belongs. Rejected: a shorter mask tail, which buys the same
  convenience at the same kind of exposure; and per-subsystem redactors,
  which is what ADR-0215's "widen the patterns, do not add a second redactor"
  already ruled out.
