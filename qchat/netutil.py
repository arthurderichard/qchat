"""Découverte des interfaces réseau locales (IPv4), sans dépendance externe."""

import fcntl
import ipaddress
import socket
import struct

SIOCGIFFLAGS = 0x8913
SIOCGIFADDR = 0x8915
SIOCGIFBRDADDR = 0x8919
SIOCGIFNETMASK = 0x891B
MAX_SCAN_HOSTS = 4096
IFF_UP = 0x1
IFF_LOOPBACK = 0x8

# Interfaces virtuelles qui ne mènent pas au réseau de l'entreprise.
IGNORED_PREFIXES = ("docker", "br-", "veth", "virbr", "vboxnet", "vmnet", "lxc", "lxd", "tun", "tap")


def _ioctl(sock, request, name: bytes) -> bytes:
    return fcntl.ioctl(sock.fileno(), request, struct.pack("256s", name[:15]))


def local_interfaces():
    """Liste de (nom, ip, broadcast) pour les interfaces actives et utiles."""
    return [(name, ip, bcast) for name, ip, bcast, _ in _interfaces()]


def local_networks(max_prefix=22):
    """Sous-réseaux des interfaces locales, ramenés à /``max_prefix`` au plus large
    autour de notre IP (évite de proposer l'analyse d'un /16 entier)."""
    nets = []
    for _, ip, _, mask in _interfaces():
        if not mask:
            continue
        prefix = max(ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen, max_prefix)
        net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
        if net not in nets:
            nets.append(net)
    return nets


def _interfaces():
    result = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        for _, name in socket.if_nameindex():
            if name.startswith(IGNORED_PREFIXES):
                continue
            raw = name.encode()
            try:
                flags = struct.unpack("H", _ioctl(s, SIOCGIFFLAGS, raw)[16:18])[0]
                if not flags & IFF_UP or flags & IFF_LOOPBACK:
                    continue
                ip = socket.inet_ntoa(_ioctl(s, SIOCGIFADDR, raw)[20:24])
            except OSError:
                continue
            try:
                bcast = socket.inet_ntoa(_ioctl(s, SIOCGIFBRDADDR, raw)[20:24])
            except OSError:
                bcast = None
            try:
                mask = socket.inet_ntoa(_ioctl(s, SIOCGIFNETMASK, raw)[20:24])
            except OSError:
                mask = None
            result.append((name, ip, bcast, mask))
    return result


def local_ips():
    return [ip for _, ip, _ in local_interfaces()]


def broadcast_addresses():
    addrs = {b for _, _, b in local_interfaces() if b and b != "0.0.0.0"}
    addrs.add("255.255.255.255")
    return sorted(addrs)


def parse_ip(text: str):
    """Valide une IPv4 ; renvoie sa forme normalisée ou None."""
    try:
        addr = ipaddress.IPv4Address(text.strip())
    except (ipaddress.AddressValueError, ValueError, AttributeError):
        return None
    if addr.is_multicast or addr.is_unspecified:
        return None
    return str(addr)


def parse_endpoint(text: str, default_port: int):
    """Analyse « ip » ou « ip:port » ; renvoie (ip, port) ou None."""
    text = text.strip()
    port = default_port
    if text.count(":") == 1:
        text, _, port_s = text.partition(":")
        if not port_s.isdigit() or not 0 < int(port_s) < 65536:
            return None
        port = int(port_s)
    ip = parse_ip(text)
    return (ip, port) if ip else None


def parse_targets(text: str):
    """Analyse une liste de cibles séparées par virgules/espaces :
    « 10.0.0.0/24 », « 10.0.0.10-10.0.0.50 », « 10.0.0.10-50 » ou une IP seule.
    Renvoie la liste ordonnée des IP ; lève ValueError avec un message lisible."""
    hosts = []
    for token in text.replace(",", " ").split():
        if "/" in token:
            try:
                net = ipaddress.IPv4Network(token, strict=False)
            except ValueError:
                raise ValueError(f"Réseau invalide : {token}") from None
            if net.num_addresses > MAX_SCAN_HOSTS:
                raise ValueError(f"Plage trop grande : {token} (maximum {MAX_SCAN_HOSTS} adresses)")
            hosts.extend(str(h) for h in (net.hosts() if net.prefixlen < 31 else net))
        elif "-" in token:
            start_s, _, end_s = token.partition("-")
            start = parse_ip(start_s)
            if start and end_s.isdigit():
                end_s = start.rsplit(".", 1)[0] + "." + end_s
            end = parse_ip(end_s)
            if not start or not end or ipaddress.IPv4Address(end) < ipaddress.IPv4Address(start):
                raise ValueError(f"Plage invalide : {token}")
            first, last = int(ipaddress.IPv4Address(start)), int(ipaddress.IPv4Address(end))
            if last - first + 1 > MAX_SCAN_HOSTS:
                raise ValueError(f"Plage trop grande : {token} (maximum {MAX_SCAN_HOSTS} adresses)")
            hosts.extend(str(ipaddress.IPv4Address(i)) for i in range(first, last + 1))
        else:
            ip = parse_ip(token)
            if not ip:
                raise ValueError(f"Adresse invalide : {token}")
            hosts.append(ip)
    hosts = list(dict.fromkeys(hosts))
    if len(hosts) > MAX_SCAN_HOSTS:
        raise ValueError(f"Trop d'adresses ({len(hosts)}, maximum {MAX_SCAN_HOSTS})")
    return hosts
