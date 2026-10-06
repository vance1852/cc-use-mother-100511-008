import unittest

from ai_governance_foundation.planner import (
    FixedAllocation,
    ReservationInput,
    Window,
    plan_allocations,
)


def reservation(application_id, *, sequence, priority_rank=2, committed=False,
                amount=1, duration=3, earliest=10, deadline=20,
                team="team-a", task="task-1", pool="gpu", preferred=None):
    return ReservationInput(
        application_id=application_id, team_id=team, task_id=task,
        resource_pool=pool, amount=amount, duration_slots=duration,
        earliest_slot=earliest, deadline_slot=deadline,
        priority_rank=priority_rank, committed=committed, sequence=sequence,
        preferred_resource_id=preferred)


def windows(capacity=1, end=30):
    return [Window("gpu-1", 0, end, capacity), Window("gpu-2", 0, end, capacity)]


class PlannerDeterminismTest(unittest.TestCase):
    def test_same_inputs_produce_identical_fingerprint(self):
        reservations = [
            reservation("app-1", sequence=1),
            reservation("app-2", sequence=2, priority_rank=1),
            reservation("app-3", sequence=3),
        ]
        first = plan_allocations(resources={"gpu-1": "gpu", "gpu-2": "gpu"},
                                 windows=windows(), fixed=[],
                                 reservations=reservations, current_slot=0)
        # 故意打乱输入顺序，结果必须一致。
        shuffled = plan_allocations(resources={"gpu-2": "gpu", "gpu-1": "gpu"},
                                    windows=list(reversed(windows())), fixed=[],
                                    reservations=list(reversed(reservations)),
                                    current_slot=0)
        self.assertEqual(first.fingerprint, shuffled.fingerprint)
        by_id = {d.application_id: d for d in first.decisions}
        shuffled_by_id = {d.application_id: d for d in shuffled.decisions}
        self.assertEqual(by_id["app-1"].placement, shuffled_by_id["app-1"].placement)

    def test_priority_and_sequence_decide_unique_winners(self):
        # 三台申请都只能在槽位 10 起跑（duration=6, deadline=16），两资源容量各 1。
        result = plan_allocations(
            resources={"gpu-1": "gpu", "gpu-2": "gpu"}, windows=windows(), fixed=[],
            reservations=[
                reservation("low-late", sequence=3, priority_rank=2, deadline=13),
                reservation("high", sequence=2, priority_rank=0, deadline=13),
                reservation("low-early", sequence=1, priority_rank=2, deadline=13),
            ], current_slot=0)
        status = {d.application_id: d.outcome for d in result.decisions}
        self.assertEqual("confirmed", status["high"])
        self.assertEqual("confirmed", status["low-early"])
        self.assertEqual("waitlisted", status["low-late"])

    def test_committed_wins_tie_at_same_priority(self):
        result = plan_allocations(
            resources={"gpu-1": "gpu"}, windows=windows(), fixed=[],
            reservations=[
                reservation("plain", sequence=1, committed=False, deadline=13),
                reservation("promised", sequence=2, committed=True, deadline=13),
            ], current_slot=0)
        status = {d.application_id: d.outcome for d in result.decisions}
        self.assertEqual("waitlisted", status["plain"])
        self.assertEqual("confirmed", status["promised"])

    def test_capacity_is_shared_slot_by_slot(self):
        result = plan_allocations(
            resources={"gpu-1": "gpu"},
            windows=[Window("gpu-1", 0, 30, 2)], fixed=[],
            reservations=[
                reservation("a", sequence=1, amount=2, deadline=13),
                reservation("b", sequence=2, amount=1, deadline=13),
            ], current_slot=0)
        status = {d.application_id: d.outcome for d in result.decisions}
        self.assertEqual("confirmed", status["a"])
        self.assertEqual("waitlisted", status["b"])

    def test_earliest_feasible_placement_respects_window_gap(self):
        result = plan_allocations(
            resources={"gpu-1": "gpu"},
            windows=[Window("gpu-1", 0, 12, 1), Window("gpu-1", 14, 30, 1)],
            fixed=[], reservations=[reservation("a", sequence=1, duration=3,
                                                earliest=10, deadline=20)],
            current_slot=0)
        placement = {d.application_id: d.placement for d in result.decisions}["a"]
        self.assertEqual(14, placement.start_slot)

    def test_waitlisted_application_gets_beyond_deadline_alternatives(self):
        result = plan_allocations(
            resources={"gpu-1": "gpu"}, windows=[Window("gpu-1", 0, 30, 1)],
            fixed=[],
            reservations=[reservation("a", sequence=1, duration=3,
                                      earliest=10, deadline=13)],
            current_slot=0)
        decision = result.decisions[0]
        self.assertEqual("confirmed", decision.outcome)
        crowded = plan_allocations(
            resources={"gpu-1": "gpu"}, windows=[Window("gpu-1", 0, 30, 1)],
            fixed=[],
            reservations=[
                reservation("winner", sequence=1, duration=10, earliest=10, deadline=25),
                reservation("loser", sequence=2, duration=3, earliest=10, deadline=13),
            ], current_slot=0)
        loser = next(d for d in crowded.decisions if d.application_id == "loser")
        self.assertEqual("waitlisted", loser.outcome)
        self.assertTrue(loser.alternatives)
        self.assertTrue(all(alt.beyond_deadline for alt in loser.alternatives))


class FixedRunTest(unittest.TestCase):
    def test_started_run_is_never_displaced_or_overcommitted(self):
        fixed = [FixedAllocation("alloc-1", "running-app", "gpu-1", 10, 13, 1)]
        result = plan_allocations(
            resources={"gpu-1": "gpu", "gpu-2": "gpu"}, windows=windows(),
            fixed=fixed,
            reservations=[
                reservation("new-high", sequence=2, priority_rank=0,
                            earliest=10, deadline=20),
            ], current_slot=0)
        placement = result.decisions[0].placement
        # 即使新申请优先级更高，也不能落到 gpu-1 的 10-13 槽位。
        self.assertNotIn(placement.resource_id, (None,))
        if placement.resource_id == "gpu-1":
            self.assertGreaterEqual(placement.start_slot, 13)
        self.assertEqual("running-app", fixed[0].application_id)

    def test_pool_with_no_remaining_capacity_reports_unavailable_reason(self):
        result = plan_allocations(
            resources={"gpu-1": "gpu"},
            windows=[Window("gpu-1", 0, 30, 1)],
            fixed=[FixedAllocation("alloc-1", "running-app", "gpu-1", 0, 30, 1)],
            reservations=[reservation("a", sequence=1, earliest=0, deadline=30)],
            current_slot=0)
        decision = result.decisions[0]
        self.assertEqual("waitlisted", decision.outcome)
        self.assertEqual("no_capacity_before_deadline", decision.reason)

    def test_unknown_pool_is_reported_explicitly(self):
        result = plan_allocations(
            resources={"gpu-1": "gpu"}, windows=windows(), fixed=[],
            reservations=[reservation("a", sequence=1, pool="tpu")],
            current_slot=0)
        self.assertEqual("resource_pool_unavailable", result.decisions[0].reason)


if __name__ == "__main__":
    unittest.main()
