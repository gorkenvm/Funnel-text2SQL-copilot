# Privacy and Consent Notes

Why the identity bridge is narrower than "everyone," why that narrowness
biases linked metrics in a specific direction, why hashing is not the
same thing as anonymisation, how the three markets differ in their
consent regimes, and the difference between the deterministic linkage
used here and probabilistic identity matching.

## The bridge requires consent AND sign-in — a double condition

`id_bridge` links a web pseudonymous id to an app device id only for
users who satisfy **two independent conditions**: they gave marketing/
analytics consent (`opt_in_flag`), **and** they signed in on both the
web and the app with the same account so the systems could be linked
deterministically. Either condition alone is not enough — a consented
user who never signs in on the app is not linkable, and a signed-in user
who declined consent is not linkable either (or should not be, in a
compliant pipeline). This double condition is the whole reason bridge
coverage is a minority of total traffic, not an oversight.

## Selection bias: linked metrics are upper or lower bounds, not the truth

Because linkage requires consent and sign-in, bridge-linked users are
systematically different from the general population: more engaged,
more trusting of the brand, more likely to have created an account in
the first place. Any metric computed only over `id_bridge` (download
counts, pairing rates, D30 retention by channel) is therefore a measure
of **the linkable population**, not of all users. Depending on the
metric's direction, this pushes readings toward an upper bound (linkable
users may be more likely to pair/retain than average) or requires
treating the number as a lower bound (linkable downloads undercount true
downloads, since non-consented or non-signed-in downloaders are
invisible to the join). Always name which direction the bias runs before
presenting a bridge-linked number as if it were a whole-population
number.

## Hashing is pseudonymisation, not anonymisation

`hashed_device_id` and `user_pseudo_id` are hashed or generated
identifiers, but a stable hash of a device or session is still a
**pseudonym**, not an anonymous value — the same input always produces
the same hash, so a hashed id can still be linked back to a real device
or person given enough auxiliary data (this is exactly why `id_bridge`
is treated as sensitive: it is where two pseudonyms become linkable, and
transitively, linkable to a real signed-in identity). Under most privacy
regimes, pseudonymised data is still personal data and is still in
scope for consent, retention and deletion obligations — "we hashed it"
is not, by itself, a privacy exemption.

## Three markets, three regimes

The three markets in this data (DE, UK, US) sit under materially
different consent defaults:

- **DE (Germany)**, under GDPR with a strict national interpretation,
  generally requires explicit opt-in before non-essential tracking —
  expect the lowest baseline consent/linkable rates of the three.
- **UK**, under UK GDPR (post-Brexit, broadly aligned with EU GDPR but
  diverging over time), sits close to the DE consent model but with some
  regulatory differences in enforcement and guidance.
- **US** has no single federal privacy law; state frameworks (e.g.
  California's CCPA/CPRA) are largely **opt-out** rather than opt-in —
  expect the highest baseline consent/linkable rates of the three,
  purely from the difference in default legal posture, before any
  product or UX effect is considered.

Any cross-market comparison of a bridge-linked metric should therefore
control for — or at least caveat — how much of a market-to-market gap is
consent-regime driven versus genuinely behavioural.

## Deterministic linkage here, not probabilistic matching

The join this system uses is **deterministic**: `id_bridge` records an
explicit `web_pseudo_id <-> app_device_id` pair created at a known
`linked_at` moment because the same authenticated user signed in on
both sides. This is different from **probabilistic identity matching**
(e.g. fuzzy device fingerprinting, IP/user-agent heuristics, statistical
household matching), which infers likely links without a hard identity
event and carries real false-link risk. Deterministic linkage is safer
and more defensible for a regulated product, at the cost of coverage —
it will always link fewer users than a probabilistic approach would,
which is the trade-off behind the "selection bias" note above.
