# Attribution Notes

What "channel" means in two different places in this schema, why they
disagree, and why this system reports first-touch and last-touch views
side by side instead of a blended multi-touch score.

## Two different "channel" columns, two different attribution models

- `id_bridge.acquisition_channel` is assigned once, at the moment a user
  becomes bridge-linked (consented + signed in), and behaves like a
  **first-touch** style channel: whichever channel is recorded there
  reflects how that user's identity was originally acquired, not
  whatever campaign brought them back most recently.
- `web_events.utm_campaign` is captured **per web session**, so it
  behaves like a **last-touch / session-level** tag: if the same
  pseudonymous web user returns later through a different campaign (or
  organically), the utm value on that later session reflects the new
  touch, not the original one.

These are genuinely different attribution models living in the same
schema, not two ways of reading the same fact. A user acquired via a
paid channel who later returns organically and completes the test will
show `acquisition_channel = paid` in `id_bridge` but `utm_campaign =
organic/none` on the completing session in `web_events`. Neither column
is "wrong" — they answer different questions ("who brought this person
in the first place" vs. "what brought them to this particular session")
and should be labelled as such whenever they are reported, rather than
merged into one unlabelled "channel."

## Why this system does not report multi-touch attribution

A defensible multi-touch attribution model (linear, time-decay,
U-shaped, or a trained/data-driven model) needs a **complete,
timestamped touchpoint history per identity** across every session and
device, not just the two attribution snapshots available here. Building
one from this schema would require:

- deterministic identity resolution at *every* touch, not only at the
  one moment a user becomes bridge-linked (`linked_at` is a single
  point in time, not a running log of every prior touch that led up to
  it); and
- a chosen credit-splitting model, which is itself an analytical
  decision that should not be made silently inside a KPI definition.

Because the available schema captures only a first-touch snapshot
(`acquisition_channel`) and a last-touch/session snapshot
(`utm_campaign`), rather than a full touchpoint log, a multi-touch
number built on top of it would look precise while resting on data the
schema cannot actually support — it would overstate the confidence of
the analysis. Reporting first-touch and last-touch views side by side,
each explicitly labelled, is the more honest choice: it shows what the
data can support and where the two views agree or disagree, instead of
collapsing them into a single blended figure that implies a granularity
the underlying data does not have.
