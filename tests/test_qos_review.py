
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from controller.ryu_controller import RyuController
from network.qos_runtime import apply, cleanup, build_apply
from network.qos_plan import QOS_PORTS


class TestQoSReview(unittest.TestCase):

    def test_icmp_protocol_isolation(self):
        ctrl = SimpleNamespace(
            current_path="A",
            _sw=lambda dpid: dpid,
            _edge_port=lambda t, e, d: 1 if e == 1 else 2,
            _l3_fwd=Mock()
        )

        RyuController._install_l3_flows(
            ctrl, "10.0.10.1", "10.0.40.10",
            "icmp", 0, None, None
        )

        calls = ctrl._l3_fwd.call_args_list
        self.assertEqual(len(calls), 6)

        for call in calls:
            self.assertEqual(
                call.kwargs["l4"]["ip_proto"], 1
            )

    def test_all_classes_on_both_paths(self):
        cases = [
            ("10.0.10.1", "10.0.40.10", "tcp", 80, 1),
            ("10.0.30.1", "10.0.40.30", "udp", 5000, 2),
            ("10.0.20.1", "10.0.40.20", "tcp", 5201, 3),
        ]

        for path, port in [("A", 4), ("B", 5)]:
            for sip, dip, proto, dport, queue in cases:
                ctrl = SimpleNamespace(
                    current_path=path,
                    _sw=lambda dpid: dpid,
                    _edge_port=lambda t, e, d: 1 if e == 1 else 2,
                    _l3_fwd=Mock()
                )

                RyuController._install_l3_flows(
                    ctrl, sip, dip, proto, dport, None, None
                )

                for index in (2, 3):
                    kwargs = ctrl._l3_fwd.call_args_list[index].kwargs
                    self.assertEqual(kwargs["queue_id"], queue)
                    self.assertEqual(kwargs["next_port"], port)

    @patch("network.qos_runtime.subprocess.run")
    @patch("network.qos_runtime.ovs")
    def test_apply_empty_ports(self, ovs, run):
        ovs.return_value = "[]"

        apply("review-test")

        self.assertEqual(ovs.call_count, len(QOS_PORTS))
        run.assert_called_once_with(
            build_apply("review-test"), check=True
        )

    @patch("network.qos_runtime.subprocess.run")
    @patch("network.qos_runtime.ovs")
    def test_cleanup_preserves_foreign_qos(self, ovs, run):
        ovs.side_effect = [
            "owned-qos",
            "owned-queue",
            "owned-qos",
            "foreign-qos",
            "[]",
            "[]"
        ]

        cleanup("review-test")

        run.assert_called_once()
        cmd = run.call_args.args[0]

        self.assertIn("owned-qos", cmd)
        self.assertIn("owned-queue", cmd)
        self.assertIn(QOS_PORTS[0], cmd)

        self.assertNotIn("foreign-qos", cmd)
        self.assertNotIn(QOS_PORTS[1], cmd)


if __name__ == "__main__":
    unittest.main()
