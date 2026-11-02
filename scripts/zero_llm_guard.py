"""Offline screening guard: Python network operations may use loopback only."""
import ipaddress
import json
import os
import sys


def allowed_host(host):
    if isinstance(host, bytes):
        host = host.decode('ascii', errors='replace')
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def install(log_path):
    def audit(event, args):
        host = None
        if event in ('socket.connect', 'socket.bind'):
            address = args[1]
            if isinstance(address, tuple):
                host = address[0]
        elif event == 'socket.getaddrinfo':
            host = args[0]
        elif event == 'socket.sendto':
            address = args[-1]
            if isinstance(address, tuple):
                host = address[0]
        if host is not None and not allowed_host(host):
            with open(log_path, 'a') as stream:
                stream.write(json.dumps({'event': event, 'host': str(host), 'blocked': True}) + '\n')
            raise PermissionError('zero_llm: external networking disabled')
    sys.addaudithook(audit)


def clean_env():
    # Do not read .env or pass model credentials/proxies to either child process.
    return {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'LC_ALL', 'TZ')}
