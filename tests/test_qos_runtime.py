
import unittest
from unittest.mock import patch

from network.qos_runtime import build_apply, apply, cleanup


class TestQoSRuntime(unittest.TestCase):

    def test_four_ports(self):
        cmd = build_apply("test-run")
        self.assertEqual(cmd.count("QoS"), 4)

    def test_sixteen_queues(self):
        cmd = build_apply("test-run")
        self.assertEqual(cmd.count("Queue"), 16)

    @patch("network.qos_runtime.subprocess.run")
    @patch("network.qos_runtime.ovs")
    def test_refuse_existing_qos(self, ovs, run):
        ovs.return_value = "existing-qos"

        with self.assertRaises(RuntimeError):
            apply("test-run")

        run.assert_not_called()

    @patch("network.qos_runtime.subprocess.run")
    @patch("network.qos_runtime.ovs")
    def test_cleanup_no_owned_records(self, ovs, run):
        ovs.return_value = ""
        cleanup("test-run")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
