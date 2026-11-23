import unittest
from hkipo.quota import evaluate_limits, QuotaUnavailable


class QuotaTests(unittest.TestCase):
    def limits(self, used=25):
        return {"rateLimitsByLimitId": {"codex": {"limitId": "codex",
                "secondary": {"windowDurationMins": 300, "usedPercent": used, "resetsAt": 2000},
                "primary": {"windowDurationMins": 10080, "usedPercent": 90, "resetsAt": 3000}},
                "code_review": {"limitId": "code_review", "primary": {"windowDurationMins": 300, "usedPercent": 100, "resetsAt": 2000}}}}

    def test_window_duration_not_primary_and_review_excluded(self):
        self.assertEqual(evaluate_limits(self.limits(), now=1000)["remaining"], .75)

    def test_boundary_equality_preserves_reserve(self):
        self.assertFalse(evaluate_limits(self.limits(80), now=1000)["allowed"])
        self.assertTrue(evaluate_limits(self.limits(79), now=1000)["allowed"])

    def test_unverified_not_unlimited(self):
        for limits in [{}, self.limits()]:
            with self.assertRaises(QuotaUnavailable):
                evaluate_limits(limits, now=3000)

    def test_week_exhaustion_hard_limit(self):
        limits = self.limits()
        limits["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 100
        self.assertTrue(evaluate_limits(limits, now=1000)["hard_limit"])


class WeeklyPlanTests(unittest.TestCase):
    def limits(self, used=0):
        return {"ordinaryUsageAllowed": True, "rateLimitsByLimitId": {"codex": {
            "limitId": "codex", "planType": "prolite", "secondary": None,
            "primary": {"windowDurationMins": 10080, "usedPercent": used, "resetsAt": 3000},
            "spendControlReached": False, "rateLimitReachedType": None}}}

    def test_explicit_weekly_plan_uses_actual_window(self):
        result = evaluate_limits(self.limits(), now=1000)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(result['window_kind'], 'weekly')
        self.assertEqual(result['window_duration_mins'], 10080)
        self.assertEqual(result['resets_at'], 3000)
        ordinary = evaluate_limits(QuotaTests().limits(), now=1000)
        self.assertEqual(ordinary['window_kind'], 'five_hour')
        self.assertEqual(ordinary['window_duration_mins'], 300)

    def test_weekly_exception_requires_explicit_identity_and_permission(self):
        for plan in [None, 'plus', 'unknown']:
            limits = self.limits(); limits['rateLimitsByLimitId']['codex']['planType'] = plan
            with self.assertRaises(QuotaUnavailable): evaluate_limits(limits, now=1000)
        for allowed in [None, False, 1, 'true']:
            limits = self.limits(); limits['ordinaryUsageAllowed'] = allowed
            with self.assertRaises(QuotaUnavailable): evaluate_limits(limits, now=1000)
        limits = self.limits(); limits['rateLimitsByLimitId']['codex']['secondary'] = {}
        with self.assertRaises(QuotaUnavailable): evaluate_limits(limits, now=1000)

    def test_weekly_invalid_or_expired_readings_rejected(self):
        for field, values in [('usedPercent', [True, -1, 101, float('nan'), '0']),
                              ('resetsAt', [True, 1000, 999, 1000 + 10080 * 60 + 1, float('inf'), None]),
                              ('windowDurationMins', [True, 3000, float('nan'), '10080'])]:
            for value in values:
                with self.subTest(field=field, value=value):
                    limits = self.limits(); limits['rateLimitsByLimitId']['codex']['primary'][field] = value
                    with self.assertRaises(QuotaUnavailable): evaluate_limits(limits, now=1000)

    def test_weekly_reserve_exhaustion_and_hard_flags(self):
        from hkipo.quota import quota_deferral
        self.assertTrue(evaluate_limits(self.limits(79), now=1000)['allowed'])
        for used in [80, 100]:
            result = evaluate_limits(self.limits(used), now=1000)
            self.assertFalse(result['allowed'])
            self.assertEqual(quota_deferral(result, now=1000)['retry_at'], 3120)
            self.assertIsNone(quota_deferral(result, now=1061))
        for flag, value in [('spendControlReached', True), ('rateLimitReachedType', 'unknown')]:
            limits = self.limits(); limits['rateLimitsByLimitId']['codex'][flag] = value
            result = evaluate_limits(limits, now=1000)
            self.assertFalse(result['allowed']); self.assertTrue(result['hard_limit'])
            self.assertIsNone(quota_deferral(result, now=1000))


class QuotaDeferralTests(unittest.TestCase):
    limits = QuotaTests.limits
    def test_weekly_reset_and_padding(self):
        from hkipo.quota import quota_deferral
        limits = self.limits(85)
        limits['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = 100
        observation = evaluate_limits(limits, now=1000)
        self.assertEqual(observation['resets_at'], 3000)
        self.assertEqual(quota_deferral(observation, now=1000)['retry_at'], 3120)
        self.assertIsNone(quota_deferral(observation, now=1061))

    def test_unknown_hard_or_invalid_reset_not_auto_retry(self):
        from hkipo.quota import quota_deferral
        for changes in [{'spendControlReached': True}, {'rateLimitReachedType': 'unknown'}]:
            limits = self.limits(85)
            limits['rateLimitsByLimitId']['codex'].update(changes)
            self.assertIsNone(quota_deferral(evaluate_limits(limits, now=1000), now=1000))
        limits = self.limits(85)
        limits['rateLimitsByLimitId']['codex']['primary']['resetsAt'] = 900
        with self.assertRaises(QuotaUnavailable):
            evaluate_limits(limits, now=1000)

    def test_gate_typed_observation_and_no_model(self):
        from unittest.mock import patch
        from hkipo.quota import quota_gate, QuotaDeferred
        with patch('hkipo.quota.time.time', return_value=1000), patch('hkipo.quota.read_limits', return_value=self.limits(80)) as read:
            with self.assertRaises(QuotaDeferred) as error:
                quota_gate()
            self.assertEqual(error.exception.observation['blocking_resets_at'], 2000)
            self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
