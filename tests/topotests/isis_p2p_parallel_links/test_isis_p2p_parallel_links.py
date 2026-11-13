#!/usr/bin/env python
# SPDX-License-Identifier: ISC
#
# Copyright (C) 2026 Yoann Lopez
#

"""
Test two parallel point-to-point links between the same two routers.

          10.0.1.0/24, metric 10
    +----+------------------------+----+
    | r1 |                        | r2 |
    +----+------------------------+----+
          10.0.2.0/24, metric 50

Each router reaches the other's loopback over the metric 10 link. When that
link goes down, traffic moves to the metric 50 link; when it comes back, the
SPF must pair each IS reachability entry with the adjacency of its own
circuit, and traffic must move back to the metric 10 link. Flapping the
metric 50 link must not move traffic.
"""

import functools
import json
import os
import sys
import time

import pytest

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.common_config import step
from lib.topogen import Topogen, get_topogen

pytestmark = [pytest.mark.isisd]

# The other router's loopback, per router.
PEER_LOOPBACK = {"r1": "10.255.0.2/32", "r2": "10.255.0.1/32"}


def build_topo(tgen):
    for router in ("r1", "r2"):
        tgen.add_router(router)

    for name in ("s1", "s2"):
        switch = tgen.add_switch(name)
        switch.add_link(tgen.gears["r1"])
        switch.add_link(tgen.gears["r2"])


def setup_module(mod):
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    for router in tgen.routers().values():
        router.load_frr_config()

    tgen.start_router()


def teardown_module():
    get_topogen().stop_topology()


def _route_to_peer(router, interface, metric):
    """The route to the other router's loopback, through one interface."""
    prefix = PEER_LOOPBACK[router.name]
    expected = {
        prefix: [
            {
                "protocol": "isis",
                "selected": True,
                "metric": metric,
                "nexthops": [{"interfaceName": interface, "active": True}],
            }
        ]
    }
    output = json.loads(router.vtysh_cmd("show ip route {} json".format(prefix)))
    result = topotest.json_cmp(output, expected)
    if result is None and len(output[prefix][0]["nexthops"]) != 1:
        return "{}: more than one nexthop: {}".format(
            router.name, output[prefix][0]["nexthops"]
        )
    return result


def _expect_route(interface, metric):
    """Both routers reach each other's loopback through an interface."""
    tgen = get_topogen()
    for name in ("r1", "r2"):
        router = tgen.gears[name]
        test_func = functools.partial(
            _route_to_peer, router, "{}-{}".format(name, interface), metric
        )
        _, result = topotest.run_and_expect(test_func, None, count=40, wait=0.5)
        assert result is None, "{}: route not through {}-{}: {}".format(
            name, name, interface, result
        )


def _adjacencies_up(router, count):
    output = json.loads(router.vtysh_cmd("show isis neighbor json"))
    states = [
        c["state"]
        for area in output.get("areas", [])
        for c in area.get("circuits", [])
        if "adj" in c
    ]
    if states != ["Up"] * count:
        return "{}: adjacencies {}".format(router.name, states)
    return None


def _flap(interface):
    """Take r1's interface down, then up again."""
    r1 = get_topogen().gears["r1"]
    r1.cmd("ip link set r1-{} down".format(interface))
    time.sleep(2)
    r1.cmd("ip link set r1-{} up".format(interface))


def test_converged():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("Both adjacencies come up on each router")
    for router in tgen.routers().values():
        test_func = functools.partial(_adjacencies_up, router, 2)
        _, result = topotest.run_and_expect(test_func, None, count=60, wait=0.5)
        assert result is None, result

    step("Traffic uses the metric 10 link")
    _expect_route("eth0", 20)


def test_cheaper_link_flap():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    for cycle in (1, 2):
        step("Cycle {}: the metric 10 link goes down, traffic moves".format(cycle))
        r1 = tgen.gears["r1"]
        r1.cmd("ip link set r1-eth0 down")
        _expect_route("eth1", 60)

        step("Cycle {}: the metric 10 link comes back, traffic moves back".format(cycle))
        r1.cmd("ip link set r1-eth0 up")
        _expect_route("eth0", 20)


def test_costlier_link_flap():
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    step("The metric 50 link flaps, traffic stays on the metric 10 link")
    _flap("eth1")
    for router in tgen.routers().values():
        test_func = functools.partial(_adjacencies_up, router, 2)
        _, result = topotest.run_and_expect(test_func, None, count=60, wait=0.5)
        assert result is None, result
    _expect_route("eth0", 20)


def test_memory_leak():
    tgen = get_topogen()
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")

    tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
