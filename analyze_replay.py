"""
Network-frame stats extraction from rrrocket --network-parse output.
All functions receive the parsed replay dict and return stats dicts keyed
by player name.  Field names match what the detail-view UI expects.
"""

# ── RL field constants ────────────────────────────────────────────────────────
_SSL_SPEED  = 2200.0   # supersonic threshold (uu/s)
_BOOST_SPD  = 1400.0   # boost-speed threshold
_SLOW_SPD   = 700.0    # slow-speed threshold
_GROUND_H   = 100.0    # "on ground" height (uu)
_LOW_AIR_H  = 600.0    # "low air" height (uu)
_FIELD_HALF = 0.0      # midfield Y
_FIELD_THIRD = 1707.0  # one third of field in Y
_MAX_SPEED  = 2300.0   # normalise avg_speed_pct


# ── private helpers ───────────────────────────────────────────────────────────

def _speed(rb: dict) -> float:
    if not rb:
        return 0.0
    lv = rb.get("linear_velocity") or {}
    x = lv.get("x") or 0.0
    y = lv.get("y") or 0.0
    z = lv.get("z") or 0.0
    return (x*x + y*y + z*z) ** 0.5


def _loc(rb: dict):
    if not rb:
        return None
    loc = rb.get("location") or {}
    if not loc:
        return None
    return (loc.get("x") or 0.0, loc.get("y") or 0.0, loc.get("z") or 0.0)


# ── public API ────────────────────────────────────────────────────────────────

def build_actor_maps(replay):
    """
    Scan network frames to build actor linkage maps.

    Returns (obj_ids, pri_name, car_to_pri, boost_to_car):
      obj_ids      – internal lookup dict (first return value, ignored by callers)
      pri_name     – {pri_actor_id: player_name}
      car_to_pri   – {car_actor_id: pri_actor_id}  (all segments, complete)
      boost_to_car – {boost_actor_id: car_actor_id} (all segments, complete)
    """
    objects = replay.get("objects") or []
    frames  = (replay.get("network_frames") or {}).get("frames") or []

    OID_PRI_NAME = None
    OID_PRI_LINK = None   # Engine.Pawn:PlayerReplicationInfo  (car→PRI)
    OID_COMP_VEH = None   # TAGame.CarComponent_TA:Vehicle      (component→car)
    OID_RB_STATE = None
    OID_BOOST    = None   # TAGame.CarComponent_Boost_TA:ReplicatedBoost
    OID_PICKUP   = None   # TAGame.VehiclePickup_TA:NewReplicatedPickupData
    OID_HIT_TEAM = None
    OID_DEMOLISH = None   # TAGame.Car_TA:ReplicatedDemolishExtended

    CAR_ARCHS    = set()
    PRI_ARCHS    = set()
    BOOST_ARCHS  = set()
    BALL_ARCHS   = set()

    for i, name in enumerate(objects):
        if ":PlayerName" in name:
            OID_PRI_NAME = i
        elif name == "Engine.Pawn:PlayerReplicationInfo":
            OID_PRI_LINK = i
        elif name == "TAGame.CarComponent_TA:Vehicle":
            OID_COMP_VEH = i
        elif ":ReplicatedRBState" in name:
            OID_RB_STATE = i
        elif name == "TAGame.CarComponent_Boost_TA:ReplicatedBoost":
            OID_BOOST = i
        elif ":NewReplicatedPickupData" in name:
            OID_PICKUP = i
        elif ":HitTeamNum" in name:
            OID_HIT_TEAM = i
        elif "ReplicatedDemolishExtended" in name:
            OID_DEMOLISH = i

        if "Archetypes.Car." in name:
            CAR_ARCHS.add(i)
        elif "Default__PRI_TA" in name or name.endswith("PRI_TA"):
            PRI_ARCHS.add(i)
        elif "CarComponent_Boost" in name:
            BOOST_ARCHS.add(i)
        elif "Archetypes.Ball." in name:
            BALL_ARCHS.add(i)

    obj_ids = {
        "pri_name": OID_PRI_NAME, "pri_link": OID_PRI_LINK,
        "comp_veh": OID_COMP_VEH, "rb_state": OID_RB_STATE,
        "boost": OID_BOOST, "pickup": OID_PICKUP,
        "hit_team": OID_HIT_TEAM, "demolish": OID_DEMOLISH,
    }

    actor_class: dict[int, str] = {}
    pri_name:    dict[int, str] = {}
    car_to_pri:  dict[int, int] = {}
    boost_to_car: dict[int, int] = {}   # boost_actor_id → car_actor_id (complete)

    for frame in frames:
        for actor in (frame.get("new_actors") or []):
            aid = actor.get("actor_id")
            oid = actor.get("object_id")
            if aid is None or oid is None:
                continue
            if oid in CAR_ARCHS:
                actor_class[aid] = "car"
            elif oid in PRI_ARCHS:
                actor_class[aid] = "pri"
            elif oid in BOOST_ARCHS:
                actor_class[aid] = "boost"
            elif oid in BALL_ARCHS:
                actor_class[aid] = "ball"

        for upd in (frame.get("updated_actors") or []):
            aid  = upd.get("actor_id")
            oid  = upd.get("object_id")
            attr = upd.get("attribute") or {}
            if aid is None or oid is None:
                continue
            if oid == OID_PRI_NAME:
                v = attr.get("String") or attr.get("FString") or ""
                if v:
                    pri_name[aid] = v
            elif oid == OID_PRI_LINK:
                aa = attr.get("ActiveActor") or {}
                if aa.get("active"):
                    car_to_pri[aid] = aa["actor"]
            elif oid == OID_COMP_VEH and actor_class.get(aid) == "boost":
                aa = attr.get("ActiveActor") or {}
                if aa.get("active"):
                    boost_to_car[aid] = aa["actor"]

    # Return boost_to_car (complete — all segments) instead of the lossy reversal
    return obj_ids, pri_name, car_to_pri, boost_to_car


def extract_demos(frames, pri_name, car_to_pri):
    """Returns list of {time, attacker, victim, self_demo}."""
    demos = []
    seen: set = set()
    for frame in frames:
        t = frame.get("time", 0)
        for upd in (frame.get("updated_actors") or []):
            attr = upd.get("attribute") or {}
            de = attr.get("DemolishExtended")
            if de is None:
                continue
            key = (round(t, 2), upd.get("actor_id"))
            if key in seen:
                continue
            seen.add(key)
            att_pri_id = (de.get("attacker_pri") or {}).get("actor", -1)
            vic_car_id = (de.get("victim")       or {}).get("actor", -1)
            self_demo  = bool(de.get("self_demolish"))
            # primary: attacker_pri → pri_name
            att_name = pri_name.get(att_pri_id, "")
            if not att_name:
                # fallback: attacker car actor → car_to_pri → pri_name
                att_car_id = (de.get("attacker") or {}).get("actor", -1)
                att_name   = pri_name.get(car_to_pri.get(att_car_id, -1), "")
            vic_pri  = car_to_pri.get(vic_car_id, -1)
            vic_name = pri_name.get(vic_pri, "")
            if not att_name or not vic_name:
                continue
            demos.append({
                "time":      t,
                "attacker":  att_name,
                "victim":    vic_name,
                "self_demo": self_demo,
            })
    return demos


def extract_boost_stats(frames, car_to_pri, pri_name, boost_to_car, duration):
    """
    Returns {player_name: {bpm, avg_boost, time_empty_pct, time_full_pct,
                           amount_collected, boost_used,
                           big_pads, small_pads, boost_sonic_used, overfill}}.

    boost_to_car must be the COMPLETE map {boost_actor_id: car_actor_id} returned
    by build_actor_maps (all game segments, not a lossy reversal).
    """
    OID_RB = _find_rb_oid(frames)
    car_speed: dict[int, float] = {}

    # boost_actor_id → [(time, raw_amount_0_255, car_speed_at_time)]
    boost_samples: dict[int, list] = {}

    for frame in frames:
        t = frame.get("time", 0)
        for upd in (frame.get("updated_actors") or []):
            aid  = upd.get("actor_id")
            oid  = upd.get("object_id")
            attr = upd.get("attribute") or {}
            if oid == OID_RB and aid in car_to_pri:
                rb = attr.get("RigidBody") or {}
                car_speed[aid] = _speed(rb)
            rb_data = attr.get("ReplicatedBoost")
            if rb_data is not None and aid in boost_to_car:
                car_id = boost_to_car[aid]
                amount = rb_data.get("boost_amount", 0)
                if aid not in boost_samples:
                    boost_samples[aid] = []
                boost_samples[aid].append((t, amount, car_speed.get(car_id, 0)))

    # Accumulate per player across ALL boost actors / game segments
    accum: dict[str, dict] = {}

    for boost_id, samples in boost_samples.items():
        car_id = boost_to_car.get(boost_id)
        if car_id is None:
            continue
        pri_id = car_to_pri.get(car_id)
        if pri_id is None:
            continue
        name = pri_name.get(pri_id)
        if not name or len(samples) < 2:
            continue

        samples.sort(key=lambda s: s[0])
        seg_total  = 0.0
        seg_empty  = 0.0
        seg_full   = 0.0
        seg_used   = 0.0
        seg_sonic  = 0.0
        seg_coll   = 0.0
        seg_big    = 0
        seg_small  = 0
        seg_over   = 0.0
        seg_bsum   = 0.0

        for i in range(1, len(samples)):
            t0, a0, spd0 = samples[i-1]
            t1, a1, _    = samples[i]
            dt = t1 - t0
            if dt <= 0:
                continue
            seg_total += dt
            a0n = a0 / 2.55
            seg_bsum  += a0n * dt
            if a0 == 0:
                seg_empty += dt
            if a0 >= 254:
                seg_full  += dt
            delta = a1 - a0
            if delta < 0:
                used = -delta / 2.55
                seg_used  += used
                if spd0 >= _SSL_SPEED:
                    seg_sonic += used
            elif delta > 0:
                gain = delta / 2.55
                seg_coll += gain
                if gain > 12:
                    # big pad fills to 100%; boost already held is wasted
                    seg_big  += 1
                    seg_over += a0n
                else:
                    seg_small += 1
                    seg_over  += max(0.0, a0n + 12 - 100)

        if seg_total <= 0:
            continue

        if name not in accum:
            accum[name] = dict(total=0.0, empty=0.0, full=0.0, used=0.0,
                               sonic=0.0, coll=0.0, big=0, small=0,
                               over=0.0, bsum=0.0)
        a = accum[name]
        a["total"] += seg_total
        a["empty"] += seg_empty
        a["full"]  += seg_full
        a["used"]  += seg_used
        a["sonic"] += seg_sonic
        a["coll"]  += seg_coll
        a["big"]   += seg_big
        a["small"] += seg_small
        a["over"]  += seg_over
        a["bsum"]  += seg_bsum

    results: dict = {}
    for name, a in accum.items():
        tt = a["total"]
        if tt <= 0:
            continue
        dur = duration if duration and duration > 0 else tt
        results[name] = {
            "bpm":              round(a["coll"] / (dur / 60), 1) if dur > 0 else 0,
            "avg_boost":        round(a["bsum"] / tt, 1),
            "time_empty_pct":   round(a["empty"] / tt * 100, 1),
            "time_full_pct":    round(a["full"]  / tt * 100, 1),
            "amount_collected": round(a["coll"], 1),
            "boost_used":       round(a["used"], 1),
            "big_pads":         a["big"],
            "small_pads":       a["small"],
            "boost_sonic_used": round(a["sonic"], 1),
            "overfill":         round(a["over"], 1),
        }
    return results


def extract_position_stats(frames, objects, car_to_pri, pri_name, p_teams):
    """
    Returns {player_name: {def_pct, mid_pct, atk_pct, def_half_pct, off_half_pct}}.
    Accumulates across all car segments (handles actor ID reuse after goals).
    """
    OID_RB = _find_rb_oid(frames)

    # car_id → [(time, y)] — collected across all frames
    car_pos: dict[int, list] = {}

    for frame in frames:
        t = frame.get("time", 0)
        for upd in (frame.get("updated_actors") or []):
            aid  = upd.get("actor_id")
            oid  = upd.get("object_id")
            attr = upd.get("attribute") or {}
            if oid != OID_RB or aid not in car_to_pri:
                continue
            rb  = attr.get("RigidBody") or {}
            loc = _loc(rb)
            if loc is None:
                continue
            if aid not in car_pos:
                car_pos[aid] = []
            car_pos[aid].append((t, loc[1]))

    # Accumulate per player across all car actor IDs
    accum: dict[str, dict] = {}

    for car_id, samples in car_pos.items():
        pri_id = car_to_pri.get(car_id)
        if pri_id is None:
            continue
        name = pri_name.get(pri_id)
        if not name:
            continue
        team = p_teams.get(name, -1)
        samples.sort(key=lambda s: s[0])
        if len(samples) < 2:
            continue

        seg_total = 0.0
        seg_off   = 0.0
        seg_def   = 0.0
        seg_offh  = 0.0
        seg_defh  = 0.0

        for i in range(1, len(samples)):
            t0, y0 = samples[i-1]
            t1, _  = samples[i]
            dt = t1 - t0
            if dt <= 0:
                continue
            seg_total += dt
            off_y = y0 if team == 0 else -y0
            def_y = -y0 if team == 0 else y0

            if off_y > _FIELD_THIRD:
                seg_off += dt
            elif def_y > _FIELD_THIRD:
                seg_def += dt
            if off_y > _FIELD_HALF:
                seg_offh += dt
            else:
                seg_defh += dt

        if seg_total <= 0:
            continue

        if name not in accum:
            accum[name] = dict(total=0.0, off=0.0, defth=0.0, offh=0.0, defh=0.0)
        a = accum[name]
        a["total"] += seg_total
        a["off"]   += seg_off
        a["defth"] += seg_def
        a["offh"]  += seg_offh
        a["defh"]  += seg_defh

    results: dict = {}
    for name, a in accum.items():
        tt = a["total"]
        if tt <= 0:
            continue
        neutral = max(0.0, tt - a["off"] - a["defth"])
        results[name] = {
            "def_pct":      round(a["defth"] / tt * 100, 1),
            "mid_pct":      round(neutral    / tt * 100, 1),
            "atk_pct":      round(a["off"]   / tt * 100, 1),
            "def_half_pct": round(a["defh"]  / tt * 100, 1),
            "off_half_pct": round(a["offh"]  / tt * 100, 1),
        }
    return results


def extract_movement_stats(frames, objects, car_to_pri, pri_name, car_to_boost=None):
    """
    Returns {player_name: {avg_speed_pct, total_dist,
                           time_slow, time_boost, time_supersonic,
                           time_ground, time_low_air, time_high_air,
                           slide_total, slide_count, slide_avg}}.
    Accumulates across all car segments (handles actor ID reuse after goals).
    """
    OID_RB = _find_rb_oid(frames)

    OID_HB = None
    for i, name in enumerate(objects):
        if "bReplicatedHandbrake" in name:
            OID_HB = i
            break

    car_rb:     dict[int, list] = {}   # car_id → [(time, speed, z)]
    hb_events:  dict[int, list] = {}   # car_id → [(time, bool)]

    for frame in frames:
        t = frame.get("time", 0)
        for upd in (frame.get("updated_actors") or []):
            aid  = upd.get("actor_id")
            oid  = upd.get("object_id")
            attr = upd.get("attribute") or {}
            if aid not in car_to_pri:
                continue
            if oid == OID_RB:
                rb  = attr.get("RigidBody") or {}
                spd = _speed(rb)
                loc = _loc(rb)
                z   = loc[2] if loc else 0.0
                if aid not in car_rb:
                    car_rb[aid] = []
                car_rb[aid].append((t, spd, z))
            if oid == OID_HB:
                val = attr.get("Boolean")
                if val is not None:
                    if aid not in hb_events:
                        hb_events[aid] = []
                    hb_events[aid].append((t, bool(val)))

    # Accumulate per player across all car actor IDs
    accum: dict[str, dict] = {}

    for car_id, samples in car_rb.items():
        pri_id = car_to_pri.get(car_id)
        if pri_id is None:
            continue
        name = pri_name.get(pri_id)
        if not name or len(samples) < 2:
            continue
        samples.sort(key=lambda s: s[0])

        seg_total    = 0.0
        seg_ssum     = 0.0
        seg_dist     = 0.0
        seg_ssl      = 0.0
        seg_bspd     = 0.0
        seg_slow     = 0.0
        seg_ground   = 0.0
        seg_low      = 0.0
        seg_high     = 0.0

        for i in range(1, len(samples)):
            t0, spd0, z0 = samples[i-1]
            t1, _,    _  = samples[i]
            dt = t1 - t0
            if dt <= 0:
                continue
            seg_total += dt
            seg_ssum  += spd0 * dt
            seg_dist  += spd0 * dt

            if spd0 >= _SSL_SPEED:
                seg_ssl  += dt
            elif spd0 >= _BOOST_SPD:
                seg_bspd += dt
            elif spd0 < _SLOW_SPD:
                seg_slow += dt

            if z0 <= _GROUND_H:
                seg_ground += dt
            elif z0 <= _LOW_AIR_H:
                seg_low    += dt
            else:
                seg_high   += dt

        # Powerslide for this car segment
        seg_slide_total = 0.0
        seg_slide_count = 0
        evts = sorted(hb_events.get(car_id, []), key=lambda e: e[0])
        slide_start: float | None = None
        for t_ev, pressing in evts:
            if pressing and slide_start is None:
                slide_start = t_ev
                seg_slide_count += 1
            elif not pressing and slide_start is not None:
                seg_slide_total += t_ev - slide_start
                slide_start = None
        if slide_start is not None and samples:
            seg_slide_total += samples[-1][0] - slide_start

        if seg_total <= 0:
            continue

        if name not in accum:
            accum[name] = dict(total=0.0, ssum=0.0, dist=0.0,
                               ssl=0.0, bspd=0.0, slow=0.0,
                               ground=0.0, low=0.0, high=0.0,
                               slide_total=0.0, slide_count=0)
        a = accum[name]
        a["total"]       += seg_total
        a["ssum"]        += seg_ssum
        a["dist"]        += seg_dist
        a["ssl"]         += seg_ssl
        a["bspd"]        += seg_bspd
        a["slow"]        += seg_slow
        a["ground"]      += seg_ground
        a["low"]         += seg_low
        a["high"]        += seg_high
        a["slide_total"] += seg_slide_total
        a["slide_count"] += seg_slide_count

    results: dict = {}
    for name, a in accum.items():
        tt = a["total"]
        if tt <= 0:
            continue
        sc = a["slide_count"]
        st = a["slide_total"]
        results[name] = {
            "avg_speed_pct":   round(a["ssum"] / tt / _MAX_SPEED * 100, 2),
            "total_dist":      round(a["dist"]),
            "time_slow":       round(a["slow"],   2),
            "time_boost":      round(a["bspd"],   2),
            "time_supersonic": round(a["ssl"],    2),
            "time_ground":     round(a["ground"], 2),
            "time_low_air":    round(a["low"],    2),
            "time_high_air":   round(a["high"],   2),
            "slide_total":     round(st,          2),
            "slide_count":     sc,
            "slide_avg":       round(st / sc, 2) if sc else 0.0,
        }
    return results


def extract_ball_stats(frames, objects):
    """
    Returns {blue_side_pct, orange_side_pct,
             blue_possession_pct, orange_possession_pct}.
    """
    OID_RB = _find_rb_oid(frames)
    OID_HIT_TEAM = None
    for i, name in enumerate(objects):
        if ":HitTeamNum" in name:
            OID_HIT_TEAM = i
            break

    ball_actor: int | None = None
    ball_samples: list = []   # [(time, y)]
    hit_team_samples: list = []  # [(time, team_num)]  0=blue, 1=orange
    last_hit_team: int = -1

    for frame in frames:
        t = frame.get("time", 0)
        for actor in (frame.get("new_actors") or []):
            oid = actor.get("object_id")
            if oid is not None and oid < len(objects) and "Ball" in objects[oid]:
                ball_actor = actor.get("actor_id")
        for upd in (frame.get("updated_actors") or []):
            aid  = upd.get("actor_id")
            oid  = upd.get("object_id")
            attr = upd.get("attribute") or {}
            if aid != ball_actor:
                continue
            if oid == OID_RB:
                rb  = attr.get("RigidBody") or {}
                loc = _loc(rb)
                if loc is not None:
                    ball_samples.append((t, loc[1]))
            if oid == OID_HIT_TEAM:
                raw = attr.get("Byte")
                if isinstance(raw, (int, float)):
                    last_hit_team = int(raw)
                elif isinstance(raw, dict):
                    v = str(raw.get("value", ""))
                    last_hit_team = 0 if "0" in v else (1 if "1" in v else last_hit_team)
                hit_team_samples.append((t, last_hit_team))

    if len(ball_samples) < 2:
        return {}

    total_time     = 0.0
    t_blue_side    = 0.0
    t_orange_side  = 0.0
    t_blue_poss    = 0.0
    t_orange_poss  = 0.0

    ht_idx = 0
    cur_team = -1

    for i in range(1, len(ball_samples)):
        t0, y0 = ball_samples[i-1]
        t1, _  = ball_samples[i]
        dt = t1 - t0
        if dt <= 0:
            continue
        total_time += dt

        if y0 < 0:
            t_blue_side += dt
        else:
            t_orange_side += dt

        while ht_idx < len(hit_team_samples) and hit_team_samples[ht_idx][0] <= t0:
            cur_team = hit_team_samples[ht_idx][1]
            ht_idx += 1

        if cur_team == 0:
            t_blue_poss += dt
        elif cur_team == 1:
            t_orange_poss += dt

    if total_time <= 0:
        return {}

    tagged = t_blue_poss + t_orange_poss
    if tagged > 0:
        bposs = t_blue_poss   / tagged * 100
        oposs = t_orange_poss / tagged * 100
    else:
        bposs = oposs = 50.0

    return {
        "blue_side_pct":         round(t_blue_side   / total_time * 100, 1),
        "orange_side_pct":       round(t_orange_side  / total_time * 100, 1),
        "blue_possession_pct":   round(bposs, 1),
        "orange_possession_pct": round(oposs, 1),
    }


# ── private ───────────────────────────────────────────────────────────────────

def _find_rb_oid(frames):
    """Find the object_id used for RigidBody (ReplicatedRBState) updates."""
    for frame in frames:
        for upd in (frame.get("updated_actors") or []):
            if (upd.get("attribute") or {}).get("RigidBody") is not None:
                return upd["object_id"]
    return None
