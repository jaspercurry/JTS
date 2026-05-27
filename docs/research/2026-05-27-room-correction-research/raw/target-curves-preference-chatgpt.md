# Target Curves and Preference Tuning for JTS

## Executive summary

The strongest research-backed conclusion is not that there is one universally correct in-room target curve. It is that listeners generally prefer a **smooth in-room balance that slopes downward from bass to treble**, often with **some low-frequency rise relative to the midrange**, while the exact amount varies with the loudspeaker’s directivity, the room’s reflectivity, the listener, and the program material. The classic Brüel & Kjær work argued that a playback-room curve should be a little elevated in the bass and a little rolled off in the treble for typical commercial recordings, while Toole’s later work showed that a steady-state room curve is only interpretable if you also know the loudspeaker’s anechoic behavior and the room’s acoustics. Toole also emphasized that listeners do **not** generally prefer targets that rise toward high frequencies. citeturn13view3turn2view1turn2view0turn3view0

The second strong conclusion is that **preference variation is real and is largest at the spectral extremes**, especially bass. Toole’s review of Olive et al. found that trained and untrained listeners differed materially in their preferred low- and high-frequency balance, with untrained listeners tending to choose “more of everything.” In headphone work, Olive and colleagues found a majority cluster around the Harman target, plus smaller “more bass” and “less bass” clusters, and they found systematic effects of age, sex, and listening experience on preferred bass and treble levels. Older listeners in particular tended to prefer less bass and/or more treble, plausibly related to age-related hearing changes. citeturn3view0turn18view0turn18view1turn18view3turn8view0turn17search0turn17search5

For JTS, that means the architecture should stay explicitly layered. **Room correction** should address repeatable, physically correctable behavior, mainly in the bass and lower midrange, and should avoid “fixing” loudspeaker directivity or reflected-sound problems that EQ cannot solve. **Target curve** should be a separate declarative layer that expresses the intended in-room tonal balance. **Preference EQ** should be a reversible delta layer for subjective tuning, with hard limits, automatic headroom protection, and clear language that says “this is voicing, not accuracy.” This separation follows directly from Toole’s evidence on what equalization can and cannot do, and from Welti’s evidence that low-frequency seat-to-seat consistency has to come before meaningful global EQ. citeturn2view2turn3view0turn23view0

For product design, the most defensible default for JTS is a **reference target with modest bass rise and gentle downward tilt**, plus a **small, bounded set of reversible preference controls**. In novice mode, users should mostly interact with phrases such as “more bass,” “less boomy,” “warmer,” or “vocals more forward,” while deterministic code translates those phrases into broad, low-Q, headroom-aware deltas. In power-user mode, expose the underlying parameters, but keep room-correction range, boost ceilings, and summed per-band gain under hard constraints. citeturn13view3turn2view0turn18view1turn23view0

## Preferred target curves in the literature

The older Brüel & Kjær domestic-room work remains historically important because it explicitly said that the preferred playback curve in a room should **not** usually be flat at the listening position for ordinary commercial recordings. Motter wrote that for many commercial recordings the curve should “boost a little at low frequencies and roll off a little at high frequencies,” and that the usable evaluation range was weighted most strongly from about **60 Hz to 6 kHz**. He also noted that this shape was derived partly from listening tests and partly from average concert-hall curves. That makes the B&K curve valuable as a legacy reference, but it should be treated as a **historical practitioner target**, not a modern universal standard. citeturn13view3

Toole’s loudspeaker research changed the framing of the problem. In the 1986 JAES papers, higher-rated loudspeakers tended to have **smoother on-axis response**, **smoother sound power**, and **fewer irregularities in directivity-related behavior**. In the 2015 calibration paper, Toole argued that a room curve by itself is ambiguous because it is the product of the loudspeaker, the room, and the measurement method. In a more reflective room, the measured steady-state response moves toward the loudspeaker’s predicted room curve; in a dead room it approaches the direct sound. That is why JTS should never treat a measured room curve as a standalone truth. citeturn11view1turn11view3turn2view1

Toole also reviewed evidence from Olive et al. on **subjectively preferred steady-state room-curve targets** in a domestic room. The broad result was that listeners tended to prefer a curve that **rose toward low frequencies** relative to the midrange, aligning reasonably well with the predicted steady-state response of highly rated loudspeakers in a “typically reflective” room. At the same time, Toole stressed that the variation at the frequency extremes was substantial and that “a single target curve is not likely to satisfy all listeners.” He further noted that, across room-correction schemes, the common pattern was a **downward slope over at least part of the band**, and that none of the known targets rose toward high frequencies. citeturn3view0turn2view0

The Harman and Olive/Welti headphone work is relevant, but only with care. In Olive, Welti, and McMullin’s 2013 headphone target study, the most preferred headphone targets were the ones **derived from the in-room response of a calibrated loudspeaker system**, not the classic diffuse-field or free-field targets. The paper also described a standard in-room room-response target and a modified one with **less bass and treble**, and the modified variant was generally preferred in that experiment. This is strong evidence that target preference is not just about “flatness,” but it is still a **headphone** result and should not be transplanted directly into speaker-room EQ without accounting for room acoustics and loudspeaker directivity. citeturn1view1turn4search5turn2view1

A practical synthesis for JTS is therefore straightforward: the default speaker target should aim for **modest LF rise plus gentle broadband downward tilt**, while the exact curve remains adjustable inside bounded ranges. That is much more defensible than either a perfectly flat in-room target or a one-size-fits-all “Harman curve for speakers” simplification. citeturn13view3turn2view0turn3view0

### Working interpretation of the literature

| Source family | High-confidence finding | Product implication for JTS |
|---|---|---|
| Brüel & Kjær domestic-room work | Ordinary recordings often sound better with a little bass lift and a little HF roll-off rather than a flat listening-position response. | Use a reference target that is slightly bass-up / treble-down relative to flat. |
| Toole loudspeaker work | What matters most is a good loudspeaker with smooth direct sound and smooth directivity; room-curve EQ cannot rescue bad directivity. | Keep room correction conservative and avoid “correcting” reflected-field/directivity artifacts. |
| Toole calibration review | A room curve is not a universal target; the preferred slope depends on loudspeaker radiation and room reflectivity. | Separate measured correction from target voicing. |
| Olive/Welti preference work | Listeners often prefer targets derived from good loudspeakers in rooms, not free-field/diffuse-field abstractions. | Base the default target on in-room listening balance, not on “flat trace at seat” ideology. |

The table above is a synthesis of the cited literature, not a direct claim that all authors recommended the same numeric curve. citeturn13view3turn2view1turn3view0turn1view1

## Listener variation and what it means for JTS

Listener variation is robust enough that JTS should treat it as a core product requirement rather than noise around a single ideal. Toole’s review of domestic-room target studies showed substantial variation at both spectral extremes and concluded that one target is unlikely to satisfy everyone. In the same discussion, he noted that untrained listeners in the Olive et al. domestic-room experiment tended to choose **more bass and more treble** than trained listeners. That is exactly the kind of variation that should live in a preference layer, not inside the room-correction engine. citeturn3view0

The best quantified clustering result in the accessible literature comes from the Harman headphone program. Olive’s 2022 summary reported three listener classes: about **64%** clustered around the Harman target, about **15%** preferred roughly **4–6 dB more bass**, and about **21%** preferred about **2 dB less bass**. The same summary also reported that the “less bass” group had disproportionate representation from females and listeners over 50, while the “more bass” group was smaller and more male-skewed. This is not speaker-room data, but it is a strong demonstration that **bass preference segmentation is real and large enough to design for**. citeturn18view0turn18view1turn18view2

Age and hearing matter. Olive’s headphone summaries found that listeners in the **55+** group tended to prefer a brighter, less bass-heavy balance than younger groups, and he explicitly suggested age-related hearing loss as a possible explanation. That interpretation is consistent with NIDCD’s guidance that age-related hearing loss is common and typically affects older adults substantially, and with ISO 7029’s role as the standard statistical reference for age-related hearing-threshold deviation. citeturn18view3turn8view0turn17search0turn17search5

Room and loudspeaker behavior also change the meaning of a target. Toole showed that below roughly **200–300 Hz**, room modes dominate; above that region, **early reflections and loudspeaker directivity** remain perceptually important far higher in frequency than many users assume. He also argued that equalization cannot add or remove reflections, change reverberation time, reduce seat-to-seat bass variation on its own, or correct faulty loudspeaker directivity. Welti and Devantier then showed why the bass problem must be handled structurally: with one subwoofer or one-seat optimization, seat-to-seat bass variation can remain large enough that global EQ is ineffective; with multiple subwoofers, variation is reduced and global EQ becomes much more meaningful over an area. citeturn2view2turn3view0turn23view0

For JTS, the operational conclusion is simple. Preferences that mainly move **bass amount**, **treble amount**, or **vocal presence** should be treated as normal, expected user variation. Complaints that map to measurable repeatable LF excess should first be checked against the room-correction layer; complaints that do not map cleanly to repeatable measured problems should become reversible preference deltas. citeturn2view2turn18view1turn23view0

## Recommended DSP layer model for JTS

JTS should preserve three explicit DSP layers and make them visible in both code and UX.

The **room-correction layer** should contain only measurement-derived filters that target **repeatable, physically useful corrections**. In JTS’s current design, that naturally means time alignment, bass-management interactions, and mostly **cut-oriented LF correction** in the region where room modes dominate and multiple measurements show stable excess energy. Toole’s review supports this conservative approach: he lists several things conventional EQ cannot do, including fixing reflections, reverberation, seat-to-seat bass variation, or frequency-dependent loudspeaker directivity. Welti’s multi-subwoofer work adds that equalization becomes more effective only after spatial bass variation has been reduced. citeturn2view2turn3view0turn23view0

The **target-curve layer** should be a declarative description of the intended in-room tonal balance, independent of the measurement solver. Conceptually, it should answer only one question: *given a competent baseline result, what spectral balance should the system aim for?* A target curve is therefore not “correction” and should never be explained to users as measured truth. This distinction follows directly from Toole’s claim that one target will not fit all listeners, and from the reality that the same room-correction result can be voiced differently without either version becoming more or less “accurate” in the narrow measurement sense. Dirac’s official educational material makes the same point from a product angle: the best target depends on the room and the listener’s taste, and the same curve can behave differently in different rooms. That is a marketing source rather than peer-reviewed evidence, but it aligns well with the literature. citeturn3view0turn5search3

The **preference-EQ layer** should contain only reversible subjective deltas. In JTS terms, it should store intent and bounded parameter changes such as “+2 dB low shelf below 120 Hz” or “-1.5 dB broad cut at 250 Hz.” It should never rewrite the room-correction layer, and it should never be allowed to create narrow deep boosts into room nulls. The assistant should be allowed to propose a preference action, but deterministic code should own the resulting filters, gain staging, and hard safety limits. That follows directly from the literature showing both meaningful listener variation and hard physical limits on what EQ can repair. citeturn2view2turn18view1turn23view0

The cleanest signal flow is:

**Measurement model → room correction → target curve → preference EQ → automatic headroom trim → optional loudness compensation → safety limiter or protection logic**

That ordering preserves semantics. Room correction creates the best physically sensible baseline; target curve sets the intended voicing; preference EQ modifies that voicing; headroom compensation prevents clipping; optional loudness compensation remains distinct because it is **level dependent**, not a static room or taste parameter. The loudness step should therefore be presented as an optional additional mode, not as part of target or preference. This is an engineering recommendation inferred from the evidence, rather than a published standard. citeturn2view2turn3view0

In the UX, the assistant should use explicit language such as: “I can fix a measured room excess here,” versus “I can make a reversible voicing change here.” That wording is important because it prevents users from equating “preferred” with “measured-flat” or “AI-approved.” It also avoids the common failure mode where users ask for “more detail” and the system silently converts that into aggressive permanent upper-mid boost. citeturn1view1turn3view0

For A/B comparison and rollback, JTS should offer **three-way switching**: **room correction only**, **room correction plus target**, and **full system including preference**. All three states should be **level matched** by the same preamp/headroom logic, so the user is not biased by louder playback. Rollback should be instant and lossless because the preference layer is stored as a separate delta object, not as a destructive rewrite of the base correction. The recommendation to separate these A/B states is an engineering inference from the cited literature on listener preference variation and room-correction limits. citeturn3view0turn2view2

## Recommended target and preference schemas

The parameter exposure below is a JTS design recommendation synthesized from the literature. The numeric bounds are **not** published universal limits; they are a practical safety envelope derived from the facts that listeners want some target variation, that EQ is most trustworthy in the bass only when the behavior is spatially repeatable, and that LF boosts consume headroom and can worsen excursion risk. citeturn13view3turn3view0turn23view0turn18view1

### Recommended target curve schema

| Field | Recommendation for JTS | Why |
|---|---|---|
| Reference anchor | Normalize target at **500 Hz** or **1 kHz** | Keeps target description independent of overall playback gain. |
| Bass shelf gain | Default **+3 dB**, novice range **0 to +5 dB**, power-user hard limit **-3 to +6 dB** | Consistent with B&K-style LF lift and Olive/Toole evidence that LF preference varies materially. |
| Bass shelf corner | Default **120 Hz**, range **70 to 180 Hz** | Covers “weight” to “warmth” region without turning the target into a one-note sub-bass control. |
| Bass shelf Q | Gentle only, default **0.7**, range **0.5 to 1.0** | Broad tonal shaping, not modal surgery. |
| Broadband tilt | Default **-0.8 dB/octave**, novice range **-0.4 to -1.2 dB/octave**, power-user hard limit **0 to -1.5 dB/octave** | Matches the literature’s downward-slope consensus without letting novices create HF-rising pseudo-reference targets. |
| Treble shelf | Default **0 dB** relative to tilt, range **-2 to +2 dB** above about **4–8 kHz** | Useful for hearing and taste variation, but should remain secondary to the overall tilt. |
| Correction range | Default room correction to **20–300 Hz**; optional advanced extension to **500 Hz** only with broad, stable features | Below the transition region room EQ is most physically useful; above it directivity/reflections become ambiguous. |
| Maximum room-correction boost | Default **0 dB**, optional hard limit **+3 dB** | Cuts are usually safer than boosts in rooms; narrow null boosting should be blocked. |
| Maximum room-correction cut | Hard limit **-10 dB** per filter, with broad-Q preference for modal peaks | Prevents extreme solver behavior and audible overfitting. |
| Loudness compensation | **Separate optional layer**, off by default | It is level-dependent and should not be confused with room correction or target voicing. |

A sensible out-of-box target for JTS would therefore be: **anchor at 500 Hz, +3 dB low shelf centered around 120 Hz, -0.8 dB/octave tilt, no extra treble shelf, room correction mainly below 300 Hz**. That is close enough to the broad literature consensus to be defensible, while still leaving room for preference tuning. citeturn13view3turn2view0turn3view0

### Recommended preference EQ schema

A reversible preference profile should store **only deltas**, not re-run room correction or overwrite the target. The suggested schema is below.

| Stored item | Recommendation |
|---|---|
| Profile ID and timestamp | Required for rollback and history |
| Based-on measurement set ID | Required to know which room baseline the preference was tuned against |
| Based-on target profile ID | Required so preference stays clearly subordinate to a chosen target |
| Semantic intents | Store normalized intents such as `more_bass`, `warmer`, `vocal_presence_up`, `less_boom` |
| Deterministic filter deltas | Store the actual generated shelf/peak/cut filters with gain, Fc, Q, and enabled state |
| Assistant rationale text | Store a short plain-English explanation such as “preference voicing change, not room correction” |
| Headroom offset | Store the automatic negative preamp needed to preserve clipping margin |
| Safety metadata | Total positive gain, LF gain budget used, any blocked requests and why |
| User notes and A/B rating | Optional but valuable for undo and personalization |
| Expiry or portability flags | Optional; useful if preference should not automatically carry across rooms or speakers |

This model keeps preference **portable but not blind**. A profile can travel across targets or rooms only if JTS explicitly decides it should; otherwise it can warn that a preference tuned in one room may not transfer cleanly to another. That is especially important because Toole’s work shows that the same in-room tonal result can arise from different combinations of loudspeaker behavior and room reflectivity. citeturn2view1turn3view0

### Mapping subjective language to safe EQ intents

The mapping below is a **recommended JTS intent layer**, not a published dictionary. It is informed by Olive’s descriptor work on words such as boomy, thin, dull, bright, muffled, harsh, and missing mids, plus Toole’s warnings about what room EQ cannot fix. The numeric bounds are proposed JTS safety limits. citeturn1view1turn2view2turn3view0

| User phrase | Likely EQ intent | Safe novice bounds | Caveats |
|---|---|---|---|
| more bass | Broad low shelf up | **+1 to +3 dB**, Fc **90–150 Hz**, Q **0.5–0.8**; power-user hard limit **+6 dB** | First check whether the user really means more sub-bass, more warmth, or more punch. |
| less boomy | Broad upper-bass / low-mid cut | **-1 to -4 dB**, Fc **80–180 Hz**, Q **0.7–1.2** | If measurements show a repeatable modal peak, this may belong in room correction instead of preference. |
| warmer | Slight bass-up and/or treble-down voicing | Low shelf **+1 to +2 dB** below **150 Hz** and/or high shelf **-1 to -2 dB** above **4 kHz** | Avoid adding too much 150–300 Hz energy or it becomes muddy instead of warm. |
| brighter | Gentle positive tilt or treble shelf | High shelf **+1 to +2 dB** above **4–6 kHz**; power-user hard limit **+3 dB** | Not a cure for poor directivity or dark recordings; older listeners may need more than younger listeners. |
| more detail | Mild presence/air lift | **+0.5 to +1.5 dB** around **2–4 kHz** or above **6 kHz**, broad Q only | “Detail” is often a preference word, not an accuracy diagnosis. Overdoing this easily becomes harsh. |
| vocals recessed | Broad presence lift | **+1 to +2.5 dB** around **1–3 kHz**, Q **0.5–1.0** | Sometimes the better move is reducing low-mid masking instead of boosting presence. |
| harsh | Upper-mid / low-treble reduction | **-1 to -3 dB** around **2.5–6 kHz**, broad Q | Could also be caused by playback level, recording quality, or loudspeaker directivity. |
| thin | More bass weight and maybe lower-mid fill | Low shelf **+1 to +3 dB** below **120 Hz**, optional **+1 dB** around **150–250 Hz** | Never solve a deep measured room null with a narrow boost in this layer. |
| muddy | Low-mid cleanup | **-1 to -3 dB** around **150–400 Hz**, Q **0.7–1.2** | If speech clarity is the complaint, a small presence lift may work better than a bigger cut. |
| more punch | Emphasize kick region, not just deep bass | **+1 to +3 dB** broad peak or shelf around **60–120 Hz**, Q **0.7–1.0** | This is different from sub-bass; too much below 40 Hz adds weight without punch. |

### Safe bounds for experimentation

In novice mode, JTS should impose a **budgeted preference envelope**. A sensible policy is:

- no more than **three active preference filters** at once;
- no positive **narrow** boosts at all;
- no positive gain below **120 Hz** except via **broad low shelf**;
- total added positive gain from the preference layer capped at **+6 dB below 150 Hz** and **+3 dB above 150 Hz**;
- automatic negative preamp equal to **max positive summed boost + 1 dB**;
- block boosts into spatially inconsistent nulls or very deep dips;
- warn or cap bass boosts if playback level plus LF boost would exceed the speaker’s modeled safety envelope.

Those bounds are stricter than many hobbyist workflows, but they fit JTS’s stated goal of conservative deterministic control. They are most strongly justified by Toole’s warnings about what EQ cannot repair, by Welti’s evidence that LF consistency must be established before EQ can work broadly, and by the fact that preference variation is real enough that easy experimentation should be encouraged **without** letting users blow through headroom. citeturn2view2turn23view0turn18view1

### UX recommendations for novice and power-user modes

In **novice mode**, the user should mostly see outcomes, not filters. A sensible flow is: measure room; apply JTS baseline correction; choose a target such as **Reference**, **Gentle Bass**, or **Speech Forward**; then optionally use a preference assistant. The assistant should always label its output explicitly as either **room correction** or **preference voicing**, and should offer instant A/B states for **Measured baseline**, **Reference target**, and **Your preference profile**. citeturn3view0turn2view2

In **power-user mode**, expose the exact target-curve parameters and the separate preference-delta layer, but keep the same hard safety gates. Experts should be able to edit shelf gain, Fc, Q, tilt, treble shelf, correction range, boost ceilings, and loudness mode, and see graphs for each layer individually and in sum. The point is transparency without letting the UX blur the boundaries between measurement correction and taste. citeturn3view0turn13view3

## Prior art and workflow comparison

The table below distinguishes between **independently sourced findings** and **vendor or incomplete evidence**. Several tools named in the request were **not fully verified in this source pass**, so those rows are intentionally limited.

| Tool or workflow | What was verified here | Takeaway for JTS | Evidence status |
|---|---|---|---|
| Harman / Olive / Welti research workflow | Preference studies support a slight bass-up, downward-sloping balance and meaningful listener variation, especially in bass. | Build around a reference target plus bounded preference deltas. | Peer-reviewed and research-summary sources verified. citeturn3view0turn18view0turn1view1 |
| Brüel & Kjær domestic-room curve | Historical target favors slight LF rise and slight HF roll-off for typical recordings. | Good historical precedent for a gentle house-curve default. | Historical paper verified; not a modern universal standard. citeturn13view3 |
| Dirac | Official educational material says the best target depends on room acoustics and personal taste; the same curve can behave differently in different rooms. | Keep target editable and separate from correction. | Official vendor source, so useful but marketing-adjacent. citeturn5search3 |
| Sonarworks | Official blog presents flat as the recommended mixing target and B&K-style house curves as listening variants. | Reinforces the distinction between “reference” and “preferred listening” modes. | Official vendor blog, not independent validation. citeturn14search12 |
| REW | Current official implementation details were not verified in this pass. | Do not assume parity claims without current doc check. | Not fully verified. |
| HouseCurve and WiiM-style flows | Current official implementation details were not verified in this pass. | JTS can still borrow the simple phone-measurement and exportable-profile concept. | Not fully verified. |
| Audyssey Reference, Flat, Dynamic EQ | Current official implementation details were not verified in this pass. | The naming convention itself is a useful UX lesson: separate static target from level-dependent loudness. | Not fully verified. |
| Roon DSP | Current official implementation details were not verified in this pass. | Per-layer DSP presets and headroom management remain useful design inspirations. | Not fully verified. |
| Genelec GLM, Neumann MA 1, miniDSP workflows | Current official implementation details were not verified in this pass. | These ecosystems are relevant benchmarks for measured correction plus user voicing, but exact current behaviors need a dedicated vendor-doc pass. | Not fully verified. |

The most reliable competitive lesson from the verified sources is this: **serious systems either expose target voicing directly or implicitly support it, and vendor materials increasingly acknowledge that the “best” curve is room- and listener-dependent**. That is fully compatible with JTS’s architecture if the system keeps the room-fix, target, and preference layers separate. citeturn5search3turn14search12turn3view0

## Open questions and source notes

Several important questions remain open.

The literature strongly supports a bounded adjustable target, but it does **not** yield a single numeric speaker-room target that is guaranteed to win across directivities, rooms, and listener populations. Toole explicitly argues against over-reading steady-state room curves without loudspeaker and room context, and he says one target is unlikely to satisfy all listeners. That means JTS should present its default target as a **well-informed starting point**, not as a revealed truth. citeturn2view1turn3view0

The preference-language mapping proposed here is also not a universal lexicon. Olive’s work gives useful descriptor anchors such as **boomy**, **thin**, **dull**, **harsh**, **bright**, and **missing mids**, but there is still product work left to do in validating how speaker listeners use those terms in a room-correction wizard specifically. JTS should therefore log anonymous preference interactions and A/B outcomes, then use those logs to improve the intent map without letting the assistant bypass deterministic safety limits. citeturn1view1turn18view3

Finally, the strongest unresolved implementation risk is **speaker safety under bass preference changes**. Literature and vendor practice support conservative LF handling, but the correct hard caps for a specific JTS build will depend on the actual woofer, enclosure alignment, amplifier limits, crossover, and maximum SPL expectations. The architecture above therefore needs one more layer of device-specific policy: a speaker capability model that can refuse bass boosts when volume, content, and temperature margin say “no.” That recommendation follows from the general LF-equalization evidence, but the exact thresholds will be product-specific. citeturn23view0turn2view2

### Source notes

The most important sources used in this report are listed below. The citations open the underlying URLs.

- Floyd E. Toole, **“The Measurement and Calibration of Sound Reproducing Systems”**, JAES 2015. Key source for what room EQ can and cannot do, for the ambiguity of room curves without loudspeaker/room context, and for the evidence that preferred home targets slope downward overall and vary across listeners. citeturn1view0turn2view0turn2view1turn2view2turn3view0
- Floyd E. Toole, **“Loudspeaker Measurements and Their Relationship to Listener Preferences”**, JAES 1986. Key source for the importance of smooth on-axis/off-axis behavior and controlled directivity. citeturn10view0turn11view1turn11view3
- Henning Møller, Brüel & Kjær, **“Relevant Loudspeaker Tests … using 1/3 octave, pink-weighted, random noise”**, AES 1974. Historical source for the classic domestic-room house-curve concept. citeturn13view0turn13view3
- Sean Olive, Todd Welti, Elisabeth McMullin, **“Listener Preference for Different Headphone Target Response Curves”**, AES 2013. Key source for preference descriptors and for targets derived from in-room loudspeaker responses outperforming older headphone targets. citeturn1view1turn4search5
- Sean E. Olive, **“The Perception and Measurement of Headphone Sound Quality”**, *Acoustics Today* 2022. Key source for listener-class segmentation, demographic differences, and bass preference clusters. citeturn1view2turn18view0turn18view1turn18view2turn18view3
- Sean Olive, **blog summary of factors influencing preferred bass and treble**, 2015. Useful secondary source summarizing age, sex, and experience effects and linking them back to AES presentations. citeturn8view0
- NIDCD, **Age-Related Hearing Loss**, and ISO 7029 abstract. Used only to ground the report’s hearing-age cautions. citeturn17search0turn17search5
- Todd Welti and Allan Devantier, **“Low-Frequency Optimization Using Multiple Subwoofers”**, JAES 2006. Key source for the requirement to solve spatial LF variation before applying global EQ over a listening area. citeturn23view0
- Dirac official educational page on target curves and Sonarworks official blog on flat versus house curves. These are vendor sources and are treated here as product-practice references, not independent scientific validation. citeturn5search3turn14search12