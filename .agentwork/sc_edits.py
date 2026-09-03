import json
E = []


def e(a, b, *lines):
    E.append({"range": [a, b], "text": list(lines)})


e(5, 53,
  '"""Contract tests for the spatial combiner + interference honesty screen.',
  '',
  'Layers: synthetic ground truth, power-domain arithmetic, the echo detector,',
  'analysis-grid bounding, then two real-data layers that skip unless their',
  'gitignored corpus roots are exported — ``JTS_FLAT_LIN_CORPUS`` (the',
  '2026-07-24/25 JTS3 corpus) and ``JTS_FLAT_LIN_S0`` (the 2026-07-25 S0',
  'session, a different capture protocol).',
  '"""')
e(123, 125, '# --- Synthetic corpus construction ---')
e(133, 136, '    """A smooth synthetic "true" response, rolled off above 8 kHz."""')
e(154, 161,
  '    """One synthetic capture: truth + seeded noise, combed by one echo.',
  '',
  '    Magnitude and IR are built from one complex spectrum, so the IR the',
  '    detector sees is the cause of the comb the combiner sees.',
  '    """')
e(188, 197,
  '    """Stratified delays spanning 150-490 us (~5-17 cm of path delta) with',
  '    seeded jitter.',
  '    """')
e(205, 212,
  '    """Mean (estimate - truth) over ``keep`` — the offset',
  '    :func:`_relative_rms_error` removes.',
  '    """')
e(219, 235,
  '    """RMS of (estimate - truth) over ``keep``, common offset removed."""')
e(240, 242, '# --- B. Power-domain arithmetic, pinned to hand-computed literals ---')
e(246, 261,
  '    """The combiner averages in linear power, never in dB.',
  '',
  '    Literals hand-computed as 10*log10((10**(a/10) + 10**(b/10)) / 2) per',
  '    bin, which differs from the naive dB mean by up to 17 dB here.',
  '    """')
e(278, 278)
e(282, 283, '    # With two positions the median IS the dB mean.')
e(291, 293,
  '    """The vectorised power mean matches plain-Python math on a random set."""')
e(311, 313, '# --- A. Synthetic ground truth ---')
e(317, 321,
  '    """A1 — averaging a decorrelated cloud recovers truth, rolloff included."""')
e(341, 342)
e(350, 381,
  '    """A1b — absolute level, which the offset-removed RMS metric cannot score.',
  '',
  "    The power mean carries the echo's own energy, +10*log10(1 + r**2) =",
  '    +0.529 dB at r=0.36 (measured +0.437 dB here; partial coherence over ten',
  '    stratified delays pulls it under the analytic value). Max-hold, which the',
  '    plan rejects as positively biased, lands +2.55 dB on the same cloud.',
  '    """')
e(405, 406)
e(419, 423,
  '    # 8 of the 10 stratified delays clear the resolution floor (3 * ~71.4 us);',
  '    # the 150 and 185 us members are unresolvable, not undetected.')
e(430, 432,
  '    """A2b — a cloud that did not move has position-stable nulls."""')
e(443, 455,
  '    """A3 — aligned nulls survive the average and the mean-vs-median screen',
  '    is blind to them, so ``geometry.locked`` is the only warning.',
  '    """')
e(477, 477)
e(482, 482)
e(487, 489,
  '    """The exclusion mask and the reported (f_lo, f_hi) intervals are one fact."""')
e(491, 493,
  '    # A partially-aligned cloud is what trips the screen: half the positions',
  '    # nulled at a bin, so the power mean and the median part company.')
e(511, 530,
  '    """The two spread statistics answer two questions; only one discriminates.',
  '',
  '    ``max_sigma_db`` (worst single bin) separates a moved cloud from a',
  '    stationary one. ``sigma_db`` (per-position band level) collapses in the',
  '    top octaves, where an octave spans many comb periods and every position',
  '    lands near ``1 + r**2``, but not at 1-2 kHz, where tau of 150-490 us puts',
  '    the comb period (2-6.7 kHz) wider than the band.',
  '    """')
e(538, 539,
  '    # A cloud that never moved disagrees with itself only by seeded noise.')
e(544, 544)
e(552, 552)
e(558, 558, '    # ...but not at 1-2 kHz, where one octave is narrower than one period.')
e(569, 587,
  '    """Both spread statistics on a hand-checkable case.',
  '',
  '    Two positions, one -10 dB notch, grid 700-1400 Hz in 100 Hz steps: only',
  '    the 1 kHz octave band has the ``MIN_BAND_BINS`` = 4 bins it needs, so',
  '    exactly one :class:`BandSpread` is reported over seven bins.',
  '    """')
e(604, 604)
e(613, 615,
  '    """N=1 is legal; spread is undefined, not zero."""')
e(630, 638, '# --- A2. Retained per-position curves ---')
e(642, 643,
  "    \"\"\"Retained rows are index-aligned with ``position_ids``, on the result's",
  '    own grid, and read-only.',
  '    """')
e(653, 655,
  '    # These captures share one grid, so the resample is the identity.')
e(661, 664,
  '    """The combined curves are recomputable from ``per_position_db``."""')
e(689, 699,
  '    """Smoothing costs three combined passes plus one per position."""')
e(712, 713)
e(719, 721,
  '    """Retained rows follow the decimated grid, not the captures\' own."""')
e(732, 735,
  '    """``CombinedResponse`` still constructs without the per-position fields."""')
e(770, 777,
  "    \"\"\"``usable_echo_estimates``'s result size is ``GeometryLock.n_confident``,",
  '    on every population this suite can build.',
  '    """')
e(793, 793)
e(799, 800, '    # A refused record is not evidence, however confident its raw fields.')
e(805, 807, '# --- Canonical grid contract ---')
e(818, 820,
  '    """The canonical grid takes the coarsest spacing over common support."""')
e(838, 840,
  '    """A log grid is rejected: the smoother binary-searches linear bins."""')
e(885, 889,
  '        # Malformed by SHAPE — not a pair at all.',
  '        ({"echo_search_us": (120.0,)}, "echo_search_us"),',
  '        ({"echo_search_us": None}, "echo_search_us"),',
  '        ({"echo_search_us": (120.0, 800.0, 900.0)}, "echo_search_us"),')
e(901, 924,
  '    """N6 — malformed *config* raises; malformed *data* refuses one position.',
  '',
  '    Shape is checked before value, and ``pytest.raises(ValueError)`` catches',
  '    neither ``IndexError`` nor ``TypeError``, so the shape rows discriminate',
  '    rather than passing incidentally.',
  '    """')
e(931, 938,
  "    \"\"\"N6's exception: a band above one capture's Nyquist refuses that",
  '    position rather than failing the combine.',
  '    """')
e(970, 972, '# --- C. Echo detector ---')
e(990, 995,
  '    """``_impulse_with_echo`` with a second, later reflection — two',
  '    independent bounces, not a comb and its rahmonic.',
  '    """')
e(1007, 1018,
  '    """C — a known echo is found to within 10% in delay and 2 dB in level.',
  '',
  "    The low anchor is 240 us: the window's bottom ``WINDOW_EDGE_MARGIN_STEPS``",
  "    is refused, and the estimators' near-floor under-read stretches that dead",
  '    zone to ~1.3 steps for the weakest echoes.',
  '    """')
e(1028, 1030,
  '    """The echo rides a shaped, rolled-off response rather than a delta pair."""')
e(1057, 1059,
  '    """A real arrival with no secondary one is refused by concentration x',
  '    corroboration, since the crest gate passes.',
  '    """')
e(1071, 1087,
  '    """The documented accuracy floor, both directions: tau within 3% over',
  '    240-500 us, and the bottom ``WINDOW_EDGE_MARGIN_STEPS`` refused rather',
  '    than under-read.',
  '    """')
e(1109, 1113,
  '    # The refusal is evidenced: both raw estimates landed within one quefrency',
  '    # step of the 120 us edge (measured 145.2 us and 135.4 us) and the ripple',
  '    # was concentrated — confidence alone could not have caught it.')
e(1143, 1193,
  '    """The search window is a rejection contract, swept so a clamp fails.',
  '',
  '    Eight true delays crossed with eight windows. Four properties: a reported',
  '    tau is inside the requested window; it is never pinned exactly to an edge',
  '    (the anti-clamp assertion); a delay above the window reports 0.0; a delay',
  '    below it is never usable evidence — it aliases up rather than vanishing,',
  '    and a window excluding tau can still contain a rahmonic at 2*tau, 3*tau.',
  '    """')
e(1226, 1235,
  '    """B1(a) — a true 830 us echo searched to 800 us refuses rather than',
  '    railing: both raw estimates stay unclamped outside the window.',
  '    """')
e(1253, 1275,
  "    \"\"\"SF1 — the window's lower edge is refused and the dead zone is one step.",
  '',
  '    A below-window echo aliases onto the bottom of the window with both',
  '    estimators agreeing, so refusing is the only honest answer; a genuine echo',
  '    1.5 steps above the edge is still reported. Parametrised over four lower',
  '    edges, so the rule is relative to the requested window.',
  '    """')
e(1279, 1284,
  '    # Below the window entirely — half its lower edge, far enough down that',
  '    # the aliasing is unambiguous.')
e(1294, 1295,
  '    # The refusal is evidenced: a surviving candidate was inside the margin.')
e(1318, 1324,
  '    """An impulse at 1000 plus reflections at exact **sample** offsets.',
  '',
  '    ``_impulse_with_echo`` rounds a delay in seconds to a sample, which would',
  '    hide the quantity under test; here the caller names the sample.',
  '    """')
e(1334, 1349,
  "    \"\"\"``search_us[0]`` is the floor of the envelope's candidate range.",
  '',
  '    Under ``ceil`` the envelope cannot select the sample below the window: at',
  '    (150, 1000) us and 48 kHz it answers from sample 8 (156.25 us after the',
  "    parabola's half-sample clamp), never sample 7 (145.833 us).",
  '    """')
e(1359, 1361,
  "    # The floor is sample 8 less the parabola's clamp; below it means the",
  '    # search reached outside the window.')
e(1367, 1368,
  '    # ...and the excluded sample is reported as a below-window arrival.')
e(1373, 1397,
  '    """No sample is both the first in-window candidate and a below-window',
  '    arrival.',
  '',
  '    Swept over every alignment of the lower edge within a sample, driven from',
  '    a reflection on the boundary sample itself. The sweep sits around sample',
  '    15 (~312 us) because the 5-19 kHz band resolves ~71 us, so a reflection',
  '    one or two samples off the direct arrival raises no local maximum.',
  '    """')
e(1399, 1401,
  '        # Lower edges at 15.0, 15.1, ... 15.9 samples: every alignment, with',
  '        # 15.0 the sample-aligned control where ``round`` and ``ceil`` agree.')
e(1415, 1425,
  "        # The envelope's answer and the below-window arrival are not the same",
  '        # sample: the arrival sits on whole sample ``first - 1`` and the',
  "        # envelope's lowest reachable answer is ``first`` less the parabola's",
  '        # clamp, so half a sample is the assertion rather than "one is bigger".')
e(1431, 1432,
  '        # ...and the first sample INSIDE the window is searched.')
e(1445, 1460,
  '    """``WINDOW_EDGE_SNAP_SAMPLES``: a sample-aligned edge written as a',
  '    decimal still means that sample.',
  '',
  '    Eight samples at 48 kHz is 166.6666...us, so a caller writes 166.6667 —',
  '    8.0000016 samples, which a bare ``ceil`` would read as 9 and charge a',
  '    whole sample of window for. The tolerance must stay far below a sample.',
  '    """')
e(1472, 1474,
  '    # A genuinely fractional edge is NOT snapped: the snap is not a rounding',
  '    # rule.')
e(1479, 1481,
  '    # The tolerance is a thousandth of a sample, so a hundredth away from an',
  '    # integer is still honoured as fractional.')
e(1488, 1499,
  '    """The envelope\'s upper bound is the last sample at or below ``search_hi``.',
  '',
  '    (150, 850) us at 48 kHz is 40.8 samples, so a ``round`` bound searched',
  '    sample 41 at 854.2 us, 4.2 us past the window the caller asked for.',
  '    """')
e(1507, 1510,
  "    # The ceiling is sample 40 plus the parabola's clamp, not \"below sample",
  '    # 41" — the old bound refined sample 41 to 854.15 us.')
e(1513, 1515,
  '    # ...and it is found when the window is widened by one sample.')
e(1526, 1562,
  '# The measured verdict for a 10-position cloud whose true delays span',
  '# _BELOW_WINDOW_CLOUD_US, searched in windows at or above it. Rows are',
  '# (search window, locked, reason, n_confident, rahmonic refusals).',
  '#',
  '# The first three rows are the edge rule; the last three are the regime it',
  '# cannot reach — a cepstral rahmonic of the excluded echo landing mid-window.',
  '# The last column pins WHICH rule declined, so a row asserts the screen',
  '# rather than emptiness.')
e(1580, 1603,
  '    """One below-window cloud swept across six raised windows: no row reaches',
  '    a cluster, and rows 4-6 are the rahmonic regime the edge rule cannot',
  '    reach.',
  '    """')
e(1617, 1618,
  '    # GEOMETRY_UNKNOWN is only honest while the usable set cannot cluster.')
e(1633, 1635,
  '    # Each refusal is recomputable from the two fields the record carries.')
e(1642, 1665,
  '    """SF1 — a below-window cloud at (300, 800) is edge-refused, not clustered.',
  '',
  '    A below-window echo is aliased rather than rejected, arriving as a',
  '    plausible in-window estimate: 150 us and 178 us echoes were reported as',
  '    318 us and 302 us. A resolution floor cannot catch that (318 us clears a',
  '    214 us floor); only the distance to the edge distinguishes the two cases.',
  '    """')
e(1667, 1671,
  '    # At (300, 800) exactly one position survives — the 400 us member, the',
  '    # only one whose both estimates cleared the margin — and one estimate is',
  '    # not a cluster.')
e(1683, 1684,
  '    # Every refusal is a rescued false positive.')
e(1690, 1691,
  '    # The same cloud in the window it belongs in reads correctly.')
e(1700, 1735,
  '    """A rahmonic false lock under a raised window is screened.',
  '',
  "    A comb's cepstrum repeats at 2*tau, 3*tau..., so a window excluding the",
  '    true delay can contain a rahmonic of it. Unlike aliasing it lands',
  '    anywhere in the window, so ``WINDOW_EDGE_MARGIN_STEPS`` cannot catch it:',
  '    the 150-400 us cloud searched at (700, 1000) used to lock at ~857 us.',
  '    The mechanism is asserted alongside the verdict, since a verdict-only',
  '    test would also pass if the detector had simply stopped working.',
  '    """')
e(1740, 1740)
e(1750, 1750, '    # --- The mechanism, so a regression is diagnosable rather than red. ---')
e(1756, 1758,
  '    # The two positions that used to be admitted are the two whose cepstral',
  '    # candidate is the third rahmonic.')
e(1766, 1769,
  '        # The envelope still corroborates the rahmonic: the screen works by',
  '        # noticing the fundamental, not by breaking corroboration.')
e(1771, 1773,
  '        # The refusing peak is the excluded echo: within half a quefrency step',
  "        # of this position's true delay.")
e(1778, 1778)
e(1785, 1812,
  '    """The rahmonic screen on one IR, both directions.',
  '',
  '    Fires: a 300 us echo searched in (700, 1000) — before the screen the',
  '    detector reported 876.1 us at confidence 1.000 — and the refusal names the',
  '    fundamental at 285.6 us. Does not fire: the same echo in the default',
  '    window, and a lone 850 us echo inside a raised (750, 1100) window, so the',
  '    screen keys on "is something below stronger", not on "is the window high".',
  '    """')
e(1821, 1822,
  '    # The candidate was the third rahmonic and the two estimators agreed.')
e(1825, 1826,
  '    # The evidence the refusal carries, so the verdict is recomputable.')
e(1836, 1840,
  '    # ...and the screen was awake while not firing: a raised',
  '    # ``RAHMONIC_FLOOR_STEPS`` would leave no analyzable region below a',
  '    # default-window candidate and degrade the rule to "no region, no opinion".')
e(1844, 1845,
  '    # A genuinely late echo inside a raised window is still measured.')
e(1856, 1902,
  '    """KNOWN LIMITATION — an honest late echo under a stronger earlier',
  '    reflection is refused as if it were a rahmonic.',
  '',
  '    "A much stronger peak sits below" is necessary for a rahmonic and not',
  "    sufficient, and the two populations' ``lower_peak_ratio`` interleave —",
  '    this honest case refuses at 2.448, a true rahmonic at 2.337 — so no',
  '    threshold on it separates them from one record. The failure direction is',
  '    a refusal rather than a wrong number, and the default window, which',
  '    contains the earlier reflection, measures the same IR cleanly.',
  '    """')
e(1911, 1913,
  '    # The refused measurement was honest: both estimators found the real echo.')
e(1918, 1919,
  '    # The refusing peak is the earlier real reflection, within half a step.')
e(1923, 1934,
  '    # The interleaving that makes this unfixable per-record: a genuine',
  '    # rahmonic lands BELOW this honest one on the statistic the screen uses.')
e(1950, 1952,
  '    # The remedy, on the same IR: the default window contains the earlier',
  '    # reflection, so no stronger peak sits below the candidate.')
e(1961, 1979,
  '    """The rule is "anything stronger below", not "an exact submultiple".',
  '',
  '    The worst measured case is the 205.6 us cloud member searched in',
  '    (650, 1000): its cepstral candidate lands at 749.8 us, 3.648x the truth,',
  '    which a submultiple test could only catch by accepting a whole quefrency',
  '    step of slop. The screen locates the fundamental at 214.2 us instead.',
  '    """')
e(1999, 2000,
  '    # The screen localises the fundamental better than either submultiple.')
e(2013, 2038,
  '    """``RAHMONIC_MARGIN`` is load-bearing in both directions.',
  '',
  '    Mutated to 0.0 the screen eats an ordinary 300 us reading; mutated to 1e9',
  '    the 150-400 us cloud locks again at ~857 us through (700, 1000). The',
  '    populations either side are measured by',
  '    :func:`test_rahmonic_margin_calibration_populations_bracket_the_constant`.',
  '    """')
e(2077, 2080,
  "# The two grids ``RAHMONIC_MARGIN``'s calibration is measured over, as",
  '# literals: windows that contain the echo, and windows that exclude it.')
e(2112, 2159,
  '    """The two populations ``RAHMONIC_MARGIN`` sits between, re-derived.',
  '',
  '    Each IR is measured by the shipped detector with the screen disabled',
  '    (``RAHMONIC_MARGIN`` patched to infinity) and classified by what the',
  '    pre-screen detector did: true positives (unrefused, confident, within 15%',
  '    of truth) whose ratio ceiling is the wall below the margin, and wrong',
  '    readings (unrefused, confident, >15% off) whose floor is the wall above.',
  '',
  '    Measured 2026-08-02: 2908 true positives, ceiling 0.9955; 439 wrong',
  '    readings, floor 2.7899. The assertions are the bracket and its width, not',
  '    those four figures — a grid this large samples both populations.',
  '    """')
e(2266, 2275,
  '    """One leg of the two-echo hazard sweep, in aggregate.',
  '',
  '    ``ratio_lo``/``ratio_hi`` span every record; ``refused_ratio_*`` span only',
  '    the ``rahmonic_of_lower_delay`` refusals (0.0 when there are none).',
  '    """')
e(2327, 2341,
  '    """Sweep the raised-window two-echo hazard and its two boundaries.',
  '',
  '    Three legs off one grid of an earlier, stronger reflection plus a later,',
  '    weaker one: **raised** (windows excluding the earlier reflection, where an',
  '    honest late echo is refused), **default_window** (the remedy, which must',
  '    refuse nothing) and **single_echo** (nothing below the candidate, which',
  '    isolates the raised window itself as not the cause).',
  '    """')
e(2400, 2435,
  '    """The raised-window hazard\'s shape: it needs the stronger-earlier-echo',
  '    geometry, the default window does not have it, and what it discards were',
  '    good measurements.',
  '',
  '    Measured 2026-08-02: the raised leg refuses 605 of 720 at ratios',
  '    1.678-4.513, discarded envelope estimates within 0.894% of the true late',
  '    echo; the default-window leg refuses 0 of 432 and the single-echo leg 0 of',
  '    370. The assertions are those walls, not the counts.',
  '    """')
e(2437, 2440,
  '    # The assertions below are walls, so print the figures a reader would',
  '    # otherwise have to re-derive. Captured by pytest unless ``-s``.')
e(2464, 2467,
  '    # Wall 4 — what was refused were good measurements. If this fails, the',
  '    # refusals have started landing on records that were wrong anyway.')
e(2473, 2492,
  '    """An edge refusal reports the corroboration it measured, not the 1.0',
  '    "could not be compared" marker.',
  '',
  '    ``tau_at_window_lower_edge`` fires only after both candidates were found',
  '    in-window and compared, so a real reading exists. Readings on this path',
  '    run from near-perfect agreement to gross disagreement; the refusal turns',
  '    on distance to the edge, not on agreement.',
  '    """')
e(2499, 2501,
  '    # Exactly the value recomputable from the raw fields the record carries.')
e(2514, 2516,
  '    # The contrast: an 830 us echo puts BOTH estimates above an 800 us',
  '    # ceiling, so neither corroborates anything and the 1.0 marker is honest.')
e(2523, 2524,
  '    # A refusal taken before either estimate exists is the same marker.')
e(2534, 2548,
  '    """The reported tau is always the envelope estimate; the cepstrum only',
  '    corroborates.',
  '',
  '    A cepstral fallback is unreachable: reporting needs ``confidence > 0``,',
  '    which needs ``corroboration < CORROBORATION_LOOSE``, which only the',
  '    both-estimators-in-window path produces.',
  '    """')
e(2567, 2581,
  "    \"\"\"B1(b) — a delay below the window's lower edge is refused, not dressed",
  '    up as an in-window one.',
  '',
  '    An echo at 95-110 us is under the 120 us search floor and below the ~71 us',
  '    quefrency step. The downstream resolution rule is anchored at zero delay,',
  '    so it screens only a low window; the edge rule generalises.',
  '    """')
e(2589, 2591,
  '        # Both guards are load bearing: the resolution floor binds on a low',
  '        # window, the edge rule on any.')
e(2602, 2616,
  '    """B1(c) — a cloud of unresolvable delays does not read as geometry-locked.',
  '',
  '    Ten positions spanning 60-150 us, all below the resolution floor: the',
  '    estimates collapse onto ~115-152 us and used to cluster at a fraction of',
  '    1.0. Both the edge refusal and the evidence rule now stop it.',
  '    """')
e(2635, 2640,
  "    # The pathology, reconstructed from the refusals' own raw fields: the",
  '    # estimates pile up (measured 114-152 us, median 135.4) and would have',
  '    # satisfied the +-15% clustering test at a fraction of 0.9.')
e(2655, 2663,
  '    """``ECHO_CONFIDENCE_FLOOR`` sits in the gap between the two measured',
  '    populations.',
  '',
  '    The negative controls are the impulse-with-no-echo families: they clear',
  '    the arrival-crest gate, so they exercise concentration x corroboration',
  '    rather than the early return.',
  '    """')
e(2705, 2707,
  '    """Detector input errors carry a machine-readable slug, so the combiner',
  '    need not match on message text.',
  '    """')
e(2715, 2727,
  '    """The reason slug is a wire value, so one assertion pins the literal a',
  '    consumer matches on rather than the imported symbol.',
  '    """')
e(2732, 2734,
  '    """One estimate clusters with itself; that must not read as a lock."""')
e(2744, 2759,
  '    """N5 — a zero-resolution, zero-delay diagnostic is inadmissible evidence.',
  '',
  "    With ``resolution_us == 0`` rule 3's threshold collapses to zero and a",
  '    ``tau_us`` of zero clears it. :func:`detect_echo` never emits that, so',
  '    this is a contract about what :func:`assess_geometry` accepts from any',
  '    source — a hand-built record, a deserialised one, a future detector.',
  '    """')
e(2780, 2781,
  '    # Not a ban on resolution_us == 0: it is the zero delay that is refused.')
e(2789, 2791,
  '    """``None`` (not measured) stays distinct from a zero-confidence',
  '    diagnostic (measured, found nothing).',
  '    """')
e(2805, 2817,
  '    """S2 — one malformed IR refuses that position and nothing else.',
  '',
  '    The curves, the screen and the geometry verdict are computed from the',
  '    rest, and ``None`` keeps meaning strictly "no IR was supplied".',
  '    """')
e(2838, 2842,
  '    # Two other positions carry a refusal of their own — the 157 and 185 us',
  "    # members sit inside the default window's lower-edge margin — which is a",
  '    # fact about those captures, present with or without the eleventh.')
e(2851, 2852,
  '    # The refusal contributes nothing: same numbers as the ten-position run.')
e(2860, 2862,
  '    """N6 — the echo window travels with the result, since a per-position tau',
  '    is only interpretable against the window it was searched in.',
  '    """')
e(2873, 2874,
  '    # Plumbed, not merely recorded: this cloud sits at ~150-500 us, so a',
  '    # 400-800 us window changes what the detector reports.')
e(2888, 2895, '# --- C2. The three S0 hardenings, on synthetic ground truth ---')
e(2899, 2908,
  '    """Magnitude-only Butterworth-shaped lowpass, steep enough to turn the',
  "    detector's 5-19 kHz default band into stopband.",
  '',
  '    Magnitude-only because the screen is a level comparison, and leaving phase',
  '    alone keeps the direct arrival where the rest of the fixture put it.',
  '    """')
e(2917, 2930,
  '    """S0-1 — a stopband residue signal is refused before either estimator runs.',
  '',
  '    A 320 us echo IR lowpassed at 2 kHz: the declared 200-2000 Hz passband',
  '    measures 48.6 dB above the analysis band, past the 25.0 dB margin.',
  '    """')
e(2946, 2947,
  '    # Declaring no passband leaves the detector as it was: the screen is',
  '    # opt-in, not a new floor.')
e(2956, 2962,
  '    """S0-1 — the screen stays quiet on an in-band signal, at two passbands a',
  '    caller might plausibly declare.',
  '    """')
e(2975, 2981,
  '    """S0-1 — a malformed ``signal_band_hz`` raises with a machine-readable',
  '    slug, like ``band_hz``: it is wrong for every capture at once.',
  '    """')
e(2988, 2990,
  "    # Nyquist clipping matches band_hz's: an upper edge above Nyquist is",
  '    # clipped rather than rejected.')
e(2996, 3022,
  '    """S0-2 — an arrival below the window is named rather than collapsing to',
  '    "ran, found nothing credible".',
  '',
  "    The S0 ground plane's geometry: a dominant arrival just below the window",
  '    (145 us at r=0.8) plus the real echo inside it (320 us at r=0.3). The',
  '    window is the sample-aligned (166.6667, 1000) us — the refusal needs the',
  "    envelope's own answer below ``search_us[0]``, which under ``ceil`` bounds",
  "    is reachable only within half a sample of the edge; at S0's own",
  '    (150, 1000) the edge rule names the same record instead.',
  '    """')
e(3031, 3032,
  "    # The mechanism: the envelope's answer is below the window it was given.")
e(3036, 3040,
  "    # The same IR through S0's (150, 1000) protocol window, a 7.2-sample lower",
  '    # edge: still a refusal, still disclosing the interloper, under the other',
  '    # of the two correct reasons.')
e(3057, 3060,
  '    # The same IR through the default window, which contains the interloper:',
  '    # nothing below to name. That is the remedy the refusal implies.')
e(3068, 3087,
  '    """S0-2 — "refused" and "ran, found nothing" stay different outcomes.',
  '',
  "    The rule needs the envelope's answer below the window AND a genuine",
  '    arrival down there to name. Swept over the whole 60-member negative',
  '    control family at the default window, where the family has no',
  '    below-window local maximum at all.',
  '    """')
e(3102, 3106,
  "                # Not exactly zero: this family's ceiling is 0.091 (see",
  '                # ``ECHO_CONFIDENCE_FLOOR``), and a low-but-nonzero score with',
  '                # an empty refusal is the same "found nothing credible".')
e(3110, 3111,
  '    # The state this test protects is actually reached.')
e(3116, 3131,
  "    \"\"\"``EARLIER_ARRIVAL_DOMINANCE_DB`` sits above the band-limited envelope's",
  '    own ringing, re-derived from one committed ladder.',
  '',
  '    ``_CALIBRATION_WRONG_READING_WINDOWS`` crossed with the 60-member negative',
  '    control family, 660 readings, plus the mutation that shows the floor is',
  "    load-bearing. The other population — S0's proud-capsule interlopers — is",
  '    real data and lives in section F.',
  '    """')
e(3184, 3189,
  '    # Re-derived 2026-08-02: 6 flips. The mutation still re-opens the defect',
  '    # on a population the shipped floor refuses 0 of.')
e(3196, 3198,
  '    # The gap in both directions: the ground-plane floor (-2.57 dB) is real',
  '    # data in section F; the ringing ceiling is measured here.')
e(3202, 3212,
  "    # A synthetic interloper at the ground plane's own level still refuses, so",
  '    # the floor is not merely "quiet enough to never fire". The window is the',
  '    # sample-aligned (166.6667, 1000) us, where the dominance rule is',
  '    # reachable; at a 7.2-sample edge the edge rule names the record instead.')
e(3220, 3243,
  "    \"\"\"An interloper that takes the envelope's answer but is too quiet to be",
  '    called dominant falls back to the honest empty refusal.',
  '',
  '    One geometry: a 145 us interloper of varying strength against a real',
  '    320 us echo at r=0.3, searched (166.6667, 1000) us — the sample-aligned',
  "    window, where the refusal is reachable. Read the band's width as an order",
  '    of magnitude, not a boundary.',
  '    """')
e(3251, 3251)
e(3256, 3257, '    # Inside the band: disclosed on the record, but not named.')
e(3261, 3262, '    # It really did take the answer — that is what makes this a band.')
e(3266, 3267, '    # ...and the interloper is still disclosed on the fallen-back record.')
e(3270, 3271,
  '    # Below the band: the interloper no longer wins the envelope at all.')
e(3279, 3301,
  '    """``effective_floor_us`` is reported on every record, including refusals.',
  '',
  '    ``search_us[0]`` understates what a window can see — the bottom',
  "    ``WINDOW_EDGE_MARGIN_STEPS`` is refused outright, so the default window's",
  '    real floor is ~191.4 us, not 120 us — and a consumer needs it most when',
  '    the window found nothing.',
  '    """')
e(3314, 3317,
  '    # Every refusal ``detect_echo`` can be driven to from a constructed input,',
  '    # including the three taken before the estimators run.')
e(3368, 3370,
  '    # The band-too-narrow refusal has its own coarser resolution: the field',
  '    # tracks the window AND the band.')
e(3387, 3390,
  '    # The one path that cannot compute it: combine_positions turning a',
  '    # detector raise into a refused record. The band is unknown there, so',
  '    # resolution_us and the floor are both 0.0.')
e(3407, 3424,
  '    """The disclosed floor and the applied edge rule are one boundary.',
  '',
  '    For any record that reached the edge check (at least one estimator landed',
  '    in the window), ``tau_at_window_lower_edge`` fires iff the lowest',
  "    in-window candidate is at or below that record's ``effective_floor_us``.",
  '    """')
e(3456, 3457,
  '    # Both sides of the boundary are exercised, so the "iff" is not vacuous.')
e(3463, 3470,
  '    """``band_deficit_db == STRENGTH_FLOOR_DB`` means "not measured", from all',
  '    three documented causes.',
  '',
  '    Cause 3 matters most downstream: a sub-bin passband must fail OPEN, or a',
  '    declared passband narrower than one FFT bin would refuse every position.',
  '    """')
e(3478, 3480,
  '    # Cause 2 — the detector returned before the screen ran. All three',
  '    # pre-screen refusals, each with a passband declared.')
e(3496, 3500,
  '    # Cause 3 — the screen ran but the passband covers no bin: at n_fft >=',
  '    # 4096 and 48 kHz the bins are ~11.7 Hz apart, so these are sub-bin',
  '    # passbands. Fail-open, and the deficit reads not-measured.')
e(3511, 3514,
  '    """S0-1 — the combiner passes the declared passband down and echoes back',
  '    the one actually applied.',
  '    """')
e(3534, 3535,
  '    # Malformed config raises rather than refusing every position.')
e(3542, 3551,
  '    """S0-3 — a verdict resting on the bare minimum of usable estimates is',
  '    qualified, not withheld.',
  '',
  '    The rule is ``n_confident == GEOMETRY_MIN_CONFIDENT and n_positions >=',
  '    2 * GEOMETRY_MIN_CONFIDENT``, and it is disclosure: the verdict, its',
  '    reason and every supporting number are unchanged.',
  '    """')
e(3577, 3578,
  '    # Two of three is the evidence that cloud had, not a shortfall.')
e(3580, 3582,
  '    # Structurally unreachable on GEOMETRY_UNKNOWN, which fires exactly when',
  '    # n_confident < GEOMETRY_MIN_CONFIDENT.')
e(3593, 3595, '# --- D. Analysis-grid bounding ---')
e(3599, 3601,
  '    """Three combed captures on a 2**18-point rFFT grid — 131073 bins, 8x',
  '    over ``MAX_ANALYSIS_BINS``.',
  '    """')
e(3621, 3684,
  '    """The analysis-grid cap must not change the curves consumers read.',
  '',
  '    The same cloud combined twice, once through the cap and once with it',
  '    lifted. Measured worst-bin agreement: the three smoothed curves 0.074,',
  '    0.075 and 0.085 dB; the raw pair ``power_mean_db`` 0.224 dB and',
  '    ``median_db`` 0.383 dB, both deliberately outside the 0.1 dB bound',
  '    because a raw per-bin curve cannot reproduce fine comb structure at 8x',
  '    coarser spacing; the retained per-position stack 0.404-0.429 dB and its',
  '    smoothed sibling 0.096-0.135 dB.',
  '',
  '    ``interference_nulls`` reads ``power_mean_db`` unsmoothed at each located',
  '    minimum, so the loose bound is asserted rather than merely disclosed; the',
  '    end-to-end cost of the cap on that statistic (0.033 dB on rung depths,',
  '    re-derived 2026-08-22) is asserted in tests/test_interference_nulls.py.',
  '    """')
e(3706, 3709,
  '    # The raw pair: bounded loosely, and asserted to be the ones that exceed',
  '    # 0.1 dB, so the docstring cannot rot in either direction.')
e(3718, 3721,
  '    # The retained per-position stack is raw in the same sense and is held to',
  '    # the same bound, row by row; the null gate reads it per position.')
e(3732, 3733,
  '    # A per-position curve cannot be quieter than the power mean of those',
  '    # same positions: averaging is what buys the mean its stability.')
e(3738, 3744,
  '    # The smoothed per-position curves sit between the two bounds: smoothing',
  '    # one position is not smoothing an average of three, so holding these to',
  "    # the combined curves' 0.1 dB would assert something untrue.")
e(3760, 3762,
  '    # ...and still far less than the raw stack they came from.')
e(3767, 3777,
  '    """Decimation is block averaging, not subsampling, and the result is still',
  '    a legal linear grid.',
  '',
  '    Checked on a flat-plus-notch construction where the answer is computable:',
  '    one -20 dB bin among 2**17 zeros must survive as a shallow dip rather than',
  '    vanishing or staying full-depth.',
  '    """')
e(3789, 3791,
  "    # Each decimated bin sits at its block's CENTRE, which is what the block's",
  "    # averaged power is the level of — not at the block's first bin.")
e(3794, 3795,
  '    # A trailing partial block is dropped.')
e(3818, 3820, '# --- E. Real-data smoke — 2026-07-24/25 JTS3 corpus ---')
e(3822, 3828,
  '# The corpus is gitignored and laptop-durable, and simply absent in CI. Its',
  '# root and skip gate live in tests/_flat_lin_corpus.py; the loader below stays',
  '# here because the two readers want different phases and program parameters.')
e(3835, 3876,
  '    """Era-exact reconstruction of the run 5 / run 7 impulse responses.',
  '',
  "    Program parameters are reused verbatim from the session's own forensics",
  '    script (``captures/flat-linearization-20260725/comb_forensics2.py``),',
  '    which is the authority on the DSP state those captures were taken under.',
  '    Registration is :func:`~tests._flat_lin_corpus.sweep_anchor`, which',
  '    locates the sweep by its own waveform, so where a composer puts it cannot',
  '    matter (#1879). MEASURE ships three repeats of each sweep, agreeing to',
  '    0.4%; the later repeats sit +16 and +32 samples off their scheduled',
  '    positions (~31 ppm of clock drift over the 43.8 s capture).',
  '    """')
e(3931, 3937,
  '# Deliberately NOT ``@requires_corpus``: every corpus reading above rests on',
  '# ``sweep_anchor`` finding the sweep by its own waveform, and pinning that on',
  '# a synthetic capture is what makes it visible to CI.')
e(3939, 3951,
  '    """``sweep_anchor`` locates the stimulus by cross-correlating the stimulus',
  '    itself, so a declared schedule position cannot move the answer (#1879).',
  '    """')
e(3962, 3963,
  '    # A capture that knows nothing about the schedule, as an archived WAV is.')
e(3970, 3971,
  '    # Now claim the composer moved it, in both directions.')
e(3979, 3984,
  '    """D — the detector reproduces the offline forensics finding: a discrete',
  '    echo at ~0.31 ms, ~-8.8 dB (r ~= 0.36), in both run 5 and run 7.',
  '    """')
e(3991, 4000,
  "        # The rahmonic screen's headroom on real data, which is where the",
  '        # "low-quefrency leakage cannot auto-refuse an honest reading" claim',
  '        # can be tested: measured 0.329-0.387. The bound is 0.45 rather than',
  '        # 0.5 so it is a tripwire on that 0.387 worst case.')
e(4007, 4014,
  '    """D — corpus frames read as geometry-locked, which is the detector',
  '    working: this corpus cannot be spatially averaged bounce-free.',
  '    """')
e(4027, 4029,
  '    """End-to-end on real data: the combiner surfaces the lock and the screen',
  '    stays quiet.',
  '    """')
e(4051, 4060,
  '# --- F. Real-data acceptance — the 2026-07-25 S0 session ---',
  '#',
  '# Gated on a second laptop-durable root and absent in CI, so a corpus',
  '# acceptance PR is not done until these have been seen to PASS.')
e(4062, 4066,
  '# Roots, skip gates, declared passbands and the era-exact deconvolution live',
  '# in tests/_flat_lin_corpus.py.')
e(4076, 4079,
  '    """The ten-position desk cloud — leg A, the control every leg-B claim is a',
  '    contrast against (~5 s, module-scoped).',
  '    """')
e(4085, 4088,
  '    """The electrical loopback\'s per-branch IRs, as the run left them."""')
e(4094, 4106,
  '    """S0-1 acceptance — the loopback\'s woofer branch is refused as stopband',
  '    residue.',
  '',
  '    Behind a 2 kHz LR4 lowpass the 5-19 kHz band holds only residue, and the',
  '    branch returned ``tau = 323.3 us`` at confidence 0.275 with an empty',
  '    refusal on the sweep stimulus (2026-07-25, s0-analysis/loopback).',
  '    """')
e(4116, 4118,
  '    # Without a declared passband the sweep still reports its confident-',
  '    # looking number, so this is a gate the caller supplies.')
e(4129, 4135,
  '    """S0-1 — the tweeter branch, whose passband overlaps the analysis band,',
  '    is measured on all three stimuli.',
  '    """')
e(4149, 4179,
  "    \"\"\"S0-1 — the deficit's separation depends on ``band_hz``, including where",
  '    it stops working.',
  '',
  '    Swept over six bands on the 13 S0 acoustic records and the 3 electrical',
  "    loopback woofer records. At a band whose lower edge sits on this speaker's",
  "    2 kHz crossover the woofer's own passband is inside the analysed band, the",
  '    deficit collapses to ~18 dB and the screen misses the case it exists for —',
  '    which is why the analysis band must stay clear of the crossover. All 13',
  '    acoustic records are the same JTS3 cdhorn.',
  '    """')
e(4198, 4199,
  '    # (band, does the screen still catch stopband residue)')
e(4231, 4232,
  '    # One octave up the margin is already thin.')
e(4238, 4239,
  "    # The honest population's spread across all six bands.")
e(4246, 4260,
  "    \"\"\"S0-2 / S0-4 — the main leg is unchanged and is the ground plane's",
  '    control.',
  '',
  '    Same speaker, program and window: four of these ten carry a below-window',
  "    arrival at 145.8 us, all 14.7-15.7 dB down, against the ground plane's",
  '    125-146 us at 0.6-2.6 dB down. One changed mic mounting is the whole',
  '    difference between a reading and a refusal.',
  '    """')
e(4261, 4280,
  '    # Per-position (tau, confidence) as s0-analysis/REPORT.md Q1, re-pinned',
  '    # 2026-07-27 when the reader moved onto ``_flat_lin_corpus.sweep_anchor``:',
  '    # that moved exactly two of the ten, both toward a stronger detection.')
e(4302, 4306,
  "    # Three of these ten, not four: cloud_04's sweep-aligned read no longer",
  '    # carries a below-window arrival at all.')
e(4312, 4313,
  '    # A desk-cloud interloper is ~12 dB quieter than a proud-capsule one.')
e(4319, 4327,
  "    \"\"\"S0-1 — the loopback report's 49.7 dB and the shipped statistic's",
  '    40.4 dB are the same signal, three metric changes apart.',
  '    """')
e(4387, 4440,
  '    """S0-1 — the gap ``BAND_BELOW_PASSBAND_MARGIN_DB`` claims, re-derived.',
  '',
  '    Three populations at the shipped defaults (5-19 kHz band, (120, 800) us',
  '    window), each against its own declared passband. The ground-plane leg is',
  '    the honest ceiling: tipping the cabinet at the floor cost top-octave',
  '    level, and it is still 13 dB clear of the threshold.',
  '',
  '    Every number here is a reading off a fixed archived corpus, so a moved pin',
  '    is classified before it is edited: a detector change moves all three',
  '    independently-captured populations coherently, while a reading change (the',
  '    loader, the composer, the calibration parse, the deconvolution) moves only',
  '    the populations sharing the broken input and is a bug in that input, not',
  '    in the pin. Never widen a tolerance to admit an unexplained number.',
  '    """')
e(4474, 4476,
  '    # Asserting 10 dB of clearance either side makes this a tripwire on the',
  '    # gap closing rather than a restatement of it.')
e(4484, 4485,
  '    # The per-population figures, so each is re-derived, not just the gap.')
e(4496, 4500,
  '    # The acoustic subset is the 16 records ``band_deficit_db`` quotes a range',
  '    # for; the electrical in-band controls read either side of zero and are',
  '    # asserted separately below.')
e(4518, 4557,
  '    """S0-2 acceptance — the three ground-plane records refuse by name and',
  '    carry the interloper.',
  '',
  '    Leg B left the capsule centimetres proud of a hard floor, manufacturing a',
  '    dominant arrival at 125-146 us (4.3-5.0 cm of path, r = 0.74-0.93). All',
  '    three used to report ``confidence = 0.000`` with an empty refusal. Which',
  "    refusal names them depends on the window's sample alignment: through the",
  '    leg-B protocol window (150, 1000) us — a 7.2-sample lower edge — the',
  '    envelope answers 156.25 us inside the edge margin and',
  '    ``tau_at_window_lower_edge`` returns first; through the sample-aligned',
  '    (166.6667, 1000) us it is ``earlier_dominant_arrival``. Both windows are',
  '    asserted: no delay, no confidence, and the interloper at the delay and',
  '    level the S0 report tabulated.',
  '    """')
e(4566, 4571,
  '    # 8 samples at 48 kHz written as the four decimals a caller would use —',
  '    # 8.0000016 samples. Deriving it as ``8.0 / SAMPLE_RATE * 1e6`` gives a',
  '    # value that round-trips to exactly 8.0 and makes the snap a no-op here.')
e(4593, 4594,
  '            # The envelope rails one sample up, at the first in-window sample',
  "            # less the parabola's clamp, on both windows.")
e(4597, 4598,
  '            # ...and the interloper the refusal is named for.')
e(4601, 4602,
  '            # Louder than anything the window contained, which is why it took',
  '            # the answer.')
e(4605, 4607,
  '        # The dominance rule is reached at the aligned window and pre-empted at',
  '        # the protocol one: the documented refusal precedence.')
e(4614, 4632,
  '    """S0-4 acceptance — the ground-plane arrivals sit under the default',
  "    window's ~191.4 us effective floor, so that window structurally cannot",
  '    report them.',
  '',
  "    The arrivals' delays are measured through the protocol window that can see",
  '    them rather than asserted against literals.',
  '    """')
e(4637, 4638,
  '        # Inside the default window there is nothing below to name.')
e(4641, 4643,
  "        # The claim the field lets a consumer make, against this capture's own",
  '        # measured arrival.')
e(4654, 4663,
  '    """S0-3 — the three-position leg is not thin evidence: zero usable',
  '    estimates is a shortfall, not the disproportion the flag reports.',
  '    """')
e(4684, 4690,
  '    # Every position refused by name, never a silent confidence collapse.',
  '    # Which of the two refusals it is belongs to',
  '    # :func:`test_ground_plane_positions_report_the_proud_capsule_arrival`.')
e(4697, 4699, "# --- Per-position residual — the design brief's §4.2 trend surface ---")
e(4711, 4716,
  '    """The role is carried, never read: the reduction is byte-identical',
  '    either way, so the combiner never becomes a weighted one.',
  '    """')
e(4730, 4736,
  '    """A position that sat off the mean reports a bigger residual.',
  '',
  '    An absolute dB target would pin the synthetic fixture; the discrimination',
  '    and the role travelling with it are what must hold.',
  '    """')
e(4739, 4740,
  '    # One position 4 dB hot across the whole band — the anchor-outlier',
  '    # signature this surface exists to see.')
e(4760, 4762,
  '    """The trusted band is the caller\'s, and a narrower one grades fewer bins."""')
e(4801, 4809,
  '    """The estimator reads the SMOOTHED pair, kept as a measurement.',
  '',
  '    Run on the raw curves, a broadband +4 dB offset on one position of four',
  "    does NOT make it the worst position: each position's own comb contributes",
  '    ~2.5 dB and swamps the level term.',
  '    """')
e(4830, 4830)

print(json.dumps(E))
