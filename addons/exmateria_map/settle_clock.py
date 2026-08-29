"""WHEN a pause after painting becomes a compile -- ADR-0186 Amendment 7.

Decision 28 closes the loop on a **settle**: a pause of about 1.5 s after
painting stops, after which the compile and the push both fire and no button
is pressed in the normal loop.  The compile already existed; what did not was
a way to know that painting had *stopped*.

Amendment 7 named `wm.operators` as the intended witness and left it open.
Measured in Blender 5.2.0 LTS it is the wrong one -- it is not populated by
`bpy.ops` calls at all, and neither `bpy.msgbus` (which does not fire on a
paint stroke) nor the undo stack (whose depth 5.2 does not expose to Python)
answers either.  What does is the candidate the ADR listed third: the canvas's
own content.  A digest of the painting is stable when untouched, moves on a
SINGLE texel, and holds still the moment the artist stops -- and costs 5.4 ms
on a 256x1024 sheet, or about 2% of one core at 4 Hz.

So this module is handed a time and a digest, and answers one question.  It is
`bpy`-free (ADR-0007 decision 4) because the rule is worth testing without a
window, a brush or a clock -- and because the compile it triggers runs off the
main thread (decision 30), which the `bpy`-free split is what makes possible.
"""

__all__ = ["SettleClock", "QUIET_DEFAULT"]

#: Amendment 7's first guess, and it says plainly that nothing measured it: it
#: is "long enough not to fire between two strokes of one gesture" and no more.
#: A **preference**, never a constant -- an artist who paints in slow
#: deliberate dabs needs a longer one, and one who scrubs needs a shorter.
QUIET_DEFAULT = 1.5


class SettleClock:
    """Compile now, or not.  One question, and no side effects.

    The state is three digests and one timestamp:

    * `_seen` -- what the canvas held at the last tick.  A tick that disagrees
      with it is painting IN PROGRESS, and restarts the pause.  This is the
      whole of what keeps a settle from firing between two strokes of one
      gesture.
    * `_compiled` -- what the last compile actually read.  A canvas equal to it
      needs nothing, which is what makes an idle Blender idle.
    * `_flight` -- what a compile is reading right now, or `None`.  A second
      compile of one map would race the first on the mesh and on the sheet, so
      a tick during one says nothing; and because `_seen` keeps tracking, paint
      that lands mid-compile is caught by the NEXT settle rather than dropped.
    """

    def __init__(self, quiet=QUIET_DEFAULT):
        self.quiet = quiet
        self._seen = None
        self._changed_at = None
        self._compiled = None
        self._flight = None

    @property
    def last_compiled(self):
        """What the last compile read, or `None` before there was one."""
        return self._compiled

    def observe(self, now, digest):
        """`digest` if this tick is a settle, else `None`.

        `now` is passed in rather than read, so the rule is testable and so a
        caller may use whatever clock its host offers.
        """
        if digest != self._seen:
            # The canvas moved: this is painting, not a pause.  The pause
            # starts again from here, however many ticks it has already run.
            self._seen = digest
            self._changed_at = now
            return None
        if self._flight is not None:
            return None
        if digest == self._compiled:
            return None
        if self._changed_at is None or now - self._changed_at < self.quiet:
            return None
        self._flight = digest
        return digest

    def compiled(self, digest):
        """A compile of `digest` has LANDED.

        Also the way a caller declares a starting point -- a map that has just
        been converted, or opened, is compiled from what it holds.
        """
        self._compiled = digest
        if self._flight == digest:
            self._flight = None
        if self._seen is None:
            self._seen = digest

    def abandoned(self):
        """A compile ended without landing -- it raised, or the map went away.

        The pause is not restarted: whatever is on the canvas still differs
        from what was last compiled, so the next quiet tick tries again.
        """
        self._flight = None
