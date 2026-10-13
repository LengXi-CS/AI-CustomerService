"""Explicitly scoped HTTP login access for a trusted IPv4 LAN."""

from ipaddress import IPv4Address, IPv4Network, ip_address, ip_network

PRIVATE_NETWORKS = tuple(
    IPv4Network(value)
    for value in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
    )
)


def lan_networks(value: str) -> tuple[IPv4Network, ...]:
    networks = []
    for item in value.split(","):
        if not item.strip():
            continue
        network = ip_network(item.strip(), strict=False)
        if not isinstance(network, IPv4Network) or not any(
            network.subnet_of(private) for private in PRIVATE_NETWORKS
        ):
            raise ValueError("局域网 HTTP 访问只支持明确的 RFC1918 IPv4 网段")
        networks.append(network)
    return tuple(networks)


def lan_http_allowed(host: str | None, client: str | None, networks: str) -> bool:
    try:
        target, peer = ip_address(host or ""), ip_address(client or "")
        peer = getattr(peer, "ipv4_mapped", None) or peer
        return (
            isinstance(target, IPv4Address)
            and isinstance(peer, IPv4Address)
            and any(target in network and peer in network for network in lan_networks(networks))
        )
    except ValueError:
        return False
