
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ryu.ofproto import ofproto_v1_3_parser as parser
from controller.ryu_controller import (
    RyuController, classify, QUEUE_IDS
)
from network.qos_plan import QUEUES, LINK_CAPACITY


class TestQoS(unittest.TestCase):

    def test_classification(self):
        self.assertEqual(QUEUE_IDS[classify('tcp', 80)], 1)
        self.assertEqual(QUEUE_IDS[classify('udp', 5000)], 2)
        self.assertEqual(QUEUE_IDS[classify('tcp', 5201)], 3)
        self.assertEqual(QUEUE_IDS[classify('icmp', 0)], 0)

    def test_bandwidth_budget(self):
        total = sum(q['min'] for q in QUEUES.values())
        self.assertLessEqual(total, LINK_CAPACITY)
        self.assertGreaterEqual(QUEUES[2]['min'], 5_000_000)

    def test_qos_enabled(self):
        ctrl = SimpleNamespace(qos_enabled=True)

        for dpid in (1, 4):
            for port in (4, 5):
                dp = SimpleNamespace(
                    id=dpid, ofproto_parser=parser
                )
                for queue_id in (0, 1, 2, 3):
                    actions = RyuController._output_actions(
                        ctrl, dp, port, queue_id
                    )
                    self.assertEqual(len(actions), 2)
                    self.assertEqual(actions[0].queue_id, queue_id)
                    self.assertEqual(actions[1].port, port)

    def test_qos_disabled(self):
        ctrl = SimpleNamespace(qos_enabled=False)
        dp = SimpleNamespace(id=1, ofproto_parser=parser)

        actions = RyuController._output_actions(ctrl, dp, 4, 1)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].port, 4)

    def test_unmanaged_port(self):
        ctrl = SimpleNamespace(qos_enabled=True)
        dp = SimpleNamespace(id=1, ofproto_parser=parser)

        actions = RyuController._output_actions(ctrl, dp, 1, 1)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].port, 1)

    def test_bidirectional_queues(self):
        for path, port in [('A', 4), ('B', 5)]:
            ctrl = SimpleNamespace(
                current_path=path,
                _sw=lambda dpid: dpid,
                _edge_port=lambda t, e, d: 1 if e == 1 else 2,
                _l3_fwd=Mock()
            )

            RyuController._install_l3_flows(
                ctrl, '10.0.10.1', '10.0.40.10',
                'tcp', 80, None, None
            )

            calls = ctrl._l3_fwd.call_args_list

            for index in (2, 3):
                self.assertEqual(calls[index].kwargs['queue_id'], 1)
                self.assertEqual(calls[index].kwargs['next_port'], port)


if __name__ == '__main__':
    unittest.main()
