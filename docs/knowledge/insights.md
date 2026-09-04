# Analytical Insights and How to Falsify Them

Working hypotheses an analyst should reach for when a KPI looks
surprising, framed so each one is falsifiable with a follow-up query
rather than accepted on first read.

## Why mobile completion is low: traffic quality vs. mobile UX

A recurring pattern in completion-rate cuts is that the mobile
completion rate is low relative to desktop — mobile users start the
hearing test at a similar rate but finish it less often, so the observed
completion rate on mobile ends up lower than on desktop. Two competing
hypotheses explain a lower mobile completion number, and they point to
very different fixes:

1. **Traffic quality**: the channels that skew mobile-heavy (for
   example a paid social channel) simply bring lower-intent traffic —
   the drop-off is about *who* is arriving, not the device they arrive
   on.
2. **Mobile UX friction**: the hearing-test flow itself is harder to
   complete on a small screen (audio permissions, form length, session
   interruptions), so completion is low *because of the device*,
   independent of traffic source.

**Falsification test**: cross-tabulate completion rate by device
*within* each channel (not device alone, and not channel alone). If
mobile completion is low in every channel by roughly the same margin,
including channels that are not mobile-heavy, mobile UX friction is the
better explanation. If the mobile completion gap only shows up in
specific mobile-skewed channels and disappears when you compare
mobile-vs-desktop users *within the same higher-quality channel*,
traffic quality is the better explanation. Do not conclude either from
the device-only cut alone — it cannot distinguish the two hypotheses on
its own, and a device-only view is exactly the input that makes this
question look simpler than it is.

## Platform pairing comparisons need a market-mix caveat

iOS vs. Android pairing-rate comparisons are confounded by market mix:
markets differ both in their iOS/Android install base split and in their
baseline propensity to pair a hearing aid at all (itself affected by the
consent-regime differences in `privacy.md`). An apparent "iOS pairs
better than Android" reading can be entirely a market-mix artifact if,
say, the platform with the higher raw pairing rate simply has more of
its share in a market that paired well for unrelated reasons. Always
read platform comparisons **within** a single market (or with market
held constant) before attributing a gap to the platform itself.

## Volume and quality are different axes — report both together

A channel can lead on volume (test starts, downloads) while lagging on
quality (completion rate, pairing rate), or vice versa. Recommending a
budget shift toward "the channel with the most downloads" without also
checking its completion and pairing rates can move spend toward a
channel that produces more top-of-funnel noise rather than more
paired devices. Any channel-mix recommendation should cite a volume
metric and a quality metric side by side, not one in isolation.

## Support-session correlation with retention is not causal

Users who trigger a `remote_support_session` and later show higher
retention or pairing rates are not necessarily retained *because* they
got support. The more parsimonious explanation is reverse causation and
selection: users who are already more engaged and more invested in the
product are both more likely to reach out for support when something
goes wrong, and more likely to stick around regardless. Treating the
correlation as "support causes retention" and recommending "drive more
support contacts" as a retention lever is not supported by this data
alone — it would need a controlled experiment (e.g. randomised proactive
outreach) to separate the causal effect of support from the selection
effect of who asks for it.
