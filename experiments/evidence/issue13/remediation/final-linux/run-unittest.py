
from collections import Counter
import json
from pathlib import Path
import sys
import unittest

class Results(unittest.TextTestResult):
    def __init__(self, *args):
        super().__init__(*args)
        self.matrix = 0
        self.matrix_passed = 0

    def addSubTest(self, test, subtest, outcome):
        if getattr(test, "_testMethodName", "") == "test_udp_status_validation_matrix":
            self.matrix += 1
            self.matrix_passed += outcome is None
        super().addSubTest(test, subtest, outcome)

sys.path.insert(0, str(Path.cwd()))
suite = unittest.defaultTestLoader.discover("tests")
result = unittest.TextTestRunner(verbosity=2, resultclass=Results).run(suite)
unsuccessful = {id(getattr(test, "test_case", test))
                for test, _ in result.failures + result.errors + result.skipped
                + result.expectedFailures + [(test, "") for test in result.unexpectedSuccesses]}
counts = dict(tests=result.testsRun, passed=result.testsRun-len(unsuccessful),
              failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
              expected_failures=len(result.expectedFailures),
              unexpected_successes=len(result.unexpectedSuccesses),
              udp_matrix_cases=result.matrix, udp_matrix_passed=result.matrix_passed,
              successful=result.wasSuccessful())
Path(sys.argv[1]).write_text(json.dumps(counts, indent=2) + "\n", encoding="utf-8")
raise SystemExit(0 if result.wasSuccessful() else 1)
