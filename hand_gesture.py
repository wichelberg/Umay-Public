"""What the hands in the camera mean for the mouse ("Umay, eyes on"): pure, no camera, no MediaPipe, so the tests
run without them. hands_worker.py feeds it MediaPipe's 21 landmarks of each hand every frame and does what it
answers. Eyes takes both hands; one hand is Hand, the pointer; two at once (in the same pose) are TwoHands.

One hand (Hand):
- The cursor follows the palm's middle (the wrist and the knuckles), not a fingertip: closing the hand moves
  the fingertips a lot and the palm hardly at all, so grabbing doesn't jump the cursor.
- Like a trackpad, not a map of the picture (2026-11-03: the middle of a 20 fps camera's picture stretched
  over both monitors made a shaking hand jump the cursor, and the far corners took a long reach): the hand's
  movement moves the cursor, slow movement finely (GAIN_PX px a camera width), fast movement up to MAX_GAIN
  times further, across the monitors. Where the hand is in the picture doesn't matter; a hand coming back
  carries on from where the cursor is (the real mouse moving it too).
- An open hand moves the cursor; a fist holds the left button down (a drag) until the hand opens again. Fist
  / open needs FRAMES frames in a row, and between the two (2 fingers out) the hand keeps what it was, so it
  doesn't flicker.
- A tap (the thumb's tip touching the index's, the middle finger out, like an OK sign) is a click; two quick
  taps are two clicks, a double click. The cursor stands still while tapping and CLICK_STILL_SECONDS after it:
  Windows takes two clicks as a double click only if the cursor moved less than 4 px between them. The middle
  finger has to be out so that closing the hand into a fist (the thumb then passes the index) is never a tap.
- ✌ (index and middle out, the others in) held for PEACE_SECONDS turns the eyes off.
- A push: the hand side-on to the camera (its back leading, as if brushing something aside: it looks narrow,
  side_on) and moved slowly sideways PUSH_DIST closes the active window, the one just clicked. Side-on, the
  cursor stands and nothing is tapped. No speed needed (2026-11-03: the owner's hand moved at 0.1-0.6 camera
  widths a second; quick flicks blur at the laptop camera's 20 frames a second and the hand is lost).
- A fist thrown (FLING_SPEED camera widths a second over FLING_WINDOW, measured on the palm before the filter,
  which lags) is a throw, left / right / up / down: the button goes up and hands_worker does something with
  the window that was grabbed (to the other monitor or closed, maximized, minimized). A drag is far slower.
  After a throw the fist doesn't grab again until the hand opens.
- A One Euro filter smooths the palm: still when the hand is still, quick when it moves fast.
- The hand gone for LOST_SECONDS while holding: the button goes up, nothing stays grabbed.

Two hands, Tony Stark's way (TwoHands), each pose held FRAMES frames by both:
- Both open: their distance zooms (Ctrl + the wheel: a page, a picture, a map), ZOOM_STEP of a camera width a
  step, apart = in.
- Both side-on, pushed apart SPREAD_DIST (backs of the hands leading, outwards): every window, side by side
  (Win+Tab).
- Both fists: the window under the cursor is stretched with them, wider / taller as they move apart.
While both hands are in a pose the cursor stands still; a drag the pointer hand had going is let go first.
"""
import math
import re

# Cameras the eyes never use: the owner's phone (Phone Link: "<phone> (Windows Virtual Camera)"), IR, virtual ones.
NOT_CAMERAS = re.compile(r"virtual|phone|\bir\b|infrared|nvidia|broadcast|obs|droidcam|iriun|link", re.IGNORECASE)
PALM = (0, 5, 9, 13, 17)                           # wrist and the knuckles
FINGERS = ((8, 6), (12, 10), (16, 14), (20, 18))   # (tip, middle joint) of index, middle, ring, little finger
FRAMES = 3
LOST_SECONDS = 0.4
PINCH_ON, PINCH_OFF = 0.28, 0.45   # thumb tip to index tip, in hand sizes (wrist to the middle knuckle)
PINCH_FRAMES = 2
CLICK_STILL_SECONDS = 0.35
PEACE_SECONDS = 1.0
GAIN_PX = 1600          # px the cursor goes for a camera width of slow movement
MAX_GAIN = 4.0          # fast movement goes this many times further
SLOW, FAST = 0.1, 1.0   # camera widths a second: from SLOW on the gain grows, at FAST it is MAX_GAIN
STILL_SPEED = 0.03      # camera widths a second: slower is the shaking of a still hand (~1.5 px a frame)
FLING_SPEED = 2.0       # camera widths (or heights) a second: a quick drag is ~1, a flick ~3
FLING_WINDOW = 0.15     # seconds the speed is measured over
OUT_SPEED = 1.2         # a held fist gone at this speed...
OUT_EDGE = 0.18         # ...this close to the picture's edge it was heading for: thrown out of the picture
ZOOM_STEP = 0.06        # camera widths the two palms' distance changes for one zoom step
SIDE_RATIO = 0.45       # knuckles (index's to little finger's) closer than this many hand sizes: side-on
PUSH_DIST = 0.15        # camera widths a side-on hand moves sideways: a push (closes the active window)
SPREAD_DIST = 0.2       # camera widths two side-on hands part by: every window
STRETCH_PX = 2000       # px the window grows for a camera width the fists part by
STRETCH_MIN_PX = 4      # smaller changes aren't sent

LOG = None  # hands_worker sets it: LOG(text) notes why a throw / a push was taken or not, for tuning


def note(text):
    if LOG is not None:
        LOG(text)


def cameras_to_try(listed, wanted=""):
    """The camera indexes worth trying, of [(index, name)]: the named one (part of its name), else every camera
    that isn't a phone, IR, virtual... (NOT_CAMERAS)."""
    if wanted:
        return [i for i, name in listed if wanted.lower() in name.lower()]
    return [i for i, name in listed if not NOT_CAMERAS.search(name)]


def best_camera(measured):
    """The camera to use of [(index, frames a second)]: the fastest, the first of equals; None if none gave any."""
    working = [(fps, -k, index) for k, (index, fps) in enumerate(measured) if fps > 0]
    return max(working)[2] if working else None


def palm(lm):
    """The palm's middle (x, y), normalized to the picture."""
    return sum(lm[i][0] for i in PALM) / len(PALM), sum(lm[i][1] for i in PALM) / len(PALM)


def mirrored_palm(lm):
    """The palm's middle as in a mirror: the hand's right is the picture's right."""
    x, y = palm(lm)
    return 1 - x, y


def extended(lm):
    """How many of the four fingers are out: the tip further from the wrist than the finger's middle joint."""
    return sum(fingers_out(lm))


def fingers_out(lm):
    """[index, middle, ring, little]: which fingers are out."""
    wx, wy = lm[0][:2]

    def far(i):
        return math.hypot(lm[i][0] - wx, lm[i][1] - wy)
    return [far(tip) > far(joint) * 1.1 for tip, joint in FINGERS]


def hand_size(lm):
    return math.hypot(lm[9][0] - lm[0][0], lm[9][1] - lm[0][1]) or 1e-6


def pinch(lm):
    """How far apart the thumb's and the index's tips are, in hand sizes."""
    return math.hypot(lm[4][0] - lm[8][0], lm[4][1] - lm[8][1]) / hand_size(lm)


def is_peace(lm):
    """✌: index and middle out, ring and little finger in."""
    return fingers_out(lm) == [True, True, False, False]


def knuckle_span(lm):
    """How far apart the index's and the little finger's knuckles look, in hand sizes: ~0.7 for a palm (or a
    back) facing the camera, near 0 for a hand side-on to it."""
    return math.hypot(lm[5][0] - lm[17][0], lm[5][1] - lm[17][1]) / hand_size(lm)


def side_on(lm):
    """An open hand side-on to the camera (a push's pose)."""
    return extended(lm) >= 3 and knuckle_span(lm) < SIDE_RATIO


def is_fist(lm, was_fist):
    """A fist (0-1 fingers out), open (3-4), or as it was (2: on the way)."""
    n = extended(lm)
    return True if n <= 1 else False if n >= 3 else was_fist


def monitor_of(screens, x, y):
    """The monitor (left, top, right, bottom) of [screens] the point is on, else the nearest one's."""
    for s in screens:
        if s[0] <= x < s[2] and s[1] <= y < s[3]:
            return s
    return min(screens, key=lambda s: math.dist(((s[0] + s[2]) / 2, (s[1] + s[3]) / 2), (x, y)))


def next_monitor(screens, here, way):
    """The monitor beside `here` on that side ("left" / "right"), the nearest one; None if there is none."""
    cx = (here[0] + here[2]) / 2
    side = [s for s in screens if ((s[0] + s[2]) / 2 < cx if way == "left" else (s[0] + s[2]) / 2 > cx)]
    return min(side, key=lambda s: abs((s[0] + s[2]) / 2 - cx)) if side else None


def place_on(rect, here, there):
    """Where a window (left, top, right, bottom) goes from one monitor's work area to another's: (x, y, w, h),
    in the same place relative to the area, no bigger than it."""
    hw, hh = (here[2] - here[0]) or 1, (here[3] - here[1]) or 1
    tw, th = there[2] - there[0], there[3] - there[1]
    w, h = min(rect[2] - rect[0], tw), min(rect[3] - rect[1], th)
    x = there[0] + round((rect[0] - here[0]) * tw / hw)
    y = there[1] + round((rect[1] - here[1]) * th / hh)
    return min(max(x, there[0]), there[2] - w), min(max(y, there[1]), there[3] - h), w, h


def _way(vx, vy):
    if abs(vx) >= abs(vy):
        return "right" if vx > 0 else "left"
    return "down" if vy > 0 else "up"


def out_of_picture(flight, now):
    """The way a held fist that is gone now was thrown out of the picture, or None: it was going at OUT_SPEED or
    faster (just before) and was about to reach the picture's edge that way. A drag that loses the hand for a
    frame mid-picture is no throw (a throw may close a window)."""
    t, vx, vy, x, y = flight
    if now - t > FLING_WINDOW * 2 or max(abs(vx), abs(vy)) < OUT_SPEED:
        return None
    way = _way(vx, vy)
    ahead = {"right": x + vx * FLING_WINDOW, "left": 1 - (x + vx * FLING_WINDOW),
             "down": y + vy * FLING_WINDOW, "up": 1 - (y + vy * FLING_WINDOW)}[way]
    return way if ahead >= 1 - OUT_EDGE else None


def gain(speed):
    """How much further than GAIN_PX the cursor goes at `speed` camera widths a second."""
    return 1 + (MAX_GAIN - 1) * min(1.0, max(0.0, (speed - SLOW) / (FAST - SLOW)))


class OneEuro:
    """The One Euro filter (Casiez et al. 2012) for one value."""

    def __init__(self, min_cutoff=1.2, beta=0.004, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.x = self.dx = self.t = None

    @staticmethod
    def _alpha(cutoff, dt):
        r = 2 * math.pi * cutoff * dt
        return r / (r + 1)

    def __call__(self, x, t):
        if self.t is None or t <= self.t:
            self.x, self.dx, self.t = x, 0.0, t
            return x
        dt = t - self.t
        dx = (x - self.x) / dt
        a = self._alpha(self.d_cutoff, dt)
        self.dx = a * dx + (1 - a) * self.dx
        a = self._alpha(self.min_cutoff + self.beta * abs(self.dx), dt)
        self.x = a * x + (1 - a) * self.x
        self.t = t
        return self.x

    def reset(self):
        self.x = self.dx = self.t = None


class Hand:
    """The pointer hand's state from frame to frame: update() answers what the mouse should do.
    area: the desktop (left, top, right, bottom px); aspect: the picture's height / width."""

    def __init__(self, area, aspect=0.75):
        self.area, self.aspect = area, aspect
        self.fx, self.fy = OneEuro(1.0, 8.0), OneEuro(1.0, 8.0)  # in camera widths: beta ~ 2000x the px one's
        self.pos = ((area[0] + area[2]) / 2, (area[1] + area[3]) / 2)
        self.last = None           # (time, filtered x, y) of the previous frame with the hand
        self.fist = False          # what the hand is, after FRAMES frames agreed
        self.pressed = False       # the left button is down
        self.votes = 0
        self.last_seen = None
        self.pinching, self.pinch_votes = False, 0
        self.still_until = 0.0     # no moves until then (a tap, a double tap)
        self.peace_since = None
        self.trail = []            # (time, x, y) of the palm while holding, for a throw
        self.flight = None         # (time, vx, vy, x, y): the held palm's last speed and where it was
        self.top_speed = 0.0       # the fastest it went while held (for the log)
        self.side_votes = self.side_misses = 0   # frames side-on / not since
        self.push_from = None      # where a side-on hand started (x, mirrored)
        self.pushed = False        # this push has closed a window: the next needs the pose left first

    def update(self, lm, now, cursor=None):
        """lm: the 21 landmarks [(x, y, z)] or None (no hand); cursor: where the cursor is now (px), if known.
        Returns [("move", x, y), ("down",), ("up",), ("click",), ("fling", way), ("stop",)]."""
        out = []
        if lm is None:
            if self.pressed and self.flight is not None:
                way, self.flight = out_of_picture(self.flight, now), None
                if way:   # thrown out of the picture (2026-11-03: a throw to the outer monitor edge left it first)
                    self.pressed, self.votes, self.trail = False, 0, []
                    note(f"throw {way}: the fist left the picture that way")
                    return [("up",), ("fling", way)]
            if self.pressed and self.last_seen is not None and now - self.last_seen >= LOST_SECONDS:
                self.pressed, self.fist, self.votes = False, False, 0
                note(f"let go: the fist was lost (fastest {self.top_speed:.1f} widths/s)")
                out.append(("up",))
            return out
        if self.last_seen is None or now - self.last_seen >= LOST_SECONDS:
            self.forget()          # back after a while: no jump from where it was
        self.last_seen = now
        ux, uy = mirrored_palm(lm)
        move = self._move(ux, uy, now, cursor)
        if is_peace(lm) and not self.pressed:
            self.peace_since = self.peace_since or now
            if now - self.peace_since >= PEACE_SECONDS:
                self.peace_since = None
                return out + [("stop",)]
        else:
            self.peace_since = None
        if not self.pressed:
            if self._push(lm, ux):
                return out + [("close",)]
            if self.side_votes:
                return out         # side-on: a push, not a move, nor a tap (the thumb may hide the index)
        if not self.pressed and self._tap(lm, now):
            self.still_until = now + CLICK_STILL_SECONDS
            return out + [("click",)]
        if self.pinching or now < self.still_until:
            return out
        if move:
            out.append(("move", round(self.pos[0]), round(self.pos[1])))
        if self.pressed:
            throw = self._throw(ux, uy, now)
            if throw:
                self.pressed, self.votes, self.trail, self.flight = False, 0, [], None
                return out + [("up",), ("fling", throw)]
        fist = is_fist(lm, self.fist)
        self.votes = self.votes + 1 if fist != self.fist else 0
        if self.votes >= FRAMES:
            self.fist, self.votes = fist, 0
            if fist and not self.pressed:
                self.pressed, self.trail, self.flight, self.top_speed = True, [], None, 0.0
                out.append(("down",))
            elif not fist and self.pressed:
                self.pressed = False
                note(f"let go: the hand opened (fastest {self.top_speed:.1f} widths/s, a throw is {FLING_SPEED})")
                out.append(("up",))
        return out

    def _move(self, ux, uy, now, cursor):
        """Moves self.pos by the filtered palm's movement since the last frame; True if it moved a pixel."""
        x, y = self.fx(ux, now), self.fy(uy, now)
        last, self.last = self.last, (now, x, y)
        if cursor is not None and math.dist(cursor, self.pos) > 3:
            self.pos = tuple(map(float, cursor))   # the real mouse moved it, or the edge of the desktop held it
        if last is None or now <= last[0]:
            return False
        dx, dy = x - last[1], (y - last[2]) * self.aspect
        speed = math.hypot(dx, dy) / (now - last[0])
        if speed < STILL_SPEED or self.pinching or now < self.still_until:
            return False           # the filter follows, the cursor stands (a still hand shakes a little)
        g = gain(speed)
        left, top, right, bottom = self.area
        old = self.pos
        self.pos = (min(right - 1, max(left, old[0] + dx * GAIN_PX * g)),
                    min(bottom - 1, max(top, old[1] + dy * GAIN_PX * g)))
        return round(self.pos[0]) != round(old[0]) or round(self.pos[1]) != round(old[1])

    def forget(self):
        """The hand's next frame starts afresh (no movement from an old position)."""
        self.fx.reset()
        self.fy.reset()
        self.last = None

    def _throw(self, x, y, now):
        """"left" / "right" / "up" / "down" when the held palm moves at FLING_SPEED or faster, else None."""
        self.trail = [p for p in self.trail if now - p[0] <= FLING_WINDOW] + [(now, x, y)]
        t0, x0, y0 = self.trail[0]
        if now - t0 < FLING_WINDOW * 0.5:
            return None
        vx, vy = (x - x0) / (now - t0), (y - y0) / (now - t0)
        tp, xp, yp = self.trail[-2]
        last = ((x - xp) / (now - tp), (y - yp) / (now - tp)) if now > tp else (0.0, 0.0)
        # A throw speeds up: over FLING_WINDOW it is slower than at its end, where the hand may leave the picture.
        self.flight = (now, *max((vx, vy), last, key=lambda v: max(map(abs, v))), x, y)
        self.top_speed = max(self.top_speed, abs(vx), abs(vy))
        if max(abs(vx), abs(vy)) < FLING_SPEED:
            return None
        note(f"throw {_way(vx, vy)}: {max(abs(vx), abs(vy)):.1f} widths/s")
        return _way(vx, vy)

    def _push(self, lm, x):
        """True on the frame a side-on hand has gone PUSH_DIST sideways (once a push). FRAMES frames make the
        pose, and as many not side-on end it (a frame misjudged doesn't)."""
        if side_on(lm):
            self.side_votes, self.side_misses = self.side_votes + 1, 0
        elif self.side_votes:
            self.side_misses += 1
            if self.side_misses >= FRAMES:
                if self.push_from is not None and not self.pushed:
                    note(f"no push: side-on, it went {abs(x - self.push_from):.2f} of the {PUSH_DIST} needed")
                self.side_votes = self.side_misses = 0
                self.push_from, self.pushed = None, False
                self.forget()      # no jump for where it went meanwhile
            return False
        if self.side_votes < FRAMES:
            return False
        if self.push_from is None:
            self.push_from = x
            note(f"side-on (knuckles {knuckle_span(lm):.2f} hand sizes apart, under {SIDE_RATIO}): a push may start")
            return False
        if self.pushed or abs(x - self.push_from) < PUSH_DIST:
            return False
        self.pushed = True
        note(f"push: side-on, {abs(x - self.push_from):.2f} sideways: closing the active window")
        return True

    def no_push(self):
        """A push needs the side-on pose taken afresh (two hands just pushed: one still side-on isn't one)."""
        self.side_votes, self.side_misses, self.push_from, self.pushed = FRAMES, 0, None, True

    def _tap(self, lm, now):
        """True on the frame a tap starts (PINCH_FRAMES frames of thumb on index, the middle finger out)."""
        d = pinch(lm)
        if self.pinching:
            if d > PINCH_OFF:
                self.pinching, self.pinch_votes = False, 0
                self.still_until = now + CLICK_STILL_SECONDS
            return False
        if d < PINCH_ON and fingers_out(lm)[1] and not self.fist:
            self.pinch_votes += 1
            if self.pinch_votes >= PINCH_FRAMES:
                self.pinching, self.pinch_votes = True, 0
                return True
        else:
            self.pinch_votes = 0
        return False

    def let_go(self):
        """[("up",)] if the button is down, else []; a fist still closed doesn't grab again until it opens."""
        if self.pressed:
            self.pressed, self.votes, self.trail, self.flight = False, 0, [], None
            return [("up",)]
        return []

    release = let_go


def _pose(a, b):
    """"side" (both hands side-on), "open" (both), "fist" (both) or None."""
    if side_on(a) and side_on(b):
        return "side"
    na, nb = extended(a), extended(b)
    if na >= 3 and nb >= 3:
        return "open"
    if na <= 1 and nb <= 1:
        return "fist"
    return None


class TwoHands:
    """Both hands in one pose: zoom (open), every window (side-on), stretching a window (fists)."""

    def __init__(self, aspect=0.75):
        self.aspect = aspect
        self.mode, self.wanted, self.votes = None, None, 0
        self.zoom_from = None
        self.spread_from, self.spread = None, False
        self.stretch_from, self.sent = None, (0, 0)

    def _apart(self, a, b):
        (ax, ay), (bx, by) = mirrored_palm(a), mirrored_palm(b)
        return abs(ax - bx), abs(ay - by) * self.aspect

    def update(self, a, b, now):
        """a, b: both hands' landmarks (None: not both there). Returns (in a pose, actions)."""
        pose = _pose(a, b) if a is not None and b is not None else None
        out = []
        if pose != self.mode:
            self.votes = self.votes + 1 if pose == self.wanted else 1
            self.wanted = pose
            if self.votes >= FRAMES or (pose is None and self.mode is not None and self.votes >= 2):
                out += self._leave()
                if pose is not None:
                    out += self._enter(pose, a, b, now)
                self.votes = 0
        else:
            self.votes, self.wanted = 0, None
        if pose is not None and pose == self.mode:   # (a frame out of the pose, on the way out: nothing)
            out += {"open": self._zoom, "side": self._spread, "fist": self._stretch}[pose](a, b)
        return self.mode is not None, out

    def _enter(self, pose, a, b, now):
        self.mode = pose
        if pose == "open":
            self.zoom_from = math.hypot(*self._apart(a, b))
            return []
        if pose == "side":
            self.spread_from, self.spread, self.parted = self._apart(a, b)[0], False, 0.0
            return []
        self.stretch_from, self.sent = self._apart(a, b), (0, 0)
        return [("stretch_start",)]

    def _leave(self):
        mode, self.mode = self.mode, None
        if mode == "fist":
            return [("stretch_end",)]
        if mode == "side" and not self.spread:
            note(f"no spread: two side-on hands parted {self.parted:.2f} of the {SPREAD_DIST} needed")
        return []

    def _zoom(self, a, b):
        d = math.hypot(*self._apart(a, b))
        steps = int((d - self.zoom_from) / ZOOM_STEP)
        if not steps:
            return []
        self.zoom_from += steps * ZOOM_STEP
        return [("zoom", 1 if steps > 0 else -1)] * min(3, abs(steps))

    def _spread(self, a, b):
        self.parted = self._apart(a, b)[0] - self.spread_from
        if self.spread or self.parted < SPREAD_DIST:
            return []
        self.spread = True
        note(f"every window: two side-on hands parted {self.parted:.2f}")
        return [("overview",)]

    def _stretch(self, a, b):
        (x, y), (x0, y0) = self._apart(a, b), self.stretch_from
        grow = (round((x - x0) * STRETCH_PX), round((y - y0) * STRETCH_PX))
        if max(abs(grow[0] - self.sent[0]), abs(grow[1] - self.sent[1])) < STRETCH_MIN_PX:
            return []
        self.sent = grow
        return [("stretch", *grow)]

    def stop(self):
        return self._leave()


class Eyes:
    """Both hands, frame by frame: the pointer (Hand) and, with both in a pose, TwoHands."""

    def __init__(self, area, aspect=0.75):
        self.pointer, self.both = Hand(area, aspect), TwoHands(aspect)
        self.pointer_at = None     # the pointer hand's palm last frame (mirrored), to know it among two

    def update(self, hands, now, cursor=None):
        """hands: the landmarks of each hand seen (0-2). Returns the actions, as Hand.update and TwoHands do."""
        hands = list(hands)[:2]
        if len(hands) == 2:
            if self.pointer_at is None:   # the hand on the right (the owner's right hand) points
                hands.sort(key=lambda lm: -mirrored_palm(lm)[0])
            else:
                hands.sort(key=lambda lm: math.dist(mirrored_palm(lm), self.pointer_at))
        pointer, other = (hands + [None, None])[:2]
        self.pointer_at = mirrored_palm(pointer) if pointer is not None else self.pointer_at
        was = self.both.mode is not None
        posed, acts = self.both.update(pointer, other, now)
        if posed:
            if not was:
                acts = self.pointer.let_go() + acts
            return acts
        if was:
            self.pointer.forget()          # no jump for where the hand went meanwhile
            self.pointer.fist = True       # a hand still closed from a stretch grabs nothing until it opens
            self.pointer.no_push()         # nor does one still side-on from a spread close a window
        return acts + self.pointer.update(pointer, now, cursor)

    @property
    def pressed(self):
        return self.pointer.pressed

    @property
    def pinching(self):
        return self.pointer.pinching

    @property
    def peace_since(self):
        return self.pointer.peace_since

    @property
    def mode(self):
        return self.both.mode

    @property
    def pushing(self):
        return self.both.mode == "side" or self.pointer.push_from is not None

    def release(self):
        """What to undo when stopping: the held button, a stretch."""
        return self.both.stop() + self.pointer.let_go()
