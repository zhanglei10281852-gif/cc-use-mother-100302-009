"""传动设备本地化设计闭环基础契约测试。"""

import unittest

from local_design import LocalizationCase, unique_by_identity


class ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.values = {'case_code': 'case-code-001', 'product_line': 'product-line-001', 'customer': 'customer-001', 'revision': 1}

    def test_fingerprint_is_stable(self) -> None:
        left = LocalizationCase(**self.values)
        right = LocalizationCase(**dict(reversed(list(self.values.items()))))
        self.assertEqual(left.fingerprint(), right.fingerprint())

    def test_evolve_keeps_original(self) -> None:
        original = LocalizationCase(**self.values)
        change_key = next(key for key, value in self.values.items() if isinstance(value, str))
        changed = original.evolve(**{change_key: "revised-value"})
        self.assertNotEqual(original.fingerprint(), changed.fingerprint())
        self.assertEqual(getattr(original, change_key), self.values[change_key])

    def test_conflicting_identity_is_rejected(self) -> None:
        first = LocalizationCase(**self.values)
        changed_values = dict(self.values)
        change_key = next(key for key in self.values if key != "case_code")
        changed_values[change_key] = 2 if isinstance(changed_values[change_key], int) else "conflict"
        second = LocalizationCase(**changed_values)
        with self.assertRaises(ValueError):
            unique_by_identity([first, second])


if __name__ == "__main__":
    unittest.main()
