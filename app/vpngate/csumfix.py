"""NFQUEUE UDP-checksum fixer for the L2TP/IPsec path.

Some home routers translate the inbound ESP-in-UDP destination
(matching the ephemeral public port they assigned to our outbound
udp/4500 flow) without recomputing the UDP checksum.  The kernel drops
such datagrams at UDP checksum validation (UdpInCsumErrors) before the
IPsec demux can look at them, so every inbound L2TP/IPsec packet is lost
while the outbound side works.

The worker queues inbound udp/4500 datagrams with the netfilter mangle
PREROUTING NFQUEUE target; this daemon recomputes stale UDP checksums
and re-injects the datagram.  Datagrams whose checksum is already valid
pass through byte-identically (only a requeue), which makes the
mitigation harmless on routers without the bug.
"""
from __future__ import annotations

import struct

QUEUE = 7

_fixed = 0
_seen = 0


def _cksum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\x00"
    total = 0
    for i in range(0, len(data), 2):
        total += (data[i] << 8) + data[i + 1]
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return (~total) & 0xFFFF


def _handle(pkt) -> None:                      # noqa: ANN001
    global _fixed, _seen
    _seen += 1
    try:
        data = pkt.get_payload()
        if len(data) >= 34 and data[0] >> 4 == 4 and data[9] == 17:
            ihl = (data[0] & 0x0F) * 4
            sport, dport, ulen, csum = struct.unpack(">HHHH",
                                                     data[ihl:ihl + 8])
            if 8 <= ulen <= len(data) - ihl:
                body = data[ihl + 8:ihl + ulen]
                head = data[ihl:ihl + 6] + b"\x00\x00"
                pseudo = data[12:20] + struct.pack(">BBH", 0, 17, ulen)
                want = _cksum(pseudo + head + body)
                if want != csum and csum != 0:
                    _fixed += 1
                    data = (data[:ihl] + data[ihl:ihl + 6]
                            + struct.pack(">H", want) + data[ihl + 8:])
                    pkt.set_payload(bytes(data))
    except Exception:                          # noqa: BLE001
        pass                                   # never let the queue stall
    pkt.accept()


def run() -> None:
    import sys

    try:
        from netfilterqueue import NetfilterQueue
    except ImportError:
        print("FATAL: python3-netfilterqueue not installed", flush=True)
        sys.exit(1)

    queue = NetfilterQueue()
    queue.bind(QUEUE, _handle)
    print(f"listening on queue {QUEUE}", flush=True)
    try:
        queue.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
