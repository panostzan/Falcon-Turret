"""Tests finding the Pi when its IP changes and Windows keeps stale ARP entries. Run: python tests/test_findpi.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "laptop"))
import receiver   # noqa: E402

MAC = receiver.PI_MAC


def test():
    os.environ.pop("PI_HOST", None)
    # stale entry for the old address (not answering) and a live one: the live one wins
    arp = f"  192.168.137.131       {MAC}     static\n  192.168.137.206       {MAC}     dynamic\n"
    receiver._arp_table = lambda: arp
    receiver._ssh_open = lambda ip, timeout=0.6: ip == "192.168.137.206"
    assert receiver.find_pi_ip() == "192.168.137.206"

    # not in ARP at all, but exactly one SSH host on the network: that is the Pi
    receiver._arp_table = lambda: ""
    receiver._ssh_open = lambda ip, timeout=0.6: ip == "192.168.137.77"
    assert receiver.find_pi_ip() == "192.168.137.77"

    # two SSH hosts: pick the one whose MAC is the Pi's (the scan fills in ARP)
    tables = iter(["", f"  192.168.137.20       aa-bb-cc-dd-ee-ff     dynamic\n  192.168.137.30       {MAC}     dynamic\n"])
    receiver._arp_table = lambda: next(tables)
    receiver._ssh_open = lambda ip, timeout=0.6: ip in ("192.168.137.20", "192.168.137.30")
    assert receiver.find_pi_ip() == "192.168.137.30"

    # nothing answers: fall back to the last known address
    receiver._arp_table = lambda: ""
    receiver._ssh_open = lambda ip, timeout=0.6: False
    assert receiver.find_pi_ip() == receiver.FALLBACK_IP

    os.environ["PI_HOST"] = "10.0.0.5"                       # manual override beats everything
    assert receiver.find_pi_ip() == "10.0.0.5"
    os.environ.pop("PI_HOST")
    print("find-the-Pi tests passed")


if __name__ == "__main__":
    test()
