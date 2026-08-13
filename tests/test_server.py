"""Ground-station tests. No pytest, no fixtures, no network:

    python3 tests/test_server.py           # quiet
    python3 tests/test_server.py -v        # every check

Deliberately one runnable file with plain asserts, matching
`ligmax-pi/tests/test_autopilot.py`: the people who run this are running it on a
laptop in a tent, minutes before a start, and "pip install pytest" is not a thing
that happens there. Flask's own test client does all the work, so nothing here
binds a port or needs the real vessel.

What this covers is the part of the server that is *rules* rather than plumbing:
the command allow-list and its validation, and the trip-recording store, whose
resume protocol has exactly the kind of off-by-one that unit tests exist for.
"""

from __future__ import annotations

import gzip
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TMP = tempfile.mkdtemp(prefix="ligmax-test-")
os.environ["LIGMAX_TRIP_STORE"] = os.path.join(_TMP, "trips")
os.environ["LIGMAX_TUNING_STORE"] = os.path.join(_TMP, "tuning.json")
os.environ["LIGMAX_LIGHT_EFFECTS_STORE"] = os.path.join(_TMP, "effects.json")
os.environ["LIGMAX_BOAT_KEY"] = "boat-secret"
os.environ["LIGMAX_ADMIN_KEY"] = "admin-secret"
os.environ["LIGMAX_COOKIE_SECRET"] = "cookie-secret"
os.environ["LIGMAX_RTK_ENABLED"] = "0"

from ligmax_gui import trips  # noqa: E402
from ligmax_gui.server import (  # noqa: E402
    COMMAND_SPECS,
    MAX_SPEED_LIMIT_MS,
    create_app,
)

VERBOSE = "-v" in sys.argv
FAILURES: list[str] = []


def check(condition: object, message: str) -> None:
    if condition:
        if VERBOSE:
            print(f"  ok    {message}")
    else:
        print(f"  FAIL  {message}")
        FAILURES.append(message)


def section(title: str) -> None:
    print(f"\n=== {title}")


# A real recording is already gzipped, so what goes on the wire does not
# compress. Random bytes, seeded, so the chunking is actually exercised and the
# same file is used on every run.
random.seed(7)
_RAW = bytes(random.getrandbits(8) for _ in range(3_500_000))
PAYLOAD = gzip.compress(_RAW, 1)

BOAT = {"Authorization": "Bearer boat-secret"}


def _client():
    return create_app().test_client()


def _admin_client():
    client = _client()
    client.get("/?key=admin-secret")  # sets the admin cookie
    return client


# --------------------------------------------------------------- commands


def test_command_specs():
    section("the command allow-list")
    client = _admin_client()

    # The autonomy node's own commands. Their absence is what the dashboard could
    # not send (next_step.md §2.1, §2.6).
    for name in ("set_speed_limit", "alternation", "forget_object"):
        check(name in COMMAND_SPECS, f"{name} is on the allow-list")

    check(
        COMMAND_SPECS["set_speed_limit"]["args"] == {"value": "float"},
        "set_speed_limit takes one speed in m/s - it is the only speed control",
    )
    check(
        COMMAND_SPECS["forget_object"]["args"] == {"id": "float"},
        "forget_object takes the track id the chart drew",
    )

    ok = client.post("/api/command", json={"name": "alternation", "args": {"on": True}})
    check(ok.status_code == 200, f"alternation is accepted ({ok.status_code})")

    ok = client.post("/api/command", json={"name": "forget_object", "args": {"id": 7}})
    check(ok.status_code == 200, f"forget_object with a real id is accepted ({ok.status_code})")

    # A track id is an integer the frontend read off a marker. Anything else is a
    # bug on this side, and it is cheaper to refuse here than to watch the
    # command sit at "sent" and come back "no object 7.5".
    for bad in (-1, float("nan"), float("inf")):
        r = client.post("/api/command", json={"name": "forget_object", "args": {"id": bad}})
        check(r.status_code == 400, f"forget_object refuses id={bad!r} ({r.status_code})")
    r = client.post("/api/command", json={"name": "forget_object", "args": {"id": "abc"}})
    check(r.status_code == 400, "forget_object refuses a non-numeric id")
    r = client.post("/api/command", json={"name": "forget_object", "args": {}})
    check(r.status_code == 400, "forget_object refuses a missing id")

    r = client.post("/api/command", json={"name": "forget_object", "args": {"id": 12.0}})
    check(r.status_code == 200, "a whole-number float is fine - JSON has no ints")

    # Ride height, and the property worth pinning: EVERY declared arg is
    # required, there is no optional-arg mechanism in this validator. A spec of
    # {"pwm", "release"} therefore cannot express "move" or "let go" - both
    # forms 400 - which is why releasing is its own command. Sending each in its
    # natural shape is the whole test.
    r = client.post("/api/command", json={"name": "set_ride_height", "args": {"pwm": 1600}})
    check(r.status_code == 200, f"set_ride_height takes a pwm on its own ({r.status_code})")
    r = client.post("/api/command", json={"name": "release_ride_height", "args": {}})
    check(r.status_code == 200, f"release_ride_height needs no args ({r.status_code})")
    check(
        "release" not in COMMAND_SPECS["set_ride_height"]["args"],
        "releasing is its own command, not a flag on this one - as a flag it "
        "and 'pwm' would both be required, and neither form could be sent",
    )
    r = client.post("/api/command", json={"name": "set_ride_height", "args": {}})
    check(r.status_code == 400, "set_ride_height without a pwm is refused")

    # The Pixhawk's safety switch. Two commands rather than one carrying a
    # boolean, because "safety on" INHIBITS the outputs and "safety off" makes
    # them live - an audit line reading `set_safety enabled=false` is read wrong
    # by exactly the person reading it in a hurry.
    for name in ("safety_on", "safety_off"):
        r = client.post("/api/command", json={"name": name, "args": {}})
        check(r.status_code == 200, f"{name} needs no args ({r.status_code})")
    check(
        COMMAND_SPECS["safety_off"].get("danger") is True,
        "safety_off is the danger one: it is the only command here that makes "
        "the thrusters capable of turning with no other action",
    )
    check(
        not COMMAND_SPECS["safety_on"].get("danger"),
        "safety_on is the safe direction and must not shout like an E-stop",
    )

    # The compass swing. Degrees true; the value is wrapped rather than refused,
    # because 361 is a typo with an obvious reading, but NaN and inf are not
    # headings at all and reach ArduPilot's magnetic model as numbers it has no
    # answer for.
    r = client.post("/api/command", json={"name": "compass_cal", "args": {"heading": 137.5}})
    check(r.status_code == 200, f"compass_cal takes a heading ({r.status_code})")
    r = client.post("/api/command", json={"name": "compass_cal", "args": {}})
    check(r.status_code == 400, "compass_cal without a heading is refused")
    for bad in (float("nan"), float("inf")):
        r = client.post("/api/command", json={"name": "compass_cal", "args": {"heading": bad}})
        check(r.status_code == 400, f"compass_cal refuses heading={bad!r} ({r.status_code})")
    r = client.post("/api/command", json={"name": "compass_cal", "args": {"heading": "north"}})
    check(r.status_code == 400, "compass_cal refuses a non-numeric heading")
    r = client.post("/api/command", json={"name": "compass_cal", "args": {"heading": 451.5}})
    check(r.status_code == 200, "a heading past 360 is wrapped, not refused")

    # The go-to and its speed cap, implemented on the vessel 2026-08-10
    # (ligmax-pi/nodes/io_manager/guided.py). The cap is bounded by NJORD's 5
    # knots and **refused rather than clamped**: an operator who asked for 4 m/s
    # and silently got 2.57 would believe the boat was doing 4.
    r = client.post("/api/command", json={"name": "set_speed_limit", "args": {"value": 1.0}})
    check(r.status_code == 200, f"a 1 m/s cap is accepted ({r.status_code})")
    r = client.post(
        "/api/command", json={"name": "set_speed_limit", "args": {"value": MAX_SPEED_LIMIT_MS}}
    )
    check(r.status_code == 200, "the vessel limit itself is accepted")
    r = client.post(
        "/api/command", json={"name": "set_speed_limit", "args": {"value": 0.1}}
    )
    check(
        r.status_code == 200,
        f"0.1 m/s is accepted - it is what a first parking attempt runs at "
        f"({r.status_code})",
    )
    for bad in (4.0, 0.05, float("nan"), float("inf")):
        r = client.post(
            "/api/command", json={"name": "set_speed_limit", "args": {"value": bad}}
        )
        check(r.status_code == 400, f"set_speed_limit refuses {bad!r} ({r.status_code})")
    check(
        MAX_SPEED_LIMIT_MS < 2.58,
        f"the ceiling is 5 knots, not something looser ({MAX_SPEED_LIMIT_MS:.4f} m/s)",
    )

    r = client.post("/api/command", json={"name": "goto", "args": {"x": 12.0, "y": -30.0}})
    check(r.status_code == 200, f"a go-to takes grid metres ({r.status_code})")
    r = client.post("/api/command", json={"name": "goto", "args": {"x": 12.0}})
    check(r.status_code == 400, "a go-to needs both coordinates")
    for bad in (float("nan"), float("inf")):
        r = client.post("/api/command", json={"name": "goto", "args": {"x": bad, "y": 0}})
        check(r.status_code == 400, f"a go-to refuses x={bad!r} ({r.status_code})")

    # And the three that were removed the same day rather than implemented:
    # hold/resume are the autopilot panel's autopilot_pause/autopilot_resume, and
    # `raw` aimed an arbitrary payload at the vessel, which is the one thing this
    # allow-list exists to prevent. All three used to render as working buttons
    # and ack "not implemented" a second later (findings.md item 34).
    # Careful mode and the run profiles went the same way on 2026-08-11: three
    # controls for "how fast may the boat go" where `set_speed_limit` now does the
    # whole job, for both nodes and for docking as well. `run_profile` had never
    # worked at all - the vessel did not forward it.
    for gone in ("hold", "resume", "raw", "careful_on", "careful_off", "run_profile"):
        check(gone not in COMMAND_SPECS, f"{gone} is no longer advertised")
        r = client.post("/api/command", json={"name": gone, "args": {}})
        check(r.status_code == 400, f"{gone} is refused outright ({r.status_code})")

    # Still an allow-list. This is the property that makes a stray fetch() from a
    # browser console unable to invent vessel behaviour.
    r = client.post("/api/command", json={"name": "go_faster", "args": {}})
    check(r.status_code == 400, "an unknown command is still refused")

    # And still admin-gated.
    r = _client().post(
        "/api/command", json={"name": "set_speed_limit", "args": {"value": 1.0}}
    )
    check(r.status_code == 403, "a read-only session cannot set the speed")


# ------------------------------------------------------------------ trips


def test_trip_upload_whole():
    section("a whole recording in one POST")
    client = _client()
    name = "20260810-091455-task1.jsonl.gz"

    r = client.post(f"/api/trip/{name}", data=PAYLOAD)
    check(r.status_code == 401, f"no boat key is refused ({r.status_code})")
    r = client.post(
        f"/api/trip/{name}", data=PAYLOAD, headers={"Authorization": "Bearer wrong"}
    )
    check(r.status_code == 401, f"a wrong boat key is refused ({r.status_code})")

    r = client.post(f"/api/trip/{name}", data=PAYLOAD, headers=BOAT)
    check(r.status_code == 200, f"accepted with the boat key ({r.status_code})")
    check(r.get_json().get("complete") is True, "and reported complete")

    stored = os.path.join(os.environ["LIGMAX_TRIP_STORE"], "ligmax", name)
    check(os.path.isfile(stored), "stored under trips/<boat>/<name>")
    check(open(stored, "rb").read() == PAYLOAD, "byte for byte")
    check(gzip.decompress(open(stored, "rb").read()) == _RAW, "and still a valid gzip")

    # The boat re-offers everything it holds after a reconnect. "No need" is the
    # honest answer; an error would be retried forever.
    r = client.post(f"/api/trip/{name}", data=PAYLOAD, headers=BOAT)
    check(
        r.status_code == 200 and r.get_json().get("already_held") is True,
        "re-offering a held recording is not an error",
    )


def test_trip_upload_resume():
    section("chunked upload, and resuming after a drop")
    client = _client()
    name = "20260810-101500-task2.jsonl.gz"
    total = len(PAYLOAD)
    step = 1 << 20

    # A piece that does not start where the file ends is the one thing a sender
    # can get wrong, so it is answered with where it should have started.
    r = client.post(
        f"/api/trip/{name}",
        data=PAYLOAD[:step],
        headers={**BOAT, "Content-Range": f"bytes 4096-{4096 + step - 1}/{total}"},
    )
    check(r.status_code == 409, f"an out-of-order piece is a 409 ({r.status_code})")
    check(
        r.get_json().get("bytes_held") == 0,
        f"and says where to resume from ({r.get_json()})",
    )

    sent = 0
    dropped_once = False
    while sent < total:
        piece = PAYLOAD[sent : sent + step]
        headers = {**BOAT, "Content-Range": f"bytes {sent}-{sent + len(piece) - 1}/{total}"}
        r = client.post(f"/api/trip/{name}", data=piece, headers=headers)
        if r.status_code != 200:
            check(False, f"chunk at {sent} failed: {r.status_code} {r.get_json()}")
            return
        sent += len(piece)

        # Halfway through, pretend the link dropped and the sender lost its place:
        # it asks the server where it got to and carries on from there. This is
        # the whole point of the protocol, so it is exercised rather than assumed.
        if not dropped_once and sent > total // 2:
            dropped_once = True
            held = client.get("/api/trip", headers=BOAT).get_json()["pending"][name]
            check(held == sent, f"the server agrees it holds {sent} bytes (said {held})")
            sent = held

    final = os.path.join(os.environ["LIGMAX_TRIP_STORE"], "ligmax", name)
    check(os.path.isfile(final), "the chunked upload was renamed into place")
    check(os.path.isfile(final) and open(final, "rb").read() == PAYLOAD, "reassembled byte for byte")
    check(not os.path.exists(final + ".part"), "with no .part left behind")


def test_trip_partial_is_not_a_recording():
    section("a partial upload is never a recording")
    client = _client()
    name = "20260810-110000-task3.jsonl.gz"
    total = len(PAYLOAD)
    half = PAYLOAD[: total // 2]

    r = client.post(
        f"/api/trip/{name}",
        data=half,
        headers={**BOAT, "Content-Range": f"bytes 0-{len(half) - 1}/{total}"},
    )
    check(r.status_code == 200 and r.get_json()["complete"] is False, "a first piece is stored")

    listing = client.get("/api/trip", headers=BOAT).get_json()
    names = [t["name"] for t in listing["trips"]]
    check(name not in names, "a half-file is NOT listed as a recording")
    check(listing["pending"].get(name) == len(half), "but is reported as pending, for resume")

    r = client.get(f"/api/trip/ligmax/{name}")
    check(r.status_code == 404, f"and cannot be downloaded ({r.status_code})")

    # An abandoned .part otherwise blocks its own name forever: every retry
    # starts at 0, the file already holds something, and every one gets a 409.
    store = trips.TripStore(os.environ["LIGMAX_TRIP_STORE"])
    part = os.path.join(os.environ["LIGMAX_TRIP_STORE"], "ligmax", name + ".part")
    check(store.sweep() == 0, "a fresh partial is left alone")
    stale = time.time() - trips.PART_MAX_AGE_S - 60
    os.utime(part, (stale, stale))
    check(store.sweep() == 1, "one untouched for hours is swept up")
    check(not os.path.exists(part), "and removed")


def test_trip_names():
    section("hostile names")
    store = trips.TripStore(os.environ["LIGMAX_TRIP_STORE"])
    for bad in ("../../etc/passwd", "..", ".hidden", "a/b", "x.part", "", "a" * 200):
        try:
            store._safe("ligmax", bad)
            check(False, f"{bad!r} was accepted as a recording name")
        except trips.TripError:
            check(True, f"{bad!r} is refused as a recording name")

    for bad in ("../..", "a/b", "boat!"):
        try:
            store._safe(bad, "ok.jsonl.gz")
            check(False, f"vessel name {bad!r} was accepted")
        except trips.TripError:
            check(True, f"vessel name {bad!r} is refused")

    # Whatever Werkzeug does with the escapes, nothing may land outside the tree.
    client = _client()
    r = client.post("/api/trip/..%2f..%2fetc%2fpasswd", data=b"x", headers=BOAT)
    check(r.status_code >= 400, f"a traversal POST is refused ({r.status_code})")
    check(
        os.path.isdir(os.environ["LIGMAX_TRIP_STORE"]),
        "and the trip directory is intact",
    )


def test_trip_limits_and_gates():
    section("limits, downloads and the delete gate")
    client = _client()

    r = client.post(
        "/api/trip/huge.jsonl.gz",
        data=b"x" * 10,
        headers={**BOAT, "Content-Range": f"bytes 0-9/{trips.MAX_TRIP_BYTES + 1}"},
    )
    check(r.status_code == 413, f"a file over the limit is refused up front ({r.status_code})")

    r = client.post("/api/trip/empty.jsonl.gz", data=b"", headers=BOAT)
    check(r.status_code == 400, f"an empty body is refused ({r.status_code})")

    r = client.post(
        "/api/trip/toobig.jsonl.gz",
        data=b"x" * (trips.MAX_CHUNK_BYTES + 1),
        headers={
            **BOAT,
            "Content-Range": f"bytes 0-{trips.MAX_CHUNK_BYTES}/{trips.MAX_TRIP_BYTES}",
        },
    )
    check(r.status_code == 413, f"an oversized chunk is refused ({r.status_code})")

    name = "20260810-091455-task1.jsonl.gz"
    r = client.get(f"/api/trip/ligmax/{name}")
    check(r.status_code == 200 and r.data == PAYLOAD, "a held recording downloads intact")
    check(
        "attachment" in r.headers.get("Content-Disposition", ""),
        "as a download rather than something the browser tries to render",
    )
    # Closed explicitly, and this is not tidiness. `send_from_directory` hands
    # back a response holding an open handle to the file, and the test client
    # never closes one for you. On Linux that is invisible - the delete below
    # unlinks a file someone still has open and both succeed. On Windows, which
    # is what this server actually runs on, the open handle makes the file
    # undeletable and the delete fails. Leaving it open tested a platform the
    # ground station is not.
    r.close()
    check(client.get("/api/trip/ligmax/nope.gz").status_code == 404, "an unknown one is a 404")

    # Reading is open; deleting is not. A recording is evidence, and the people
    # who want it in the tent are the ones without the key.
    check(client.get("/api/trip").status_code == 200, "listing is behind the read gate only")
    check(
        client.delete(f"/api/trip/ligmax/{name}").status_code == 403,
        "deleting needs an admin session",
    )
    admin = _admin_client()
    check(
        admin.delete(f"/api/trip/ligmax/{name}").status_code == 200,
        "an admin may delete",
    )
    check(
        not os.path.isfile(os.path.join(os.environ["LIGMAX_TRIP_STORE"], "ligmax", name)),
        "and the file is gone",
    )


def test_content_range_parsing():
    section("Content-Range")
    check(trips.parse_content_range(None, 512) == (0, 512, False), "no header means the whole file")
    check(
        trips.parse_content_range("bytes 0-99/1000", 100) == (0, 1000, True),
        "a well-formed range parses",
    )
    for header, body, why in (
        ("bytes 0-99/1000", 50, "a range that disagrees with the body length"),
        ("bytes 100-0/1000", 0, "a range that ends before it starts"),
        ("bytes 900-1000/1000", 101, "a range running past the declared total"),
        ("nonsense", 10, "a malformed header"),
    ):
        try:
            trips.parse_content_range(header, body)
            check(False, f"{why} was accepted")
        except trips.TripError:
            check(True, f"{why} is refused")


def test_plan_validation() -> None:
    """What ``set_plan`` lets through to the vessel, field by field.

    This file had no plan test at all, and that is exactly how ``park_no_exit`` came
    to be dropped: ``plan._waypoint`` builds its output field by field, the field was
    never added, and nothing anywhere compared what the page sends against what comes
    out.  The surprise task's final berth is the one place that flag decides the
    outcome — the run ends inside the berth, or the boat reverses out of it — and the
    page has been sending it since 2026-08-12 into a validator that discarded it.
    """
    section("plan validation — what actually reaches the boat")

    from ligmax_gui import plan as planning

    base = {
        "name": "surprise-task",
        "buoyage": "route",
        "cardinal_rule": "inside",
        "waypoints": [
            {"name": "13", "lat": 63.441135, "lon": 10.424155, "role": "transit"},
            {"name": "14", "lat": 63.441063, "lon": 10.424141,
             "role": "park_tag_parallel", "hold_s": 10, "park_probe_deg": 148},
            {"name": "15", "lat": 63.441155, "lon": 10.423620, "role": "buoys"},
            {"name": "16", "lat": 63.440880, "lon": 10.423413, "role": "buoys"},
            {"name": "17", "lat": 63.440810, "lon": 10.423840, "role": "buoys"},
            {"name": "18", "lat": 63.440950, "lon": 10.423950, "role": "park_tag",
             "hold_s": 10, "park_probe_deg": 86, "park_no_exit": True},
        ],
    }

    cleaned, why = planning.validate(base)
    check(why is None, f"the surprise-task course validates ({why})")
    assert cleaned is not None
    check(cleaned["waypoints"][5].get("park_no_exit") is True,
          "park_no_exit SURVIVES validation and reaches the vessel")
    check("park_no_exit" not in cleaned["waypoints"][1],
          "...and is not invented on a waypoint that did not ask for it")
    check(cleaned.get("buoyage") == "route" and cleaned.get("cardinal_rule") == "inside",
          "the two course-level ring rules survive too")
    check("buoyage=route" in planning.summarise(cleaned),
          "...and the audit line says which rules were sent")

    # Refused, not dropped: a flag on a role with no berth to stay in is a typo the
    # operator has to see, and the vessel refuses it identically.
    stray = {"waypoints": [dict(base["waypoints"][2], park_no_exit=True)]}
    _cleaned, why = planning.validate(stray)
    check(why is not None and "park_no_exit" in why,
          f"park_no_exit on 'buoys' is refused: {why}")

    bad = {"waypoints": [dict(base["waypoints"][5], park_no_exit="yes")]}
    _cleaned, why = planning.validate(bad)
    check(why is not None and "true or false" in why,
          f"a non-boolean park_no_exit is refused: {why}")

    for field, value in (("buoyage", "rout"), ("cardinal_rule", "insde")):
        _cleaned, why = planning.validate({**base, field: value})
        check(why is not None and field in why, f"{field}={value!r} is refused: {why}")

    # A plan that says nothing about either rule must come out exactly as it did
    # before they existed, or every other course on the boat has quietly changed.
    plain, why = planning.validate({"waypoints": [base["waypoints"][0]]})
    check(why is None and plain is not None
          and "buoyage" not in plain and "cardinal_rule" not in plain,
          "a course that sets neither rule carries neither")


TESTS = [
    test_command_specs,
    test_plan_validation,
    test_content_range_parsing,
    test_trip_upload_whole,
    test_trip_upload_resume,
    test_trip_partial_is_not_a_recording,
    test_trip_names,
    test_trip_limits_and_gates,
]


def main() -> int:
    started = time.time()
    for test in TESTS:
        test()
    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S) in {time.time() - started:.1f}s:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print(f"all checks passed in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
