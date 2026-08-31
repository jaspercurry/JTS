# Deep research 5 — documentation architecture for LLM agents driving a CLI toolbox

> Owner-run deep research, received 2026-08-31, answering the fifth Wave 6
> research assignment (see `docs/tuning-master-plan.md`, ticket 6.9; the
> assignment itself was issued after the first four). Banked verbatim below
> the rule; the adjudications live in `00-adjudications.md` and ADR-0204.
> Frozen: no further edits.

---

Documentation Architecture for an LLM Agent Driving a CLI Toolbox over SSH

TL;DR

* Adopt a three-tier, progressive-disclosure architecture, not a monolith: a single always-loaded entry document (~150–200 lines: authority model, hard safety rules, and a pointer map), a mid-tier methodology/manual set the agent loads on demand, and per-tool contracts that live primarily in each CLI's own `--help` surface. This is backed by measured evidence that instruction-following and retrieval degrade as context grows (Chroma "Context Rot," NoLiMa, RULER, "curse of instructions") and by vendor guidance from Anthropic on context engineering and progressive disclosure.
* Put per-tool detail in the tool, not (only) in a manual. The strongest measured result in this space is that interface and error-message design change agent success rates: SWE-agent's agent-computer interface scored 18.00% vs 11.00% for a bare shell on SWE-bench Lite (+10.7 points, a 64% relative gain), and Anthropic reports state-of-the-art SWE-bench gains from refining tool descriptions. A self-describing `--help` surface plus teaching error messages is higher-leverage than prose docs.
* Assume the agent will skip pointers, drift, and be injectable. Referenced files are NOT reliably read (Claude Code's own `@import` loads eagerly, and free-text "read X" pointers are frequently ignored); duplicated docs drift; and any tool output the agent reads is an untrusted injection surface. Design for cold-start discoverability (an `orient`/`start-here` entrypoint) and a strict data/instruction boundary.

Key Findings

1. Context files (CLAUDE.md / AGENTS.md / GEMINI.md / Cursor / Copilot): strong convergence, thin measurement. Every major vendor now reads a project-context file: Claude Code reads `CLAUDE.md`; OpenAI Codex, Cursor, and others read `AGENTS.md` (a jointly-backed open convention from Google, OpenAI, Factory, Sourcegraph and Cursor); Gemini CLI reads `GEMINI.md`; GitHub Copilot reads `.github/copilot-instructions.md`; Cursor uses `.cursor/rules/*.mdc`. The convergent conventions are: keep it short, imperative, universally-applicable, and link out to detail rather than pasting it. Recommended size caps cluster around ~200–300 lines (CLAUDE.md) and ~2,000 tokens (Copilot), but these numbers are vendor guidance and community convention, not measured — no vendor has published an A/B adherence curve tied to file length. The one quasi-measured claim is HumanLayer's proxy-logging analysis showing Claude Code's own system prompt contains ~50 instructions and that Claude "will ignore the contents of your CLAUDE.md if it decides that it is not relevant to its current task." (label: vendor guidance + community habit; the adherence-vs-length curve is folklore)

2. Instruction-following and retrieval degrade measurably as context grows.

* Lost in the Middle (Liu, Lin, Hewitt, Paranjape, Bevilacqua, Petroni, Liang; TACL 2024, arXiv 2307.03172, 2023): a U-shaped curve — "performance is highest when relevant information occurs at the very start or end of the context, and performance degrades when it must use information in the middle." Replicated across GPT-3.5-Turbo, GPT-4, Claude 1.3, LongChat-13B, MPT-30B and Cohere Command, with roughly a 20-point drop (~75%→~55%) on 20-document multi-document QA from position alone. (consensus / measured)
* Chroma "Context Rot" (Kelly Hong, Anton Troynikov, Jeff Huber; July 14, 2025): 18 models across Anthropic (5), OpenAI (7), Google (3) and Alibaba (3) families all degrade as input length grows even on trivial retrieval/replication tasks, holding task complexity constant. Even a single distractor "reduces performance relative to the baseline," lower needle-question semantic similarity degrades faster, and — counter to intuition — shuffled haystacks beat logically-structured ones. On LongMemEval, 306 prompts averaging ~113k tokens were compared to focused versions averaging ~300 tokens: "Across all models, we see significantly higher performance on focused prompts compared to full prompts." The report frames degradation as "non-uniform" and progressive, never as a cliff. (single-source but rigorous / measured)
* RULER (NVIDIA, arXiv 2404.06654): despite near-perfect vanilla needle-in-a-haystack scores, "only half of them can maintain satisfactory performance at the length of 32K"; effective context sits well below advertised length (widely summarized as ~50% of the claimed window). (consensus / measured)
* NoLiMa (Modarressi et al., ICML 2025, arXiv 2502.05167): with lexical overlap removed, "at 32K... 10 models drop below 50% of their strong short-length baselines. Even GPT-4o... experiences a reduction from an almost-perfect baseline of 99.3% to 69.7%." (single-source / measured)
* Curse of instructions / ManyIFEval / ScaledIF / IFEval-derivatives: the success rate of following all instructions is "precisely explained by the success rate of individual instructions to the power of total number of instructions"; a 2026 study finds reliable compositional constraint satisfaction breaks down beyond ~5–6 simultaneous constraints across 15 models. (consensus across several 2024–2026 papers / measured) Together these justify a small always-loaded core and just-in-time loading of everything else.

3. Anthropic's context-engineering guidance explicitly endorses progressive/just-in-time disclosure — but "pointers" are not reliably followed by default. Anthropic's "Effective context engineering for AI agents" (Sep 29, 2025; Applied AI team — Prithvi Rajasekaran, Ethan Dixon, Carly Ryan, Jeremy Hadfield) states: "Given that LLMs are constrained by a finite attention budget, good context engineering means finding the smallest possible set of high-signal tokens that maximize the likelihood of some desired outcome." It recommends just-in-time retrieval where agents hold lightweight identifiers (file paths, queries) and load detail at runtime via tools — Claude Code's `glob`/`grep` + `CLAUDE.md` hybrid — and cites the Chroma context-rot study directly as the motivation. (vendor guidance, grounded in the measured context-rot literature it cites) Crucially, the mechanism matters: in Claude Code, `@import` references load eagerly at launch (a developer who split a 2,100-line file into six imports measured "the same tokens as a monolithic file, providing organizational benefits only"), while free-text instructions to "read file X" are frequently ignored. True lazy loading comes from subdirectory files loaded on-demand, Skills (frontmatter blurb always loaded, body on trigger), or slash commands. (label: vendor-documented mechanism + community-measured token evidence)

4. Per-tool contracts belong in the tool's self-describing surface, and interface/error design is the best-measured lever in the whole field.

* SWE-agent ACI (Yang, Prabhakar et al., NeurIPS 2024, arXiv 2405.15793): a purpose-built agent-computer interface scored 18.00% vs 11.00% for a bare Linux shell on SWE-bench Lite — "SWE-agent solves 10.7 percentage points more issues than the baseline agent that uses just the default Linux shell" (a 64% relative gain). A linting-gated `edit` command added +3.0 points; a 100-line file-viewer window was optimal; informative error messages and concise, consistent output formats drove recovery. 51.7% of successful runs still had ≥1 failed edit — recovery depends on good feedback; recovery probability fell from 90.5% (zero prior failed edits) to 57.2% (after one). (single-source / measured, and highly relevant to a CLI toolbox)
* Anthropic "Writing effective tools for agents" (Sep 11, 2025): refining tool descriptions produced state-of-the-art SWE-bench Verified results. Principles: consolidate rather than wrap many thin API endpoints; namespace tools (prefix vs suffix had "non-trivial effects"); return high-signal natural-language fields over cryptic UUIDs (resolving UUIDs to meaningful names "significantly improves Claude's precision"); expose a `response_format` concise/detailed enum; cap responses (25,000 tokens by default in Claude Code); and write error responses that "clearly communicate specific and actionable improvements, rather than opaque error codes or tracebacks." (vendor guidance, partly measured on internal Slack/Asana evals)
* BFCL (Berkeley Function-Calling Leaderboard, Patil et al., ICML 2025): function docs (names, parameter descriptions) are the evaluation surface; models "excel at single-turn calls" but "memory, dynamic decision-making, and long-horizon reasoning remain open challenges" — implying tool contracts must be self-contained and unambiguous. (consensus / measured, though not an isolated doc-quality ablation)

5. The Skills/commands pattern: strong vendor convergence; early independent evidence is sobering. Anthropic Agent Skills ("Equipping agents for the real world," Oct 16, 2025) define a `SKILL.md` with YAML frontmatter (name + description loaded into the system prompt at startup) and a body plus bundled files/scripts loaded on demand — three-level progressive disclosure, so "the amount of context that can be bundled into a skill is effectively unbounded." Community guidance: keep `SKILL.md` under ~500 lines / ~5k tokens; frontmatter ~50–100 tokens. When to use a Skill vs a doc: a Skill fits a packaged, reusable capability with scripts and lazy-loaded reference files and reliable trigger-based loading; a plain doc fits when the agent reads it in one predictable step. But independent evaluation is thin and cautionary: the preprint SWE-Skills-Bench (arXiv 2603.15401, 2026; explicitly labeled "pre-print with preliminary results, work in progress"), testing 49 public skills over ~565 tasks with Claude Code + Claude Haiku 4.5, found "39 of 49 skills yield zero pass-rate improvement, and the average gain is only +1.2%" (89.8%→91.0%), "only seven specialized skills produce meaningful gains (up to +30%), while three degrade performance (up to −10%)," and token overhead ran from −78% to +451% while pass rates stayed flat. (label: vendor convention is consensus; efficacy evidence is single-source / preliminary and mixed)

6. Cold-start discoverability: agents explore, waste turns, and skip things — design an explicit entrypoint. Terminal-Bench (arXiv 2601.11868) found the most common single failure was "'Command not found' (24.1% of all failures), typically occurring when agents attempted to use executables not installed or not in the system PATH" — underscoring the value of tool discovery. tbench's "Terminal-Bench Challenges" reports agents show a "lack of exploration in solution space" and overuse full test suites. TerminalWorld (arXiv 2605.22535) found an "efficiency paradox" — agents "spending extra compute exploring authentic environments without making progress" — and that Terminal-Bench scores only weakly predict real-world terminal performance (Pearson r=0.20). Across these, the underlying model dominates scaffolding (upgrading the model improved Codex CLI resolution ~52%). Implication: provide a printed "start here" orientation command and pointers in CLI output, because agents will otherwise burn turns probing. (consensus / measured across terminal benchmarks)

7. Documented failure modes to design against.

* Guidance ignored: long/irrelevant context files get skipped (HumanLayer proxy analysis; CLAUDE.md community consensus). (vendor + community-measured)
* Duplicated docs drift: "each rule in exactly one place" is the repeated community prescription; duplication wastes the finite attention budget. (community habit, consistent with measured attention-budget findings)
* Reference mistaken for instruction / over-specification: SWE-agent found 52.0% of unresolved cases were "Incorrect Implementation or Overly Specific Implementation." (single-source / measured)
* Prompt injection: indirect prompt injection — malicious instructions embedded in tool output, files, emails, or fetched content the agent reads — is a documented, in-the-wild threat (Greshake et al. "NotWhatYouSignedUpFor"; InjecAgent; AgentDojo; Zscaler ThreatLabz and Forcepoint X-Labs field reports, 2026, documenting live payloads that steered agents toward crypto-payment scams). Any toolbox output the agent ingests is an attack surface. (consensus / measured + real-world observed)
* Instruction conflict across layers: the built-in system prompt silently wins over user docs when they conflict, and layered files can contradict each other. (vendor-documented + community)

Details

The measured vs vendor vs folklore ledger

* Measured (controlled experiment/benchmark): Lost-in-the-Middle U-curve; Chroma context rot (18 models); RULER effective-length; NoLiMa (GPT-4o 99.3%→69.7% at 32K); curse-of-instructions/ManyIFEval; SWE-agent ACI ablations (18% vs 11%; edit-linting +3.0 pts); Terminal-Bench failure taxonomy (24.1% "command not found"); SWE-Skills-Bench (+1.2% avg, preliminary).
* Vendor guidance (stated, limited/no independent measurement): Anthropic context-engineering and tool-writing blogs; Agent Skills design and progressive disclosure; CLAUDE.md/AGENTS.md/GEMINI.md/Copilot size and structure recommendations; "IMPORTANT/YOU MUST" emphasis improving adherence.
* Folklore/community convention (no eval behind it): the specific "~200 lines" / "~2,000 token" CLAUDE.md ceilings; "keep SKILL.md under 500 lines"; the "60–70% context = dumb zone" figure (practitioner telemetry, not a controlled study); prefix-vs-suffix namespacing being universally better; free-text "read this file" pointers being reliably followed.

Recommended architecture for the 19-tool Pi toolbox

Map the four existing documents onto loading tiers, because the tier — not the topic — decides what the agent actually attends to.

Tier 0 — The entrypoint command (new). Add one CLI verb, e.g. `toolbox orient` (and/or have the SSH login MOTD print a one-liner pointing to it), that prints on a single screen: the authority model, the hard safety rules, the tool menu with one-line purposes and authority tiers, and explicit next-step pointers ("run `toolbox <tool> --help` before first use; read methodology via `toolbox doc methodology`"). This directly addresses the cold-start and "command not found" failure modes. (inference from Terminal-Bench + Anthropic just-in-time guidance)

Tier 1 — Always-loaded constitution (keep; trim toward ~150–200 lines). The authority model + hard safety rules belong here, resident in context every session. Keep it imperative, deduplicated, and use sparing emphasis ("NEVER … — instead do …"). Move everything that is reference rather than always-true instruction down a tier. (vendor guidance + measured instruction-dilution evidence)

Tier 2 — Methodology + operator manual, loaded on demand. The ~600-line methodology guide and ~1,100-line runbook should NOT be pasted into always-loaded context. Expose them behind explicit retrieval (a `toolbox doc <name>` command, or as Skills whose frontmatter advertises "order of operations," "debugging exit codes," etc.). Because `@import` loads eagerly, do not wire these in via `@import`; use on-demand commands/Skills or subdirectory placement so they cost tokens only when relevant. (vendor-documented mechanism + measured context-rot rationale)

Tier 3 — Per-tool contracts in `--help` (primary) + a thin generated index. Make each of the 19 CLIs fully self-describing: purpose, when to use / when NOT to use, parameters with unambiguous descriptions, one worked example, exit codes, and — most important — error messages that state the specific corrective action. This is where the best-measured gains live (SWE-agent ACI; Anthropic tool-description refinement). The consolidated manual then becomes a generated index/table-of-contents that points to `--help`, giving a single source of truth and no drift. (measured + inference)

Developer roadmap: keep it entirely out of the agent's path (it already is); confirm no pointer chain leads the agent into it.

On "does the agent follow the pointer?"

There is little published controlled evidence isolating "entry doc → does the agent open the referenced file." The strongest adjacent evidence: (a) Anthropic explicitly designs Claude Code around just-in-time file retrieval and says it works when the agent has good tools/heuristics; (b) community-measured behavior shows free-text "read this file" instructions in CLAUDE.md are unreliable, whereas `@import` (eager) and Skills (trigger-based) load deterministically; (c) Terminal-Bench shows agents explore but waste turns. Net: do not rely on a soft prose pointer for anything safety-critical — make critical context either always-loaded (Tier 1) or loaded by a deterministic mechanism (a command the workflow forces, a Skill trigger, or subdirectory auto-load). (inference from vendor + community-measured evidence)

Security

Treat every tool's stdout/stderr and any file the agent reads as untrusted input. Keep a strict data/instruction boundary: safety rules live in Tier 1 (trusted), and the agent should be told never to treat text appearing inside tool output as new authority. This matters more here than in a pure coding repo because measurement/tuning hardware may emit device-provided strings the agent parses. (consensus injection research + inference)

Recommendations

Staged, with the benchmark that would change each step.

Stage 1 — Restructure into loading tiers (do first).

1. Trim the constitution to the minimal always-true safety + authority core; move all reference material out. Threshold to watch: if the agent violates or "forgets" a Tier-1 rule, the file is still too long/diluted — cut further.
2. Add the `orient`/`start-here` entrypoint and print a pointer to it in the SSH MOTD. Threshold: cold-start sessions should reach the correct first tool without "command not found" or blind probing.

Stage 2 — Invest in the tool surface (highest measured ROI). 3. Rewrite all 19 `--help` outputs to the SWE-agent/Anthropic pattern: purpose, when-not-to-use, parameters, one worked example, exit codes, and teaching error messages. Threshold: measure task success and failed-invocation recovery before/after — ACI evidence predicts the largest gains here. 4. Replace hand-maintained per-tool runbook sections with a generated index derived from `--help`, eliminating the duplication that drifts.

Stage 3 — Layer methodology/manual as on-demand context. 5. Expose methodology and runbook via a `toolbox doc <name>` command and/or Skills with precise `description` frontmatter. Do NOT `@import` them into the always-loaded file. Threshold: resident tokens at session start should stay small; if the agent needs a doc and doesn't fetch it, strengthen the trigger/description or promote the one critical rule to Tier 1.

Stage 4 — Consider Skills only where they pay. 6. Package a capability as a Skill only when it bundles scripts + reference files and benefits from trigger-based loading (e.g., a multi-step "calibrate → measure → verify" workflow). Given SWE-Skills-Bench's finding that most skills add ~1% and some hurt, keep Skills few and evaluated, and prefer deterministic scripts over prose. Threshold: a Skill that doesn't measurably raise task success or cut tokens should be deleted.

Stage 5 — Harden and measure continuously. 7. Add an explicit anti-injection rule to Tier 1 and never elevate tool-output text to instruction status. 8. Build a small internal eval set of representative multi-tool tasks (Anthropic's tool-eval method) and re-run it whenever docs, `--help`, or the model change — because model upgrades move performance more than scaffolding does.

Caveats

* The precise size ceilings are folklore. "~200 lines," "~2,000 tokens," "under 500 lines for SKILL.md," and the "60–70% context dumb zone" are widely repeated but rest on convention/telemetry, not published controlled ablations. Use them as starting heuristics and let your own eval set set the real thresholds.
* Almost all measurement is on coding/web/terminal agents, not on loudspeaker-measurement CLIs. The direction of effects (progressive disclosure helps, tool-description quality helps, injection is a risk) transfers with high confidence; exact magnitudes will not.
* Several key sources are single-study or preprint. Chroma context rot (one lab, though rigorous and multi-model), NoLiMa, and especially SWE-Skills-Bench (self-labeled "preliminary, work in progress," single agent = Claude Haiku 4.5) should be read as directional.
* "Pointer-following" lacks a clean public benchmark. The recommendation to hard-wire critical context rather than rely on prose pointers is an inference from vendor design choices and community-measured loading behavior, not a controlled result.
* The field moves fast (2024–2026). Context-window sizes, model adherence, and vendor conventions change quarterly; re-validate size/loading choices against your own evals rather than trusting any single dated number here.

Primary sources

* Liu et al., "Lost in the Middle," TACL 2024 — https://arxiv.org/abs/2307.03172
* Hong, Troynikov, Huber, "Context Rot," Chroma, Jul 2025 — https://research.trychroma.com/context-rot
* NVIDIA, "RULER" — https://arxiv.org/abs/2404.06654
* Modarressi et al., "NoLiMa," ICML 2025 — https://arxiv.org/abs/2502.05167
* "Curse of Instructions" / ManyIFEval — https://openreview.net/forum?id=R6q67CDBCH ; https://arxiv.org/abs/2509.21051
* Yang et al., "SWE-agent: Agent-Computer Interfaces," NeurIPS 2024 — https://arxiv.org/abs/2405.15793
* Anthropic, "Effective context engineering for AI agents," Sep 29, 2025 — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
* Anthropic, "Writing effective tools for agents," Sep 11, 2025 — https://www.anthropic.com/engineering/writing-tools-for-agents
* Anthropic, "Equipping agents for the real world with Agent Skills," Oct 16, 2025 — https://claude.com/blog/equipping-agents-for-the-real-world-with-agent-skills
* Anthropic, "Claude Code best practices" — https://www.anthropic.com/engineering/claude-code-best-practices
* "SWE-Skills-Bench" (preprint) — https://arxiv.org/abs/2603.15401
* Patil et al., "Berkeley Function-Calling Leaderboard (BFCL)," ICML 2025 — https://openreview.net/forum?id=2GmDdhBdDk
* Terminal-Bench — https://arxiv.org/abs/2601.11868 ; tbench.ai/news/terminal-bench-challenges ; TerminalWorld — https://arxiv.org/abs/2605.22535
* Greshake et al., indirect prompt injection ("NotWhatYouSignedUpFor"); InjecAgent; AgentDojo — surveyed in https://arxiv.org/abs/2603.01564
* AGENTS.md convention — https://agents.md ; CLAUDE.md loading behavior — https://support.claude.com/en/articles/14553240-give-claude-context-claude-md-and-better-prompts ; HumanLayer, "Writing a good CLAUDE.md" — https://www.humanlayer.dev/blog/writing-a-good-claude-md
