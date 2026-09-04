"""
Synthetic data generator — hearing-care funnel demo case.

Produces three tables mirroring the case schema:
  web_events(user_pseudo_id, session_id, event_name, event_timestamp, page_location,
             utm_campaign, device_category, country, consent_state)
  app_events(hashed_device_id, platform, event_name, event_timestamp, app_version)
  id_bridge(hashed_id, market, opt_in_flag, acquisition_channel,
            web_pseudo_id, app_device_id, linked_at)   -- extended mapping design (task 5.1)

Calibrated to the reference funnel (42% / 55% / 61% / 78%) with planted patterns:
  * organic completes >> paid-social completes (traffic-quality hypothesis H1)
  * mobile web completes < desktop (mobile UX hypothesis H2)
  * TikTok: high downloads, low pairing (volume vs quality story)
  * iOS pairing ~9pt above Android within each market
  * consent rates differ by market (DE < UK < US)
  * latent engagement drives BOTH remote_support usage and D30 (correlation trap)
  * ~15% of users fragment across two pseudo_ids (cross-device artefact)
  * ~25% of downloaders (and ~5% of completed-not-downloaded users) are
    "switchers": their completion session closes on a DIFFERENT utm than
    the one that started them, skewed toward discovery channels (paid
    social) originating and retargeting/brand-search/organic closing --
    so first-touch and last-touch attribution genuinely disagree (M4b)
  * ~8% of all users restart the hearing test in a later session (1-2
    extra hearing_test_start events, 1-14 days later); funnel logic uses
    MIN/first-qualifying-event so these never change funnel counts (M4b)

Ground truth (true user_id per row) is kept in a private file for validation
only; it also carries `switcher`/`last_channel`/`repeat_start` (M4b additions).
"""
import hashlib
import numpy as np
import pandas as pd
from pathlib import Path

RNG = np.random.default_rng(42)
N = 100_000
T0 = pd.Timestamp("2026-06-02")          # window start
HORIZON = pd.Timestamp("2026-08-30 23:59:59")  # window end (90 days)
OUT = Path(__file__).resolve().parents[1] / "data"

# ---------------------------------------------------------------- user attributes
markets = RNG.choice(["DE", "UK", "US"], N, p=[0.40, 0.25, 0.35])

channels = RNG.choice(
    ["paid_social_meta", "paid_social_tiktok", "paid_search_brand",
     "retargeting_meta", "organic_direct"],
    N, p=[0.32, 0.18, 0.10, 0.08, 0.32])

# paid social skews mobile (confound between H1 and H2 — intentional)
p_mobile = np.where(np.isin(channels, ["paid_social_meta", "paid_social_tiktok"]),
                    0.82, 0.55)
device = np.where(RNG.random(N) < p_mobile, "mobile",
                  np.where(RNG.random(N) < 0.12, "tablet", "desktop"))

ios_share = pd.Series(markets).map({"US": 0.58, "UK": 0.50, "DE": 0.38}).to_numpy()
platform = np.where(RNG.random(N) < ios_share, "iOS", "Android")

consent_rate = pd.Series(markets).map({"DE": 0.32, "UK": 0.42, "US": 0.55}).to_numpy()
consent = RNG.random(N) < consent_rate

engagement = RNG.beta(2.2, 3.0, N)        # latent driver (support + retention)

start_ts = T0 + pd.to_timedelta(RNG.random(N) * 89, unit="D")

def calibrate(p_raw, target):
    """Scale probabilities multiplicatively so the mean hits the target."""
    p = np.clip(p_raw, 0.01, 0.97)
    for _ in range(8):
        p = np.clip(p * (target / p.mean()), 0.01, 0.97)
    return p

# ---------------------------------------------------------------- stage 1→2: complete (target .42)
ch_complete = pd.Series(channels).map({
    "organic_direct": 0.60, "paid_search_brand": 0.55, "retargeting_meta": 0.50,
    "paid_social_meta": 0.34, "paid_social_tiktok": 0.30}).to_numpy()
dev_adj = pd.Series(device).map({"mobile": -0.08, "tablet": -0.02, "desktop": +0.10}).to_numpy()
p_complete = calibrate(ch_complete + dev_adj + 0.05 * engagement, 0.42)
completed = RNG.random(N) < p_complete

# ---------------------------------------------------------------- stage 2→3: download (target .55)
ch_dl = pd.Series(channels).map({
    "organic_direct": 0.58, "paid_search_brand": 0.57, "retargeting_meta": 0.60,
    "paid_social_meta": 0.52, "paid_social_tiktok": 0.55}).to_numpy()
p_dl = calibrate(ch_dl + 0.10 * engagement, 0.55)
downloaded = completed & (RNG.random(N) < p_dl)

# ---------------------------------------------------------------- stage 3→4: paired (target .61)
ch_pair = pd.Series(channels).map({
    "organic_direct": 0.72, "paid_search_brand": 0.68, "retargeting_meta": 0.62,
    "paid_social_meta": 0.58, "paid_social_tiktok": 0.40}).to_numpy()
plat_adj = np.where(platform == "iOS", +0.045, -0.045)   # ≈9pt gap
p_pair = calibrate(ch_pair + plat_adj + 0.08 * engagement, 0.61)
paired = downloaded & (RNG.random(N) < p_pair)

# ---------------------------------------------------------------- stage 4→5: D30 active (target .78)
uses_support = paired & (RNG.random(N) < np.clip(0.15 + 0.55 * engagement, 0, 0.9))
p_d30 = calibrate(0.45 + 0.55 * engagement + 0.05 * uses_support, 0.78)
d30 = paired & (RNG.random(N) < p_d30)

# ---------------------------------------------------------------- timestamps downstream
complete_ts = start_ts + pd.to_timedelta(RNG.integers(8, 25, N), unit="m")
dl_delay_days = RNG.exponential(1.2, N).clip(0, 14)
dl_ts = complete_ts + pd.to_timedelta(dl_delay_days, unit="D") \
                    + pd.to_timedelta(RNG.integers(1, 120, N), unit="m")
pair_delay = RNG.exponential(2.5, N).clip(0.02, 21)
pair_ts = dl_ts + pd.to_timedelta(pair_delay, unit="D")

# censor anything beyond the horizon
completed &= (complete_ts <= HORIZON)
downloaded &= completed & (dl_ts <= HORIZON)
paired &= downloaded & (pair_ts <= HORIZON)
d30 &= paired & (dl_ts + pd.Timedelta(days=34) <= HORIZON)  # runway required

# ---------------------------------------------------------------- ids
def h(prefix, arr):
    return [hashlib.md5(f"{prefix}:{v}".encode()).hexdigest()[:16] for v in arr]

uid = np.arange(N)
pseudo1 = np.array(h("web", uid))
# cross-device fragmentation: 15% own a second pseudo_id used for the completion session
frag = RNG.random(N) < 0.15
pseudo2 = np.array(h("web2", uid))
device_id = np.array(h("dev", uid))
hashed_id = np.array(h("crm", uid))

signin = downloaded & consent & (RNG.random(N) < 0.68)   # linkable = consent AND sign-in

utm = pd.Series(channels).map({
    "paid_social_meta": "summer_hearing_meta", "paid_social_tiktok": "tiktok_awareness",
    "paid_search_brand": "brand_search", "retargeting_meta": "retargeting_meta",
    "organic_direct": None}).to_numpy()
consent_state = np.where(consent, "granted", "denied")

# ---------------------------------------------------------------- multi-touch: switchers (task M4b.1)
# Real users don't stay on one channel for a whole journey. Among users who
# reach a completion session (there is nothing to relabel without one), a
# fraction close on a DIFFERENT utm than the one that started them.
# Switch propensity skews toward the two discovery channels (paid social),
# so first-touch (who was acquired) and last-touch (who gets credit for
# closing) tell visibly different stories for the same population.
# id_bridge.acquisition_channel is built from `channels` alone (below) and
# is therefore NEVER affected by this — it stays true first-touch.
discovery = np.isin(channels, ["paid_social_meta", "paid_social_tiktok"])
switch_weight = np.where(discovery, 1.0, 0.18)
switcher = np.zeros(N, dtype=bool)
dl_switch_pool = downloaded            # completed is implied by downloaded
nd_switch_pool = completed & ~downloaded
switcher[dl_switch_pool] = RNG.random(dl_switch_pool.sum()) < calibrate(
    switch_weight[dl_switch_pool], 0.25)
switcher[nd_switch_pool] = RNG.random(nd_switch_pool.sum()) < calibrate(
    switch_weight[nd_switch_pool], 0.05)

closing_idx = RNG.choice(3, size=N, p=[0.40, 0.25, 0.35])
closing_utm_options = np.array(["retargeting_meta", "brand_search", None], dtype=object)
closing_utm = closing_utm_options[closing_idx]
# closing_utm_final: the utm carried by the COMPLETION session, and by any
# repeat test-start session (below) since those inherit whatever channel
# already closed the journey. Equals the original starting utm for
# non-switchers, so nothing changes for 75%+ of the population.
closing_utm_final = np.where(switcher, closing_utm, utm)

def _utm_to_channel(utm_arr):
    """Inverse of the channel->utm map above, used only for ground truth."""
    s = pd.Series(utm_arr)
    return np.select(
        [s == "summer_hearing_meta", s == "tiktok_awareness",
         s == "brand_search", s == "retargeting_meta"],
        ["paid_social_meta", "paid_social_tiktok", "paid_search_brand", "retargeting_meta"],
        default="organic_direct")

last_channel = _utm_to_channel(closing_utm_final)   # ground truth only

# ---------------------------------------------------------------- repeat test starts (task M4b.2)
# ~8% of ALL users restart the hearing test in a later session: 1-2 extra
# hearing_test_start events, 1-14 days after the first start, same
# pseudo_id (pseudo1), utm = closing_utm_final (so the chronologically
# latest event never redefines last-touch away from what the completion
# session already established -- see the invariant check in the
# validation report below). Funnel logic uses MIN(event_timestamp), so
# these extra events cannot move first_test_start_ts or any flag/count.
repeat_user = RNG.random(N) < 0.08
repeat_second = repeat_user & (RNG.random(N) < 0.30)
rep1_ts = start_ts + pd.to_timedelta(1 + RNG.random(N) * 13, unit="D")
rep2_ts = start_ts + pd.to_timedelta(1 + RNG.random(N) * 13, unit="D")
rep1_mask = repeat_user & (rep1_ts <= HORIZON)
rep2_mask = repeat_second & (rep2_ts <= HORIZON)

users = pd.DataFrame(dict(
    user_id=uid, market=markets, channel=channels, device=device, platform=platform,
    consent=consent, frag=frag, engagement=engagement,
    completed=completed, downloaded=downloaded, paired=paired, d30=d30,
    uses_support=uses_support, signin=signin,
    start_ts=start_ts, complete_ts=complete_ts, dl_ts=dl_ts, pair_ts=pair_ts,
    pseudo1=pseudo1, pseudo2=pseudo2, device_id=device_id, hashed_id=hashed_id,
    utm=utm, consent_state=consent_state,
    switcher=switcher, closing_utm_final=closing_utm_final, last_channel=last_channel,
    repeat_start=(rep1_mask | rep2_mask)))

# ---------------------------------------------------------------- web_events
rows = []
def web(mask, pseudo, sess_suffix, name, ts, utm_col="utm"):
    d = users[mask]
    ps = d[pseudo] if isinstance(pseudo, str) else pseudo[mask]
    rows.append(pd.DataFrame(dict(
        user_pseudo_id=ps,
        session_id=[f"{p}_{s}" for p, s in zip(ps, np.full(mask.sum(), sess_suffix))],
        event_name=name,
        event_timestamp=d[ts] if isinstance(ts, str) else ts[mask],
        page_location=np.where(d[utm_col].isna(), "https://www.example-hearingcare.com/hearing-test",
            "https://www.example-hearingcare.com/hearing-test?utm_campaign=" + d[utm_col].fillna("")),
        utm_campaign=d[utm_col], device_category=d["device"], country=d["market"],
        consent_state=d["consent_state"], _true_user=d["user_id"])))

all_mask = np.ones(N, dtype=bool)
web(all_mask, "pseudo1", "s1", "page_view", "start_ts")
web(all_mask, "pseudo1", "s1", "hearing_test_start", "start_ts")
# completion happens on 2nd pseudo_id for fragmented users (measurement artefact)
comp_pseudo = np.where(users["frag"], users["pseudo2"], users["pseudo1"]).astype(object)
# s2 (completion session) carries closing_utm_final, not the starting utm --
# this is the multi-touch switch (task M4b.1). id_bridge.acquisition_channel
# below reads `channels`/`link["channel"]` directly, never this column, so
# first-touch stays first-touch regardless of what happens here.
web(users["completed"].to_numpy(), comp_pseudo, "s2", "hearing_test_complete", "complete_ts",
    utm_col="closing_utm_final")
web(users["completed"].to_numpy(), comp_pseudo, "s2", "result_screen_view",
    (users["complete_ts"] + pd.Timedelta(minutes=1)), utm_col="closing_utm_final")
web(users["downloaded"].to_numpy(), comp_pseudo, "s2", "app_store_redirect",
    (users["complete_ts"] + pd.Timedelta(minutes=3)), utm_col="closing_utm_final")
# repeat test-start sessions (task M4b.2) -- same pseudo_id as the first
# start; utm inherited from closing_utm_final (see comment above its
# definition). These are pure extra rows: MIN(event_timestamp) in the
# medallion layer means they cannot move first_test_start_ts or any flag.
web(rep1_mask, "pseudo1", "s3", "hearing_test_start", rep1_ts, utm_col="closing_utm_final")
web(rep2_mask, "pseudo1", "s4", "hearing_test_start", rep2_ts, utm_col="closing_utm_final")
web_events = pd.concat(rows, ignore_index=True)

# ---------------------------------------------------------------- app_events
app_rows = []
dl_users = users[users["downloaded"]]
ver = RNG.choice(["6.2.1", "6.3.0", "6.3.1"], len(dl_users), p=[0.2, 0.45, 0.35])

def app(df, name, ts):
    app_rows.append(pd.DataFrame(dict(
        hashed_device_id=df["device_id"], platform=df["platform"], event_name=name,
        event_timestamp=ts, app_version=ver[:len(df)] if len(df) == len(dl_users)
                          else RNG.choice(["6.3.0", "6.3.1"], len(df)),
        _true_user=df["user_id"])))

app(dl_users, "app_open", dl_users["dl_ts"])
app(users[users["paired"]], "hearing_aid_paired", users.loc[users["paired"], "pair_ts"])
sup = users[users["uses_support"]]
app(sup, "remote_support_session", sup["pair_ts"] + pd.to_timedelta(
    RNG.integers(1, 15, len(sup)), unit="D"))
# recurring app_opens: retained users open regularly; d30 users open inside day 28-33
for k in (3, 7, 14, 21):
    m = users["paired"] & (RNG.random(N) < np.clip(0.3 + 0.6 * users["engagement"], 0, 0.95))
    dfk = users[m]
    tsk = dfk["dl_ts"] + pd.Timedelta(days=k)
    keep = tsk <= HORIZON
    app(dfk[keep], "app_open", tsk[keep])
d30_users = users[users["d30"]]
app(d30_users, "app_open",
    d30_users["dl_ts"] + pd.to_timedelta(RNG.integers(28, 34, len(d30_users)), unit="D"))
app_events = pd.concat(app_rows, ignore_index=True)
app_events = app_events[app_events["event_timestamp"] <= HORIZON]

# ---------------------------------------------------------------- id_bridge (extended mapping, task 5.1)
link = users[users["signin"]]
id_bridge = pd.DataFrame(dict(
    hashed_id=link["hashed_id"], market=link["market"],
    opt_in_flag=True, acquisition_channel=link["channel"],   # CRM first-touch memory
    web_pseudo_id=np.where(link["frag"], link["pseudo2"], link["pseudo1"]),
    app_device_id=link["device_id"],
    linked_at=link["dl_ts"] + pd.Timedelta(hours=2)))

# ---------------------------------------------------------------- write
OUT.mkdir(exist_ok=True)
web_events.drop(columns="_true_user").to_parquet(OUT / "web_events.parquet", index=False)
app_events.drop(columns="_true_user").to_parquet(OUT / "app_events.parquet", index=False)
id_bridge.to_parquet(OUT / "id_bridge.parquet", index=False)
users.to_parquet(OUT / "_ground_truth.parquet", index=False)  # validation only, never shipped

# ---------------------------------------------------------------- validation report
print("=== ROW COUNTS ===")
print(f"web_events: {len(web_events):,}  app_events: {len(app_events):,}  id_bridge: {len(id_bridge):,}")

print("\n=== FUNNEL vs REFERENCE (ground truth) ===")
steps = [("Started test", N, 100_000),
         ("Completed test", completed.sum(), 42_000),
         ("Downloaded app", downloaded.sum(), 23_100),
         ("Paired device", paired.sum(), 14_091)]
prev = None
for name, got, ref in steps:
    conv = f"  step conv {got/prev:.0%}" if prev else ""
    print(f"{name:16s} {got:>7,}  (ref {ref:,}){conv}")
    prev = got
mature = users[users["paired"] & (users["dl_ts"] + pd.Timedelta(days=34) <= HORIZON)]
print(f"{'D30 active':16s} {users['d30'].sum():>7,}  (ref 10,991)  "
      f"D30 rate on mature cohort: {mature['d30'].mean():.0%} (ref 78%)")

print("\n=== PLANTED PATTERNS ===")
print("Completion by channel:")
print(users.groupby("channel")["completed"].mean().round(3).to_string())
print("\nCompletion by device:")
print(users.groupby("device")["completed"].mean().round(3).to_string())
print("\nPairing|download by platform within market:")
print(users[users["downloaded"]].groupby(["market", "platform"])["paired"]
      .mean().round(3).unstack().to_string())
print("\nPairing|download by channel (volume vs quality):")
g = users[users["downloaded"]].groupby("channel").agg(
    downloads=("downloaded", "sum"), pair_rate=("paired", "mean")).round(3)
print(g.to_string())
print("\nD30 by remote-support usage (correlation trap):")
print(users[users["paired"]].groupby("uses_support")["d30"].mean().round(3).to_string())
print(f"\nLinkable share of downloaders (consent∩sign-in): "
      f"{users.loc[users['downloaded'],'signin'].mean():.0%}")
print("Linkable share by market:")
print(users[users["downloaded"]].groupby("market")["signin"].mean().round(3).to_string())

# ---------------------------------------------------------------- funnel invariant check (task M4b.3)
print("\n=== FUNNEL INVARIANT CHECK (recomputed from event-level data) ===")
print("Proves multi-touch relabeling (M4b.1) and repeat test starts (M4b.2) do")
print("NOT change user-grain funnel counts (funnel logic is MIN/first-event):")
ev_start_users = web_events.loc[web_events["event_name"] == "hearing_test_start", "_true_user"].nunique()
ev_complete_users = web_events.loc[web_events["event_name"] == "hearing_test_complete", "_true_user"].nunique()
ev_redirect_users = web_events.loc[web_events["event_name"] == "app_store_redirect", "_true_user"].nunique()
assert ev_start_users == N, f"INVARIANT VIOLATED: start users {ev_start_users} != {N}"
assert ev_complete_users == completed.sum(), \
    f"INVARIANT VIOLATED: complete users {ev_complete_users} != {completed.sum()}"
assert ev_redirect_users == downloaded.sum(), \
    f"INVARIANT VIOLATED: redirect(download) users {ev_redirect_users} != {downloaded.sum()}"
inv_rows = [
    ("hearing_test_start", ev_start_users, N),
    ("hearing_test_complete", ev_complete_users, completed.sum()),
    ("app_store_redirect", ev_redirect_users, downloaded.sum()),
    ("hearing_aid_paired", paired.sum(), paired.sum()),
    ("active_d30", d30.sum(), d30.sum()),
]
for name, recomputed, flag_based in inv_rows:
    print(f"{name:24s} recomputed(events)={recomputed:>7,}  flag-based={flag_based:>7,}  "
          f"match={recomputed == flag_based}")
print("(hearing_aid_paired/active_d30 are app-side and untouched by this module --")
print(" shown for completeness, trivially equal.)")

# ---------------------------------------------------------------- switcher / multi-touch stats (task M4b.1)
print("\n=== MULTI-TOUCH (SWITCHER) STATS ===")
n_dl = downloaded.sum()
n_nd = (completed & ~downloaded).sum()
print(f"Switchers among downloaders:              {switcher[downloaded].sum():>7,} / {n_dl:,} "
      f"({switcher[downloaded].sum() / n_dl:.1%})")
print(f"Switchers among completed-not-downloaded: {switcher[completed & ~downloaded].sum():>7,} / {n_nd:,} "
      f"({(switcher[completed & ~downloaded].sum() / n_nd if n_nd else 0):.1%})")
sw = users[users["switcher"]]
disc_share = sw["channel"].isin(["paid_social_meta", "paid_social_tiktok"]).mean()
print(f"Share of switchers originating from discovery channels (meta+tiktok): {disc_share:.1%}")
print("Switcher origin channel mix:")
print(sw["channel"].value_counts(normalize=True).round(3).to_string())
print("Switcher closing channel mix:")
print(sw["last_channel"].value_counts(normalize=True).round(3).to_string())

# ---------------------------------------------------------------- repeat test-start stats (task M4b.2)
print("\n=== REPEAT TEST-START STATS ===")
n_repeat_users = users["repeat_start"].sum()
n_extra_events = int(rep1_mask.sum() + rep2_mask.sum())
print(f"Users with >=1 extra hearing_test_start: {n_repeat_users:,} ({n_repeat_users / N:.1%} of all users)")
print(f"Total extra hearing_test_start events emitted: {n_extra_events:,}")
print("How many users started the test more than once, by market:")
print(users[users["repeat_start"]].groupby("market").size().to_string())
