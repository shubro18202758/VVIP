"""A movement's route line in its direction of travel — pure, no store.

tape.orient_route reads each route segment in the direction the route
drives it: consecutive segments meet at the junction segment_adjacency
records for the pair, a one-way runs from its geometry's start to its end
(corridor.road_graph), and the stored direction stands only when nothing
else tells.  Pinned here on small hand-made networks: segments stored in
either direction, two roads that meet at both ends (on the real network:
one-way Gandhi Road 1012 beside Relief Road 1005, and 1043 / 1007), a pair
with no recorded movement, a segment without geometry.
"""

from __future__ import annotations

from event_emulator.tape import _dist_m, orient_route, route_polyline

# junctions (lon, lat) about 1-2 km apart on the city's latitude belt
JA = (72.570, 23.020)
JB = (72.580, 23.030)
JC = (72.590, 23.030)
JD = (72.600, 23.020)


def _line(a, b, bend=(0.0, 0.0)):
    """a -> b through one bent interior vertex."""
    mid = ((a[0] + b[0]) / 2 + bend[0], (a[1] + b[1]) / 2 + bend[1])
    return [a, mid, b]


class TestOrientRoute:
    def test_segments_follow_the_junctions_between_them(self):
        # 2 is stored backwards (C -> B); the route drives A -> B -> C -> D
        lines = {1: _line(JA, JB), 2: _line(JC, JB), 3: _line(JC, JD)}
        vias = [[JB], [JC]]
        out = orient_route([1, 2, 3], lines, vias=vias)
        assert [seg[0] for seg in out] == [JA, JB, JC]
        assert [seg[-1] for seg in out] == [JB, JC, JD]
        assert out[1] == lines[2][::-1]
        line = route_polyline(out)
        assert line[0] == JA and line[-1] == JD
        assert len(line) == 7                      # each shared junction once

    def test_a_one_way_settles_two_roads_that_meet_at_both_ends(self):
        # 5 (two-way) and 6 (one-way, legal B -> C) both join B and C; the
        # adjacency records the pair through BOTH junctions
        lines = {5: _line(JB, JC, (0, 0.004)), 6: _line(JB, JC, (0, -0.004))}
        both = [[JB, JC]]
        out = orient_route([5, 6], lines, oneway={6: True}, vias=both)
        assert out[1] == lines[6]                  # the one-way, its own way
        assert out[0][-1] == JB and out[0][0] == JC  # so 5 is driven C -> B
        back = orient_route([6, 5], lines, oneway={6: True}, vias=both)
        assert back[0] == lines[6] and back[1][0] == JC and back[1][-1] == JB

    def test_neighbours_settle_a_two_way_pair_that_meets_at_both_ends(self):
        # 7 and 8 both join B and C (both two-way); 9 leaves C for D, so 8
        # must be left at C, entered at B, and 7 driven C -> B
        lines = {7: _line(JB, JC, (0, 0.004)), 8: _line(JB, JC, (0, -0.004)),
                 9: _line(JC, JD)}
        out = orient_route([7, 8, 9], lines, vias=[[JB, JC], [JC]])
        assert (out[0][0], out[0][-1]) == (JC, JB)
        assert (out[1][0], out[1][-1]) == (JB, JC)
        assert (out[2][0], out[2][-1]) == (JC, JD)

    def test_an_unrecorded_pair_meets_at_its_nearest_ends(self):
        lines = {1: _line(JB, JA), 2: _line(JB, JC)}
        out = orient_route([1, 2], lines)           # no vias at all
        assert (out[0][0], out[0][-1]) == (JA, JB)
        assert (out[1][0], out[1][-1]) == (JB, JC)

    def test_continuity_outweighs_the_one_way_rule_on_an_illegal_route(self):
        # a stored route that drives one-way 2 backwards (C -> B): the line
        # still reads continuously instead of jumping ~1 km
        lines = {1: _line(JD, JC), 2: _line(JB, JC)}
        out = orient_route([1, 2], lines, oneway={2: True})
        assert out[1] == lines[2][::-1]
        assert _dist_m(out[0][-1], out[1][0]) < 1.0

    def test_a_lone_segment_keeps_its_stored_direction(self):
        lines = {4: _line(JA, JB)}
        assert orient_route([4], lines) == [lines[4]]
        assert orient_route([4], lines, oneway={4: True}) == [lines[4]]

    def test_a_segment_without_geometry_is_skipped(self):
        lines = {1: _line(JB, JA), 3: _line(JB, JC)}
        out = orient_route([1, 2, 3], lines, vias=[[JB], []])
        assert out[1] == []
        assert (out[0][-1], out[2][0]) == (JB, JB)
        assert orient_route([2], {}) == [[]]


class TestRoutePolyline:
    def test_a_gap_is_bridged_never_dropped(self):
        line = route_polyline([[JA, JB], [JC, JD]])
        assert line == [JA, JB, JC, JD]

    def test_the_shared_junction_appears_once(self):
        assert route_polyline([[JA, JB], [JB, JC]]) == [JA, JB, JC]
        assert route_polyline([[], [JB, JC]]) == [JB, JC]
